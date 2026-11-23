"""internet-signals: an MCP server over free, public internet-health data.

Sources (no API keys needed):
  * RIPEstat  - BGP routing data from RIPE NCC's route collectors
  * IODA      - Georgia Tech's Internet Outage Detection & Analysis
                (BGP visibility, active probing, darknet telescope signals)
  * Statuspage - the public JSON API most SaaS status pages expose

Design choices worth noting:
  * Tools return *summaries*, not raw API payloads. An IODA time series is
    hundreds of numbers; the model needs "baseline, worst point, % drop,
    and when". Summarizing server-side cuts tokens and reduces misreads.
  * Every tool takes an explicit time window, so the same tools work for
    live triage and for replaying historical incidents.
  * Outbound HTTP calls get their own client spans, so a trace shows which
    upstream API made a slow tool slow.

Run over stdio (default, for Claude Desktop / Claude Code / the agent) or
over Streamable HTTP with --http (for hosting it as a remote connector).
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import time
from typing import Any, Literal
from urllib.parse import urlparse

import httpx2
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from opentelemetry.trace import SpanKind, StatusCode

from upstream import __version__, telemetry
from upstream.scenarios import as_public_dicts
from upstream.timeutil import iso, iso_ts, parse_time, window

RIPESTAT = "https://stat.ripe.net/data/{call}/data.json"
IODA = "https://api.ioda.inetintel.cc.gatech.edu/v2"
USER_AGENT = f"upstream-triage/{__version__} (+https://github.com/)"
SOURCEAPP = "upstream-triage"  # RIPEstat asks clients to identify themselves

# Status pages known to use the Atlassian Statuspage JSON API. Any other
# Statuspage-hosted domain can be passed directly (e.g. "status.example.com").
STATUS_PAGES = {
    "github": "www.githubstatus.com",
    "cloudflare": "www.cloudflarestatus.com",
    "atlassian": "status.atlassian.com",
    "discord": "discordstatus.com",
    "reddit": "www.redditstatus.com",
    "twilio": "status.twilio.com",
    "datadog": "status.datadoghq.com",
    "dropbox": "status.dropbox.com",
}

READ_ONLY = ToolAnnotations(readOnlyHint=True, openWorldHint=True)

INSTRUCTIONS = """\
Tools for answering "is it us, or is it upstream?" with public internet data.
A good triage moves outward in layers and stops when the evidence is clear:
  1. Identify the network: lookup_network on an IP, prefix or ASN.
  2. Is that network reachable and routed? bgp_routing_status, bgp_update_activity.
  3. Is the whole country/region affected? ioda_signal_summary, ioda_outage_events.
  4. What else is broken right now? ioda_top_outages (optionally within one country).
  5. Is a SaaS vendor reporting an incident? service_status, service_incident_history.
