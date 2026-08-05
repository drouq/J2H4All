"""Check-in building blocks: the feel-score model (`Checkin`), the one-tap
presets (`QUICK` → `record_quick`), the conflict-safe `_upsert`, and the shared
windowed-await machinery (`set_awaiting` / `awaiting_active` / `clear_awaiting` /
`looks_like_question`, keyed so other prompts reuse it). The live prompt is the merged
21:00 daily debrief (`coach/debrief.py`), which composes these.
"""

import re
from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession

from ..models import Checkin
from ..util import as_utc as _as_utc
from ..util import utcnow as _utcnow

# How long after a check-in prompt a free-text reply is still read as the check-in
# note. Past this, the flag is stale (e.g. the evening prompt went unanswered and
# they're messaging the next morning) and the message is a coaching question instead.
REPLY_WINDOW = timedelta(minutes=30)

# Interrogatives that mark a message as a question even without a trailing '?'.
# Deliberately conservative — auxiliaries like "can"/"is"/"do" are omitted because
# check-in notes open with them ("can't sleep", "is what it is") and we must not
# swallow a genuine note. A note virtually never opens with one of these.
_QUESTION_OPENERS = {
    "what", "how", "why", "when", "where", "which", "who", "whose", "should",
    "could", "would",
}

# One-tap presets → rough 1..5 scores. Free text can refine afterward.
QUICK = {
    "fresh": {"label": "😀 Fresh", "energy": 5, "soreness": 1, "motivation": 5, "life_stress": 2},
    "good":  {"label": "🙂 Good",  "energy": 4, "soreness": 2, "motivation": 4, "life_stress": 2},
    "meh":   {"label": "😐 Meh",   "energy": 3, "soreness": 3, "motivation": 3, "life_stress": 3},
    "tired": {"label": "😣 Sore/tired", "energy": 2, "soreness": 4, "motivation": 2, "life_stress": 4},
}

_AWAITING_KEY = "awaiting_checkin_reply"


def set_awaiting(db: DbSession, key: str = _AWAITING_KEY) -> None:
    """Mark that a prompt was just sent (or answered by a tap), so a free-text reply
    within REPLY_WINDOW is captured for it rather than treated as a coaching question.
    Re-stamps the timestamp, refreshing the window. `key` lets other daily prompts
    (e.g. the lifestyle log) reuse the same window machinery with their own flag."""
    from ..models import Preference
    pref = db.scalar(select(Preference).where(Preference.key == key))
    if pref is None:
        db.add(Preference(key=key, value="1", updated_at=_utcnow()))
    else:
        pref.value = "1"
        pref.updated_at = _utcnow()
    db.commit()


def awaiting_active(db: DbSession, key: str = _AWAITING_KEY, window=None) -> bool:
    """True iff a prompt is awaiting a reply AND it's still within its window
    (`REPLY_WINDOW` unless the caller passes a longer one — the off-plan question
    rides a sync rather than a beat, so it may sit unseen for hours). A stale flag
    (prompt went unanswered overnight) is cleared and reported inactive, so the
    next-morning question isn't swallowed. Non-destructive when active — the caller
    decides whether to consume it (a question leaves the flag armed so a later note
    still lands)."""
    from ..models import Preference
    pref = db.scalar(select(Preference).where(Preference.key == key))
    if pref is None:
        return False
    stamped = _as_utc(pref.updated_at)  # SQLite hands back naive; the store is UTC
    if _utcnow() - stamped > (window or REPLY_WINDOW):
        db.delete(pref)
        db.commit()
        return False
    return True


def clear_awaiting(db: DbSession, key: str = _AWAITING_KEY) -> None:
    """Drop the awaiting flag (a reply was captured, or we're done with it)."""
    from ..models import Preference
    pref = db.scalar(select(Preference).where(Preference.key == key))
    if pref is not None:
        db.delete(pref)
        db.commit()


def looks_like_question(text: str) -> bool:
    """A coaching question, not a check-in note: ends with '?' or opens with an
    interrogative. Used to route past the awaiting-check-in capture so questions
    are always answered, even in the reply window."""
    t = (text or "").strip()
    if not t:
        return False
    if t.endswith("?"):
        return True
    first = re.split(r"[^a-z]+", t.lower(), maxsplit=1)[0]
    return first in _QUESTION_OPENERS


def _upsert(db: DbSession, today: date, **fields) -> Checkin:
    """Merge `fields` (skipping None) onto today's row. Conflict-safe: a feel tap and
    a typed debrief line can hit this in two threads at once, and `Checkin.date` is
    unique — if a concurrent insert wins the race, re-select and merge onto it rather
    than crashing (which would drop the day's log)."""
    from sqlalchemy.exc import IntegrityError

    def _apply() -> Checkin:
        ci = db.scalar(select(Checkin).where(Checkin.date == today))
        if ci is None:
            ci = Checkin(date=today, created_at=_utcnow())
            db.add(ci)
        for k, v in fields.items():
            if v is not None:
                setattr(ci, k, v)
        db.commit()
        return ci
    try:
        return _apply()
    except IntegrityError:
        db.rollback()  # someone inserted today's row between our select and insert
        return _apply()


def record_quick(db: DbSession, option: str, today: date | None = None) -> Checkin | None:
    if today is None:
        from .schedule import local_today
        today = local_today(db)  # a 23:30 SGT check-in belongs to HIS day, not UTC's
    preset = QUICK.get(option)
    if not preset:
        return None
    raw = {"quick": option}
    return _upsert(db, today, energy=preset["energy"], soreness=preset["soreness"],
                   motivation=preset["motivation"], life_stress=preset["life_stress"], raw=raw)


def today_row(db: DbSession, today: date) -> Checkin | None:
    return db.scalar(select(Checkin).where(Checkin.date == today))


def quick_key_for(ci: Checkin | None) -> str | None:
    """Which feel preset was tapped today, if any — drives the ✓ when the debrief
    card is re-rendered after a tap."""
    return (ci.raw or {}).get("quick") if ci is not None else None
