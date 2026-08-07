"""Time helpers.

Every timestamp persisted or logged by pixelforge is timezone-aware UTC in
ISO-8601 form so that records sort lexicographically and compare across
instances.
"""

from __future__ import annotations

from datetime import datetime, timezone

__all__ = ["utc_now", "utc_now_iso", "to_iso"]


def utc_now() -> datetime:
    """Return the current time as a timezone-aware UTC datetime."""
    return datetime.now(timezone.utc)


def to_iso(moment: datetime) -> str:
    """Render a datetime as an ISO-8601 UTC string with a trailing ``Z``."""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def utc_now_iso() -> str:
    """Return the current UTC time as an ISO-8601 string."""
    return to_iso(utc_now())