All times are UTC. Every tool takes an explicit [start, end] window, so they work
for live incidents and for replaying historical ones.
"""

mcp = MCPServer(name="internet-signals", version=__version__, instructions=INSTRUCTIONS)
tracer = telemetry.tracer("upstream.internet_signals")

# --------------------------------------------------------------------------- #
# HTTP plumbing
# --------------------------------------------------------------------------- #

_CACHE: dict[str, tuple[float, Any]] = {}
_CACHE_TTL_S = 60.0  # be polite to free public APIs during a demo


async def get_json(url: str, params: dict[str, Any] | None = None) -> Any:
    """GET a JSON document, with a client span, a short cache, and tool-friendly errors."""
    host = urlparse(url).hostname or url
    key = url + "?" + json.dumps(params or {}, sort_keys=True)
    with tracer.start_as_current_span(
        f"GET {host}",
        kind=SpanKind.CLIENT,
        attributes={"http.request.method": "GET", "server.address": host, "url.full": url},
    ) as span:
        cached = _CACHE.get(key)
        if cached and time.monotonic() - cached[0] < _CACHE_TTL_S:
            span.set_attribute("upstream.cache_hit", True)
            return cached[1]
        span.set_attribute("upstream.cache_hit", False)
        try:
            async with httpx2.AsyncClient(timeout=30, headers={"User-Agent": USER_AGENT}) as client:
                resp = await client.get(url, params=params)
        except httpx2.HTTPError as exc:
            span.set_status(StatusCode.ERROR, type(exc).__name__)
            span.set_attribute("error.type", type(exc).__name__)
            raise ToolError(f"Could not reach {host}: {type(exc).__name__}. The source may be down; try again.") from exc
        span.set_attribute("http.response.status_code", resp.status_code)
        if resp.status_code >= 400:
            span.set_status(StatusCode.ERROR)
            span.set_attribute("error.type", str(resp.status_code))
            raise ToolError(f"{host} returned HTTP {resp.status_code} for {resp.url}")
        try:
            body = resp.json()
        except ValueError as exc:
            raise ToolError(f"{host} did not return JSON (is that a Statuspage-hosted status page?)") from exc
        _CACHE[key] = (time.monotonic(), body)
        return body


async def ripestat(call: str, **params: Any) -> dict[str, Any]:
    body = await get_json(RIPESTAT.format(call=call), {**params, "sourceapp": SOURCEAPP})
    if body.get("status") != "ok":
        raise ToolError(f"RIPEstat {call} failed: {body.get('messages') or body.get('status')}")
    return body.get("data") or {}


async def ioda(path: str, **params: Any) -> Any:
    body = await get_json(IODA + path, {k: v for k, v in params.items() if v is not None})
    if body.get("error"):
        raise ToolError(f"IODA {path} failed: {body['error']}")
    return body.get("data")


def _ripe_time(dt) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M")


def _window(start: str, end: str, max_days: int = 14):
    try:
        return window(start, end, max_days)
    except ValueError as exc:
        raise ToolError(str(exc)) from exc


def _asn(value: str) -> str:
    """'AS13335' / 'as13335' / '13335' -> '13335'."""
    digits = value.strip().upper().removeprefix("AS")
    if not digits.isdigit():
        raise ToolError(f"{value!r} is not an ASN. Use a form like AS13335.")
    return digits


def _trim(obj: Any, max_items: int = 12, _depth: int = 0) -> Any:
    """Shrink a nested JSON payload so it fits comfortably in a model's context."""
    if _depth > 5:
        return "…"
    if isinstance(obj, dict):
        return {k: _trim(v, max_items, _depth + 1) for k, v in obj.items()}
    if isinstance(obj, list):
        items = [_trim(v, max_items, _depth + 1) for v in obj[:max_items]]
        if len(obj) > max_items:
            items.append(f"… {len(obj) - max_items} more")
        return items
    if isinstance(obj, str) and len(obj) > 400:
        return obj[:400] + "…"
    return obj


# --------------------------------------------------------------------------- #
# Network identity and BGP (RIPEstat)
# --------------------------------------------------------------------------- #


@mcp.tool(annotations=READ_ONLY)
async def lookup_network(resource: str) -> dict[str, Any]:
    """Identify who operates an IP address, prefix, or ASN (e.g. '8.8.8.8', '1.1.1.0/24', 'AS13335').

    Returns the origin ASN(s), the covering prefix, the AS holder name, and whether it is announced in BGP.
    Use this first to turn a customer IP or a vendor name's ASN into something you can investigate.
    """
    resource = resource.strip()
    result: dict[str, Any] = {"query": resource}
    if resource.upper().removeprefix("AS").isdigit():
        asns = [_asn(resource)]
    else:
        info = await ripestat("network-info", resource=resource)
        asns = [str(a) for a in info.get("asns", [])]
        result["prefix"] = info.get("prefix")
        if not asns:
            result["note"] = "No origin ASN found: this address may not be announced in BGP right now."
            return result
    result["asns"] = []
    for asn in asns[:3]:
        overview = await ripestat("as-overview", resource=f"AS{asn}")
        result["asns"].append(
            {"asn": f"AS{asn}", "holder": overview.get("holder"), "announced": overview.get("announced")}
        )
    return result


@mcp.tool(annotations=READ_ONLY)
async def bgp_routing_status(resource: str) -> dict[str, Any]:
    """Current BGP routing status for an ASN or prefix: how widely it is seen, and what it announces.

    'Visibility' is the share of RIPE RIS peers that currently see a route. Low or zero visibility for a
    network that is normally announced means it is (partly) unreachable from the internet.
    This reflects *now* only; use bgp_update_activity or bgp_routing_history for past windows.
    """
    data = await ripestat("routing-status", resource=resource.strip())
    return _trim(data)


