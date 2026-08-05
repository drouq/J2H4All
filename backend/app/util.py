"""Shared micro-helpers — the single home for constants/one-liners that had
drifted into per-module copies (RUN_TYPES existed in 4 files, _as_dt in 5,
_utcnow in 10). Modules alias these (`from .util import utcnow as _utcnow`)
so call sites stay unchanged."""

from datetime import UTC, date, datetime

# Garmin activity_type prefixes that count as running — used to filter
# activities in rollups, trends, and planned-vs-actual result linking.
# A new Garmin run subtype needs adding HERE only.
RUN_TYPES = ("running", "trail_running", "treadmill_running", "track_running", "ultra_run", "indoor_running")


def utcnow() -> datetime:
    return datetime.now(UTC)


def as_dt(d: date) -> datetime:
    """Midnight UTC for a date — for comparing a date against timestamptz columns."""
    return datetime(d.year, d.month, d.day, tzinfo=UTC)


def as_utc(dt: datetime | None) -> datetime | None:
    """Force a stored timestamp aware, assuming UTC when it isn't.

    `DateTime(timezone=True)` round-trips as AWARE on Postgres but NAIVE on SQLite,
    so arithmetic against `utcnow()` raises "can't subtract offset-naive and
    offset-aware" on one backend and not the other. Anything that subtracts a stored
    timestamp must go through this — the store is UTC end-to-end, so attaching UTC is
    always the correct reading. (`checkin.awaiting_active` grew its own inline copy of
    this; that's the drift this module exists to stop.)"""
    if dt is None:
        return None
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt
