"""Pending-Edit flag windowing: a forgotten ✏️ Edit tap must not hijack an unrelated
free-text message (a debrief, a coaching question) hours later — the same 30-min
window the check-in/debrief awaiting flags use."""
from datetime import timedelta

from sqlalchemy import select

from app.coach import checkin, revise
from app.models import Preference
from app.util import utcnow


def _backdate(db, minutes: int):
    pref = db.scalar(select(Preference).where(Preference.key == revise._PENDING_KEY))
    pref.updated_at = utcnow() - timedelta(minutes=minutes)
    db.commit()


def test_pop_none_when_nothing_pending(db):
    assert revise.pop_pending_edit(db) is None


def test_pop_returns_id_within_window(db):
    revise.set_pending_edit(db, 42)
    assert revise.pop_pending_edit(db) == 42
    # consumed → cleared
    assert db.scalar(select(Preference).where(Preference.key == revise._PENDING_KEY)) is None


def test_pop_expires_stale_edit(db):
    revise.set_pending_edit(db, 42)
    _backdate(db, int(checkin.REPLY_WINDOW.total_seconds() // 60) + 5)
    assert revise.pop_pending_edit(db) is None   # stale → not returned
    # and the stale flag is cleared so it can't linger
    assert db.scalar(select(Preference).where(Preference.key == revise._PENDING_KEY)) is None


def test_pop_active_at_window_edge(db):
    revise.set_pending_edit(db, 7)
    _backdate(db, int(checkin.REPLY_WINDOW.total_seconds() // 60) - 1)
    assert revise.pop_pending_edit(db) == 7