@mcp.tool(annotations=READ_ONLY)
async def bgp_update_activity(resource: str, start: str, end: str) -> dict[str, Any]:
    """BGP update volume over time for an ASN or prefix in [start, end] (UTC; ISO-8601 or 'now-6h').

    A sudden burst of updates followed by silence is the classic signature of routes being withdrawn
    (a network removing itself from the internet). Steady low numbers are normal background churn.
    """
    t0, t1 = _window(start, end, max_days=7)
    data = await ripestat(
        "bgp-update-activity", resource=resource.strip(), starttime=_ripe_time(t0), endtime=_ripe_time(t1)
    )
    updates = data.get("updates") or []
    rows = [
        {
            "at": u.get("starttime"),
            "announcements": u.get("announcements"),
            "withdrawals": u.get("withdrawals"),
        }
        for u in updates
    ]
    counts = [(r["announcements"] or 0) + (r["withdrawals"] or 0) for r in rows]
    summary: dict[str, Any] = {
        "resource": resource,
        "window": [iso(t0), iso(t1)],
        "bucket": data.get("sampling_period_human"),
        "buckets": rows,
    }
    if counts:
        median = statistics.median(counts)
        peak = max(range(len(counts)), key=counts.__getitem__)
        summary["median_updates_per_bucket"] = median
        summary["peak"] = {"at": rows[peak]["at"], "updates": counts[peak]}
        summary["peak_vs_median"] = round(counts[peak] / median, 1) if median else None
    return summary


@mcp.tool(annotations=READ_ONLY)
async def bgp_routing_history(resource: str, start: str, end: str) -> dict[str, Any]:
    """Which origin ASNs announced a prefix (or an ASN's prefixes) during [start, end], and for how long.

    Works best on a single prefix. Gaps in a prefix's timeline mean it was withdrawn; a new, unexpected
    origin ASN for the same prefix can mean a hijack or misconfiguration.
    """
    t0, t1 = _window(start, end, max_days=14)
    data = await ripestat(
        "routing-history", resource=resource.strip(), starttime=_ripe_time(t0), endtime=_ripe_time(t1)
    )
    return {"resource": resource, "window": [iso(t0), iso(t1)], "history": _trim(data.get("by_origin", data), 8)}


# --------------------------------------------------------------------------- #
# Outage detection (IODA)
# --------------------------------------------------------------------------- #

EntityType = Literal["country", "asn", "region"]


def _entity_code(entity_type: str, code: str) -> str:
    return _asn(code) if entity_type == "asn" else code.strip().upper() if entity_type == "country" else code.strip()


@mcp.tool(annotations=READ_ONLY)
async def ioda_outage_events(entity_type: EntityType, entity_code: str, start: str, end: str) -> dict[str, Any]:
    """Outage events IODA detected for a country (ISO code, e.g. 'ES'), ASN (e.g. 'AS4804') or region id.

    Each event names the signal that tripped: 'bgp' (routes disappeared), 'ping-slash24' (hosts stopped
    answering active probes), or 'merit-nt' (darknet telescope traffic dropped). Agreement across signals
    is strong evidence of a real connectivity outage.
    """
    t0, t1 = _window(start, end)
    code = _entity_code(entity_type, entity_code)
    data = await ioda(
        "/outages/events",
        entityType=entity_type,
        entityCode=code,
        **{"from": int(t0.timestamp()), "until": int(t1.timestamp())},
    )
    events = sorted(data or [], key=lambda e: e.get("start", 0))
    return {
        "entity": f"{entity_type}/{code}",
        "window": [iso(t0), iso(t1)],
        "event_count": len(events),
        "events": [
            {
                "signal": e.get("datasource"),
                "start": iso_ts(e["start"]),
                "end": iso_ts(e["start"] + (e.get("duration") or 0)),
                "duration_min": round((e.get("duration") or 0) / 60),
                "score": round(e.get("score") or 0),
                "name": e.get("location_name"),
            }
            for e in events[:25]
        ],
        "note": None if events else "No outage events detected in this window.",
    }


