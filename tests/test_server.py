"""Server tests with canned API responses (no network needed).

The fixtures mirror real response shapes from IODA and RIPEstat, captured from
the Iberian blackout (2025-04-28) and the Optus outage (2023-11-08).
"""

import asyncio
import json

import pytest
from mcp import Client

from upstream import server

IODA_EVENTS_ES = [
    {"location": "country/ES", "start": 1745836200, "duration": 55200, "datasource": "ping-slash24",
     "score": 35446.8, "location_name": "Spain"},
    {"location": "country/ES", "start": 1745848800, "duration": 44700, "datasource": "bgp",
     "score": 751.1, "location_name": "Spain"},
]
# 12 points of normal, then a drop to ~40%, five-minute steps starting 08:00Z.
IODA_SIGNAL_ES = [[
    # Newer IODA datasources carry a list of per-region records per point, not a number.
    # Captured from the live API in Nov 2025; this shape used to crash the summarizer.
    {
        "entityType": "country", "entityCode": "ES", "entityName": "Spain", "datasource": "ping-slash24-loss",
        "from": 1745827200, "until": 1745838000, "step": 600,
        "values": [[{"country_code": "ES", "geo_db": "ipinfo", "agg_values": {"loss_pct": 32.06, "probe_count": 69745}}]] * 18,
    },
    {
        "entityType": "country", "entityCode": "ES", "entityName": "Spain", "datasource": "bgp",
        "from": 1745827200, "until": 1745838000, "step": 300,
        "values": [100] * 12 + [None] + [40] * 12 + [55] * 11,
    },
]]
IODA_SUMMARY_AU = [{
    "scores": {"ping-slash24.median": 31323.5, "bgp.median": 20218.7, "overall": 633319914.2},
    "event_cnt": 2,
    "entity": {"code": "4804", "name": "AS4804 (MPX-AS)", "type": "asn", "attrs": {"org": "Optus"}},
}]
RIPE_AS_OVERVIEW = {"status": "ok", "data": {"holder": "CLOUDFLARENET - Cloudflare, Inc.", "announced": True}}
RIPE_NETWORK_INFO = {"status": "ok", "data": {"asns": ["13335"], "prefix": "1.1.1.0/24"}}
RIPE_UPDATES = {"status": "ok", "data": {"sampling_period_human": "1 hour", "updates": [
    {"starttime": "2021-10-04T14:00:00", "announcements": 900, "withdrawals": None},
    {"starttime": "2021-10-04T15:00:00", "announcements": 101897, "withdrawals": None},
    {"starttime": "2021-10-04T16:00:00", "announcements": 5182, "withdrawals": None},
]}}
STATUS_SUMMARY = {
    "status": {"indicator": "minor", "description": "Partially Degraded Service"},
    "components": [{"name": "Actions", "status": "degraded_performance"}, {"name": "API", "status": "operational"}],
    "incidents": [{"name": "Delayed Actions runs", "status": "investigating", "impact": "minor",
                   "created_at": "2026-11-23T18:00:00Z", "incident_updates": [{"body": "We are investigating."}],
                   "shortlink": "https://stspg.io/x"}],
}


async def fake_get_json(url, params=None):
    if "network-info" in url:
        return RIPE_NETWORK_INFO
    if "as-overview" in url:
        return RIPE_AS_OVERVIEW
    if "bgp-update-activity" in url:
        return RIPE_UPDATES
    if "/outages/events" in url:
        return {"error": None, "data": IODA_EVENTS_ES}
    if "/signals/raw/" in url:
        return {"error": None, "data": IODA_SIGNAL_ES}
    if "/outages/summary" in url:
        assert params["relatedTo"] == "country/AU"
        return {"error": None, "data": IODA_SUMMARY_AU}
    if url.endswith("summary.json"):
        return STATUS_SUMMARY
    raise AssertionError(f"unexpected URL {url}")


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    monkeypatch.setattr(server, "get_json", fake_get_json)


