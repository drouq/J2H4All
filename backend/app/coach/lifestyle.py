"""Lifestyle-log store (PRD §12 manual-flags fallback for Garmin's blocked Lifestyle
Logging). The `LifestyleLog` write helper (`_upsert`) used by the merged 21:00 daily
debrief (`coach/debrief.py`), which parses the life factors Garmin can't see — alcohol,
illness, sleep disruptors, nutrition, extra workouts, stress — into its `data` flags,
plus the one-tap `TAPS` presets for the same fields (see below for why taps exist).
Surfaced to the coach via `signals.recent_lifestyle`.
"""

from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession

from ..models import LifestyleLog
from ..util import utcnow as _utcnow


# One-tap life flags for the debrief card. Garmin's native Lifestyle Logging is
# blocked (mobile-only, undocumented), and the typed fallback captured exactly ONE
# row in six weeks (found 2026-08-03): he answers the debrief with the feel emoji
# and almost never types a line, so the flags the coach most needs for recovery
# attribution — and the illness red-flag hook — were never arriving. A tap costs
# nothing and fills the same `data` fields the LLM parse would. The value says
# "(tapped)" so the coach reads it as a flag with no detail, not as his words.
TAPS = {
    "alcohol": {"label": "🍺 Drinks", "value": "drank today (tapped, no detail)"},
    "illness": {"label": "🤒 Run down", "value": "feeling run down or ill (tapped, no detail)"},
    "sleep": {"label": "😴 Bad sleep", "value": "sleep disrupted or short (tapped, no detail)"},
    "stress": {"label": "😰 Stress", "value": "stressful day (tapped, no detail)"},
}
CLEAR_TAP = "none"
CLEAR_SUMMARY = "Quiet day — nothing to flag."


def _upsert(db: DbSession, day: date, raw_text: str | None, data: dict | None,
            replace: bool = False, remove: list[str] | None = None) -> LifestyleLog:
    """Write today's row; conflict-safe on the unique `date` (see checkin._upsert).

    MERGES by default — a null field leaves what's already stored alone, so tapping
    🍺 and then typing "slept badly" keeps both, in either order. (Same skip-nulls
    contract as `checkin._upsert`.) `replace=True` wipes the flags first, for the
    "nothing to flag" tap, which has to be able to contradict an earlier tap.
    `remove` drops keys outright — a null can't, since nulls are what "leave it
    alone" means here — which is how a tap is un-tapped."""
    from sqlalchemy.exc import IntegrityError

    def _apply() -> LifestyleLog:
        row = db.scalar(select(LifestyleLog).where(LifestyleLog.date == day))
        if row is None:
            row = LifestyleLog(date=day, created_at=_utcnow())
            db.add(row)
        if raw_text is not None:
            row.raw_text = raw_text
        merged = {} if replace else dict(row.data or {})
        merged.update({k: v for k, v in (data or {}).items() if v is not None})
        for k in remove or []:
            merged.pop(k, None)
        row.data = merged
        row.updated_at = _utcnow()
        db.commit()
        return row
    try:
        return _apply()
    except IntegrityError:
        db.rollback()
        return _apply()


CLEAR_LABEL = "👌 Nothing to flag"


def record_tap(db: DbSession, key: str, day: date) -> tuple[str, bool] | None:
    """Toggle one life flag from a card tap. Returns (button label, now-on), or None
    if the key isn't one of ours.

    Flags are independent and multi-select — several can be on at once — so a tap
    TOGGLES rather than sets: the card shows a ✓, and a ✓ you can't clear is a
    mis-tap you can only undo by wiping the day. `CLEAR_TAP` is the one exclusive
    option: it wipes the flags, and setting any flag clears it back."""
    if key == CLEAR_TAP:
        _upsert(db, day, raw_text=None, data={"summary": CLEAR_SUMMARY}, replace=True)
        return CLEAR_LABEL, True
    tap = TAPS.get(key)
    if tap is None:
        return None
    row = db.scalar(select(LifestyleLog).where(LifestyleLog.date == day))
    data = (row.data if row is not None else None) or {}
    if data.get(key):
        _upsert(db, day, raw_text=None, data=None, remove=[key])
        return tap["label"], False
    # A real flag contradicts an earlier "nothing to flag".
    stale_clear = ["summary"] if data.get("summary") == CLEAR_SUMMARY else None
    _upsert(db, day, raw_text=None, data={key: tap["value"]}, remove=stale_clear)
    return tap["label"], True


def logged_flags(db: DbSession, day: date) -> set[str]:
    """Which flag keys already sit on today's row — drives the card's ✓ marks."""
    row = db.scalar(select(LifestyleLog).where(LifestyleLog.date == day))
    data = (row.data if row else None) or {}
    if data.get("summary") == CLEAR_SUMMARY and not any(data.get(k) for k in TAPS):
        return {CLEAR_TAP}
    return {k for k in TAPS if data.get(k)}
