"""Replay scenarios: real internet incidents, replayed as if they were happening now.

On a normal day the internet is boring, so a live demo usually finds nothing.
IODA and RIPEstat both keep history, which lets the agent investigate a past
incident "live": it gets a fake 'now' and a symptom report from a fictional
on-call engineer, and has to work it out from the data.

Each scenario's `expect` is what a good investigation should conclude. It is
NOT shown to the agent; `upstream eval` uses it to grade the agent's answer.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class Scenario:
    id: str
    title: str
    now: str  # ISO-8601 UTC: the moment the "page" comes in
    prompt: str  # what the on-call engineer types
    expect: str  # what a good diagnosis concludes (hidden from the agent)


SCENARIOS: dict[str, Scenario] = {
    s.id: s
    for s in [
        Scenario(
            id="iberian-blackout",
            title="Spain & Portugal power blackout (2025-04-28)",
            now="2025-04-28T13:00Z",
            prompt=(
                "Our Madrid and Lisbon offices both dropped off the VPN around 10:30 UTC, and customers in "
                "Spain are telling support our app is unreachable. Our cloud region is in Frankfurt and looks "
                "healthy. Is this us, our cloud provider, or something bigger?"
            ),
            expect=(
                "Country-wide connectivity loss in Spain and Portugal starting ~10:30 UTC, visible in IODA "
                "active probing and BGP signals across many networks at once. Not the company's app or cloud "
                "region. (It was a power-grid blackout across the Iberian peninsula.)"
            ),
        ),
        Scenario(
            id="facebook-bgp",
            title="Facebook / Meta withdraws itself from the internet (2021-10-04)",
            now="2021-10-04T16:45Z",
            prompt=(
                "Since roughly 15:40 UTC our 'Log in with Facebook' button fails and nobody here can load "
                "facebook.com, Instagram or WhatsApp. DNS lookups for facebook.com are timing out. Is our DNS "
                "broken or is this on Facebook's side? Facebook's network is AS32934."
            ),
            expect=(
                "Meta's own network (AS32934) shows a burst of BGP updates around 15:40 UTC consistent with "
                "its prefixes, including those of its authoritative DNS servers, being withdrawn. The problem "
                "is on Facebook's side, not the company's DNS."
            ),
        ),
        Scenario(
            id="optus-australia",
            title="Optus nationwide outage, Australia (2023-11-08)",
            now="2023-11-08T00:30Z",
            prompt=(
                "Support tickets from Australian customers spiked starting around 17:00 UTC yesterday. "
                "They're on both mobile and home broadband. Our Sydney region is green and other countries "
                "are fine. What's going on?"
            ),
            expect=(
                "A single large Australian ISP, AS4804 (Optus), lost connectivity from ~17:00 UTC: IODA shows "
                "outage events on both BGP and active probing for that ASN. Customers on that ISP can't reach "
                "anything; the company's service is fine."
            ),
        ),
        Scenario(
            id="not-the-network",
            title="'Is it a network attack?' — mass Windows crashes (2024-07-19)",
            now="2024-07-19T07:00Z",
            prompt=(
                "Starting around 04:10 UTC, Windows machines in our offices in the US, UK and Australia are "
                "crashing to blue screens, and our airport check-in kiosks are down. Leadership wants to know "
                "if this is a network outage or an attack on the internet. Is the internet having a problem?"
            ),
            expect=(
                "Internet-level signals for the US, UK and Australia look normal around 04:10 UTC: no "
                "country-level connectivity drop. The network is very likely innocent; look at the endpoints "
                "and recent software or agent updates instead. (It was a faulty CrowdStrike sensor update.)"
            ),
        ),
    ]
}


def as_public_dicts() -> list[dict]:
    """Scenarios without the answer key, for exposing over MCP."""
    return [{k: v for k, v in asdict(s).items() if not k.startswith("expect")} for s in SCENARIOS.values()]