def call(tool, **arguments):
    async def go():
        async with Client(server.mcp) as client:
            result = await client.call_tool(tool, arguments)
            text = "".join(b.text for b in result.content if b.type == "text")
            return result.is_error, (json.loads(text) if not result.is_error else text)

    return asyncio.run(go())


def test_lookup_ip_resolves_asn_and_holder():
    err, out = call("lookup_network", resource="1.1.1.1")
    assert not err
    assert out["prefix"] == "1.1.1.0/24"
    assert out["asns"][0] == {"asn": "AS13335", "holder": "CLOUDFLARENET - Cloudflare, Inc.", "announced": True}


def test_ioda_events_are_humanized():
    err, out = call("ioda_outage_events", entity_type="country", entity_code="es",
                    start="2025-04-28T06:00Z", end="2025-04-29T06:00Z")
    assert not err and out["entity"] == "country/ES" and out["event_count"] == 2
    assert out["events"][0]["start"] == "2025-04-28T10:30Z"
    assert out["events"][0]["signal"] == "ping-slash24"


def test_signal_summary_finds_the_drop():
    err, out = call("ioda_signal_summary", entity_type="country", entity_code="ES",
                    start="2025-04-28T08:00Z", end="2025-04-28T11:00Z")
    assert not err
    assert [s["signal"] for s in out["signals"]] == ["bgp"]
    sig = out["signals"][0]
    assert sig["baseline"] == 100
    assert sig["lowest"]["pct_of_baseline"] == 40
    assert sig["lowest"]["at"] == "2025-04-28T09:05Z"
    # The non-core datasource (list-of-dicts values) is named, not summarized, and must not crash the call.
    assert out["other_datasources_not_summarized"] == ["ping-slash24-loss"]


def test_signal_summary_skips_non_numeric_series_when_asked_for_explicitly():
    """Asking for a datasource whose values aren't numbers reports it as skipped instead of crashing."""
    weird = {"datasource": "ping-slash24-loss", "from": 0, "step": 600,
             "values": [[{"agg_values": {"loss_pct": 1.0}}]] * 5}
    out = server._summarize_series(weird)
    assert out["signal"] == "ping-slash24-loss" and out["note"].startswith("Skipped")


def test_bgp_update_burst_is_flagged():
    err, out = call("bgp_update_activity", resource="AS32934", start="2021-10-04T14:00Z", end="2021-10-04T17:00Z")
    assert not err
    assert out["peak"] == {"at": "2021-10-04T15:00:00", "updates": 101897}
    assert out["peak_vs_median"] > 10


def test_top_outages_within_country():
    err, out = call("ioda_top_outages", start="2023-11-07T16:00Z", end="2023-11-08T05:00Z",
                    entity_type="asn", within_country="au")
    assert not err
    assert out["outages"][0]["entity"] == "asn/4804"
    assert out["outages"][0]["signals"] == ["bgp", "ping-slash24"]


def test_service_status():
    err, out = call("service_status", service="GitHub")
    assert not err and out["page"] == "www.githubstatus.com"
    assert out["degraded_components"] == [{"component": "Actions", "status": "degraded_performance"}]
    assert out["open_incidents"][0]["latest_update"] == "We are investigating."


def test_bad_input_is_a_tool_error_not_a_crash():
    err, text = call("bgp_update_activity", resource="AS1", start="2021-10-05T00:00Z", end="2021-10-04T00:00Z")
    assert err and "must be after start" in text
    err, text = call("ioda_outage_events", entity_type="asn", entity_code="Optus", start="now-1d", end="now")
    assert err and "not an ASN" in text


def test_scenarios_resource_hides_answer_keys():
    async def go():
        async with Client(server.mcp) as client:
            res = await client.read_resource("upstream://scenarios")
            return json.loads(res.contents[0].text)

    scenarios = asyncio.run(go())
    assert {s["id"] for s in scenarios} >= {"iberian-blackout", "facebook-bgp"}
    assert all("expect" not in s for s in scenarios)