def _summarize_series(series: dict[str, Any], buckets: int = 24) -> dict[str, Any]:
    step, t0 = series.get("step") or 300, series.get("from") or 0
    points = [(t0 + i * step, v) for i, v in enumerate(series.get("values") or []) if v is not None]
    out: dict[str, Any] = {"signal": series.get("datasource"), "step_seconds": step}
    if len(points) < 3:
        out["note"] = "Not enough data points in this window."
        return out
    values = [v for _, v in points]
    # Baseline = the first quarter of the window, so pick windows that start before the trouble.
    baseline = statistics.median(values[: max(3, len(values) // 4)]) or statistics.median(values) or 1
    low_t, low_v = min(points, key=lambda p: p[1])
    last_t, last_v = points[-1]
    out.update(
        baseline=round(baseline, 1),
        lowest={"at": iso_ts(low_t), "value": low_v, "pct_of_baseline": round(100 * low_v / baseline)},
        latest={"at": iso_ts(last_t), "value": last_v, "pct_of_baseline": round(100 * last_v / baseline)},
    )
    size = max(1, len(points) // buckets)
    out["timeline_pct_of_baseline"] = [
        {"at": iso_ts(chunk[0][0]), "pct": round(100 * statistics.mean(v for _, v in chunk) / baseline)}
        for chunk in (points[i : i + size] for i in range(0, len(points), size))
    ]
    return out


@mcp.tool(annotations=READ_ONLY)
async def ioda_signal_summary(
    entity_type: EntityType,
    entity_code: str,
    start: str,
    end: str,
    signal: Literal["all", "bgp", "ping-slash24", "merit-nt"] = "all",
) -> dict[str, Any]:
    """How connectivity signals for a country or ASN moved during [start, end], relative to a baseline.

    The baseline is the median of the first quarter of the window, so start the window a few hours BEFORE
    the suspected incident. A signal falling to well under 100% of baseline (say <80%) means a real drop;
    small wiggles are normal. Returns lowest point, latest value, and a ~24-point timeline per signal.
    """
    t0, t1 = _window(start, end, max_days=7)
    code = _entity_code(entity_type, entity_code)
    data = await ioda(
        f"/signals/raw/{entity_type}/{code}",
        datasource=None if signal == "all" else signal,
        **{"from": int(t0.timestamp()), "until": int(t1.timestamp())},
    )
    series = [s for group in (data or []) for s in (group or [])]
    if not series:
        return {"entity": f"{entity_type}/{code}", "note": "IODA has no signal data for this entity and window."}
    return {
        "entity": f"{entity_type}/{code}",
        "name": series[0].get("entityName"),
        "window": [iso(t0), iso(t1)],
        "signals": [_summarize_series(s) for s in series],
    }


@mcp.tool(annotations=READ_ONLY)
async def ioda_top_outages(
    start: str,
    end: str,
    entity_type: EntityType = "country",
    within_country: str | None = None,
    limit: int = 10,
) -> dict[str, Any]:
    """The most severe outages IODA saw during [start, end], ranked by severity score.

    Use entity_type='asn' with within_country='AU' to find which ISP in a country is having trouble.
    Scores are relative; compare them to each other, not to a fixed threshold.
    """
    t0, t1 = _window(start, end)
    data = await ioda(
        "/outages/summary",
        entityType=entity_type,
        relatedTo=f"country/{within_country.strip().upper()}" if within_country else None,
        limit=max(1, min(limit, 25)),
        **{"from": int(t0.timestamp()), "until": int(t1.timestamp())},
    )
    rows = []
    for item in data or []:
        entity, scores = item.get("entity") or {}, item.get("scores") or {}
        rows.append(
            {
                "entity": f"{entity.get('type')}/{entity.get('code')}",
                "name": entity.get("name"),
                "org": (entity.get("attrs") or {}).get("org"),
                "events": item.get("event_cnt"),
                "signals": sorted(k.split(".")[0] for k in scores if k != "overall"),
                "score": round(scores.get("overall") or 0),
            }
        )
    return {"window": [iso(t0), iso(t1)], "scope": within_country or "global", "outages": rows}


# --------------------------------------------------------------------------- #
# SaaS status pages (Atlassian Statuspage JSON API)
# --------------------------------------------------------------------------- #


def _status_host(service: str) -> str:
    key = service.strip().lower()
    if key in STATUS_PAGES:
        return STATUS_PAGES[key]
    host = urlparse(key if "://" in key else f"https://{key}").hostname
    if not host or "." not in host:
        raise ToolError(f"Unknown service {service!r}. Known: {', '.join(STATUS_PAGES)}; or pass a status page domain.")
    return host


@mcp.tool(annotations=READ_ONLY)
async def service_status(service: str) -> dict[str, Any]:
    """Live status of a SaaS vendor from its public status page.

    `service` is a known name (github, cloudflare, atlassian, discord, reddit, twilio, datadog, dropbox) or
    any Statuspage-hosted domain like 'status.example.com'. Returns the overall indicator, components that
    are not operational, and unresolved incidents with their latest update.
    """
    host = _status_host(service)
    body = await get_json(f"https://{host}/api/v2/summary.json")
    degraded = [
        {"component": c.get("name"), "status": c.get("status")}
        for c in body.get("components", [])
        if c.get("status") != "operational" and not c.get("group")
    ]
    incidents = [
        {
            "name": i.get("name"),
            "status": i.get("status"),
            "impact": i.get("impact"),
            "started": i.get("started_at") or i.get("created_at"),
            "latest_update": ((i.get("incident_updates") or [{}])[0].get("body") or "")[:400],
            "link": i.get("shortlink"),
        }
        for i in body.get("incidents", [])
    ]
    status = body.get("status") or {}
    return {
        "service": service,
        "page": host,
        "indicator": status.get("indicator"),
        "description": status.get("description"),
        "degraded_components": degraded[:20],
        "open_incidents": incidents,
    }


@mcp.tool(annotations=READ_ONLY)
async def service_incident_history(service: str, start: str, end: str) -> dict[str, Any]:
    """Incidents a SaaS vendor posted on its status page that started within [start, end].

    Statuspage only publishes roughly the 50 most recent incidents, so this cannot reach far into the
    past for busy vendors. An empty result for an old window means 'unknown', not 'no incident'.
    """
    t0, t1 = _window(start, end, max_days=90)
    host = _status_host(service)
    body = await get_json(f"https://{host}/api/v2/incidents.json")
    all_incidents = body.get("incidents", [])
    oldest = min((i.get("created_at") for i in all_incidents if i.get("created_at")), default=None)
    matches = []
    for i in all_incidents:
        created = i.get("started_at") or i.get("created_at")
        if not created:
            continue
        if t0 <= parse_time(created) <= t1:
            matches.append(
                {
                    "name": i.get("name"),
                    "impact": i.get("impact"),
                    "started": created,
                    "resolved": i.get("resolved_at"),
                    "components": [c.get("name") for c in i.get("components", [])][:10],
                    "link": i.get("shortlink"),
                }
            )
    return {
        "service": service,
        "window": [iso(t0), iso(t1)],
        "oldest_incident_available": oldest,
        "incidents": matches,
    }


# --------------------------------------------------------------------------- #
# A prompt and a resource, so MCP clients like Claude Desktop get more than tools
# --------------------------------------------------------------------------- #


@mcp.prompt(title="Triage an incident")
def triage(symptom: str, location: str = "", started: str = "") -> str:
    """Start an 'is it us or upstream?' investigation."""
    return (
        f"Help me triage an incident. Symptom: {symptom}\n"
        + (f"Where: {location}\n" if location else "")
        + (f"Started around (UTC): {started}\n" if started else "")
        + "Work outward from the affected network to the country to SaaS vendors, using the internet-signals "
        "tools. Finish with: verdict, confidence, the evidence for it, what would change your mind, next steps."
    )


@mcp.resource("upstream://scenarios", name="replay-scenarios", mime_type="application/json")
def scenarios() -> str:
    """Historical incidents you can replay with the agent (answer keys withheld)."""
    return json.dumps(as_public_dicts(), indent=2)


# --------------------------------------------------------------------------- #
# Plain HTTP routes for hosted deployments (Render health checks, a landing page)
# --------------------------------------------------------------------------- #

LANDING = """\
internet-signals MCP server (part of upstream-triage)

This is a remote MCP server, not a website. To use it:
  * Claude (web/desktop): Settings > Connectors > Add custom connector,
    and paste this server's URL ending in /mcp
  * Claude Code: claude mcp add --transport http internet-signals <this-url>/mcp

Tools: lookup_network, bgp_routing_status, bgp_update_activity, bgp_routing_history,
ioda_outage_events, ioda_signal_summary, ioda_top_outages, service_status,
service_incident_history. Data: RIPEstat, IODA (Georgia Tech), public status pages.
"""


@mcp.custom_route("/health", methods=["GET"], include_in_schema=False)
async def health(_request):
    from starlette.responses import JSONResponse

    return JSONResponse({"status": "ok", "version": __version__})


@mcp.custom_route("/", methods=["GET"], include_in_schema=False)
async def landing(_request):
    from starlette.responses import PlainTextResponse

    return PlainTextResponse(LANDING)


# --------------------------------------------------------------------------- #


def main() -> None:
    parser = argparse.ArgumentParser(description="internet-signals MCP server")
    parser.add_argument("--http", action="store_true", help="serve Streamable HTTP instead of stdio")
    parser.add_argument("--host", default=os.environ.get("HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8000")))
    args = parser.parse_args()

    telemetry.setup("internet-signals-mcp")
    if args.http:
        # Stateless: every request stands alone, which suits free-tier hosting that sleeps and scales.
        mcp.run("streamable-http", host=args.host, port=args.port, stateless_http=True)
    else:
        mcp.run()


if __name__ == "__main__":
    main()
