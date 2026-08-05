"""Check-in routing: the awaiting flag must expire so a next-day question isn't
swallowed as a check-in note, and plain questions must never be captured even
inside the window (the bug: every Telegram question answered 'Logged your
check-in.'). Covers checkin.awaiting_active / clear_awaiting / looks_like_question.
"""
from datetime import timedelta

from sqlalchemy import select

from app.coach import checkin
from app.models import Preference
from app.util import utcnow


def _backdate(db, minutes: int) -> None:
    """Age the awaiting flag by `minutes` to simulate an older prompt."""
    pref = db.scalar(select(Preference).where(Preference.key == checkin._AWAITING_KEY))
    pref.updated_at = utcnow() - timedelta(minutes=minutes)
    db.commit()


def test_awaiting_inactive_when_never_set(db):
    assert checkin.awaiting_active(db) is False


def test_awaiting_active_within_window(db):
    checkin.set_awaiting(db)
    assert checkin.awaiting_active(db) is True
    # non-destructive while active — a second check still sees it
    assert checkin.awaiting_active(db) is True


def test_awaiting_expires_and_self_clears_when_stale(db):
    checkin.set_awaiting(db)
    _backdate(db, int(checkin.REPLY_WINDOW.total_seconds() // 60) + 5)
    assert checkin.awaiting_active(db) is False
    # the stale flag was removed, not just reported inactive
    assert db.scalar(select(Preference).where(Preference.key == checkin._AWAITING_KEY)) is None


def test_awaiting_active_at_window_edge(db):
    checkin.set_awaiting(db)
    _backdate(db, int(checkin.REPLY_WINDOW.total_seconds() // 60) - 1)
    assert checkin.awaiting_active(db) is True


def test_clear_awaiting_is_idempotent(db):
    checkin.clear_awaiting(db)  # nothing set — must not raise
    checkin.set_awaiting(db)
    checkin.clear_awaiting(db)
    assert checkin.awaiting_active(db) is False


def test_set_awaiting_refreshes_window(db):
    checkin.set_awaiting(db)
    _backdate(db, int(checkin.REPLY_WINDOW.total_seconds() // 60) + 5)  # now stale
    checkin.set_awaiting(db)  # re-stamp (e.g. a quick tap)
    assert checkin.awaiting_active(db) is True


def test_looks_like_question_trailing_qmark(db):
    assert checkin.looks_like_question("what is my actual zone 2?") is True
    assert checkin.looks_like_question("legs feel great today, should I push?") is True


def test_looks_like_question_interrogative_opener_without_qmark(db):
    assert checkin.looks_like_question("how do we determine my zones") is True
    assert checkin.looks_like_question("Why is my watch showing a different one") is True
    assert checkin.looks_like_question("should I run tomorrow") is True


def test_upsert_recovers_from_unique_race(db, monkeypatch):
    """A feel tap + a typed debrief line can race on the unique(date) row; if a
    concurrent insert wins, _upsert re-selects and merges instead of crashing."""
    from datetime import date

    from sqlalchemy.exc import IntegrityError

    from app.coach import checkin
    real_commit = db.commit
    state = {"raised": False}

    def flaky_commit():
        if not state["raised"]:
            state["raised"] = True
            raise IntegrityError("insert", {}, Exception("duplicate date"))
        return real_commit()

    monkeypatch.setattr(db, "commit", flaky_commit)
    ci = checkin._upsert(db, date(2026, 7, 19), energy=4, soreness=2)
    assert ci.energy == 4 and ci.soreness == 2  # recovered on the retry


def test_checkin_notes_are_not_questions(db):
    # These must route to the check-in capture, not the coach.
    assert checkin.looks_like_question("legs heavy, slept badly, knee a bit cranky") is False
    assert checkin.looks_like_question("can't sleep, wired") is False
    assert checkin.looks_like_question("felt fresh on the run") is False
    assert checkin.looks_like_question("") is False
    assert checkin.looks_like_question("   ") is False
