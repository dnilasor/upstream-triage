"""Small helpers for the time windows every tool takes."""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

UTC = timezone.utc
_RELATIVE = re.compile(r"^now(?:\s*-\s*(\d+)\s*([mhd]))?$")
_UNITS = {"m": "minutes", "h": "hours", "d": "days"}


def parse_time(value: str) -> datetime:
    """Parse 'now', 'now-6h', 'now-2d', a unix timestamp, or ISO-8601 (UTC assumed)."""
    raw = str(value).strip()
    match = _RELATIVE.match(raw.lower())
    if match:
        now = datetime.now(UTC)
        if match.group(1):
            now -= timedelta(**{_UNITS[match.group(2)]: int(match.group(1))})
        return now
    if raw.isdigit():
        return datetime.fromtimestamp(int(raw), tz=UTC)
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00").replace("z", "+00:00"))
    except ValueError as exc:
        raise ValueError(
            f"Can't parse time {value!r}. Use ISO-8601 like 2025-04-28T10:00Z, a unix timestamp, or now-6h."
        ) from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def window(start: str, end: str, max_days: int = 14) -> tuple[datetime, datetime]:
    """Parse and sanity-check a [start, end] window."""
    t0, t1 = parse_time(start), parse_time(end)
    if t1 <= t0:
        raise ValueError(f"end ({iso(t1)}) must be after start ({iso(t0)}).")
    if t1 - t0 > timedelta(days=max_days):
        raise ValueError(f"Window is longer than {max_days} days; narrow it down.")
    return t0, t1


def iso(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%MZ")


def iso_ts(ts: float | int) -> str:
    return iso(datetime.fromtimestamp(ts, tz=UTC))
