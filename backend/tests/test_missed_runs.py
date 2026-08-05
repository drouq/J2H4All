"""The missed-run notice (coach/missed.py) — the twin of the off-plan question.

The gap it closes, exactly: run 40% of a session and the coach asks; run 0% of it and
the coach said nothing. `ask_about_deviations` JOINs an existing SessionResult, so a
session with no activity could never reach it, and `build_brief` was only handed TODAY's
sessions so it had no view of yesterday either.

Every rule asserted here is the coach's own call (asked with full doctrine + live prod
state), not an implementation convenience — the exclusions especially. Changing one
should mean changing the doctrine, so these are written to fail loudly.
"""
from datetime import date, timedelta

import pytest

from app.coach import brief, missed
from app.models import Session as PlanSession
from app.models import SessionResult
from app.util import utcnow

_TODAY = date(2026, 8, 5)          # a Wednesday


def _session(db, d, type_="easy", title="Easy Aerobic — 40 min Z2", status="planned"):
    s = PlanSession(date=d, type=type_, title=title, purpose="p", status=status,
                    duration_min=40, target_zone="Z2",
                    created_at=utcnow(), updated_at=utcnow())
    db.add(s)
    db.commit()
    return s


def _ran(db, session):
    db.add(SessionResult(session_id=session.id, activity_id=1, completed=True,
                         actual_duration_min=40.0, created_at=utcnow()))
    db.commit()


# ------------------------------------------------------------------ what qualifies

def test_a_planned_run_with_no_activity_is_noticed(db):
    s = _session(db, _TODAY - timedelta(days=1))
    assert missed.outstanding_runs(db, _TODAY) == [s]


def test_a_run_that_happened_is_not_noticed(db):
    s = _session(db, _TODAY - timedelta(days=1))
    _ran(db, s)
    assert missed.outstanding_runs(db, _TODAY) == []


def test_todays_session_is_never_noticed(db):
    """The day isn't over — they run at 5-6pm, sometimes 21:00. Asking mid-day is the
    failure mode the whole design avoids."""
    _session(db, _TODAY)
    assert missed.outstanding_runs(db, _TODAY) == []


def test_a_future_session_is_never_noticed(db):
    _session(db, _TODAY + timedelta(days=2))
    assert missed.outstanding_runs(db, _TODAY) == []


def test_the_lookback_stays_inside_the_grace_window(db):
    """The coach only ever asks about a MISSED run — one that's still liveable and still
    carrying its 🏃. Push LOOKBACK_DAYS past ABANDONED_AFTER_DAYS and it starts asking
    about abandoned sessions, which is exactly the chasing the doctrine forbids."""
    from app.coach import completion
    assert missed.LOOKBACK_DAYS < completion.ABANDONED_AFTER_DAYS

    s = _session(db, _TODAY - timedelta(days=missed.LOOKBACK_DAYS))
    assert completion.classify(s, None, _TODAY) == completion.MISSED
    assert missed.outstanding_runs(db, _TODAY) == [s]


def test_nothing_older_than_the_lookback(db):
    """Beyond a week it isn't rescheduleable, it's archaeology — and this is the guard
    that stops a first deploy interrogating them about months of history."""
    _session(db, _TODAY - timedelta(days=missed.LOOKBACK_DAYS + 1))
    assert missed.outstanding_runs(db, _TODAY) == []


# ------------------------------------------------------------------ the hard exclusions

def test_optional_runs_never_trigger(db):
    """The optional 4th run exists so they can decline it WITHOUT a conversation. Asking
    converts an explicit permission into a soft obligation."""
    _session(db, _TODAY - timedelta(days=1), title="[Optional] Easy Aerobic — 30 min Z2")
    assert missed.outstanding_runs(db, _TODAY) == []


def test_the_optional_marker_is_matched_case_insensitively(db):
    _session(db, _TODAY - timedelta(days=1), title="Easy Aerobic — 30 min [OPTIONAL]")
    assert missed.outstanding_runs(db, _TODAY) == []


def test_gym_sessions_never_trigger(db):
    """A habit structure they set, not a stimulus being periodized — they move them around
    and nothing the coach would prescribe changes. Pinging would dilute the run signal."""
    _session(db, _TODAY - timedelta(days=1), type_="strength", title="Gym — Pull (upper)")
    assert missed.outstanding_runs(db, _TODAY) == []


def test_rest_days_never_trigger(db):
    _session(db, _TODAY - timedelta(days=1), type_="rest", title="Rest")
    assert missed.outstanding_runs(db, _TODAY) == []


def test_a_superseded_session_is_not_noticed(db):
    """A revised-away session was never their to miss."""
    _session(db, _TODAY - timedelta(days=1), status="superseded")
    assert missed.outstanding_runs(db, _TODAY) == []


# ------------------------------------------------------------------ raised at most once

def test_a_session_is_raised_once_and_never_again(db):
    """The rule that makes the feature safe: the maximum cost of a false positive is
    one sentence, one time."""
    s = _session(db, _TODAY - timedelta(days=1))
    text, sessions = missed.pending_notice(db, _TODAY)
    assert sessions == [s] and text
    missed.mark_raised(db, sessions)
    assert missed.pending_notice(db, _TODAY) is None
    assert missed.outstanding_runs(db, _TODAY) == []


def test_marking_is_the_callers_job_so_an_unsent_notice_is_not_burnt(db):
    """pending_notice must NOT mark on its own — if the send fails the session has to
    stay eligible for tomorrow."""
    _session(db, _TODAY - timedelta(days=1))
    missed.pending_notice(db, _TODAY)
    assert missed.pending_notice(db, _TODAY) is not None


# ------------------------------------------------------------------ the message

def test_the_notice_names_each_run_and_offers_to_rework(db):
    _session(db, _TODAY - timedelta(days=3), title="B2B Easy Run — 30 min Z2")
    _session(db, _TODAY - timedelta(days=1), title="Easy Aerobic — 40 min Z2")
    text, _ = missed.pending_notice(db, _TODAY)
    assert "2 runs sitting empty" in text
    assert "Sunday's B2B Easy Run — 30 min Z2" in text
    assert "Tuesday's Easy Aerobic — 40 min Z2" in text
    assert "needs shifting" in text          # door open to rescheduling
    assert "nothing to make up" in text      # don't-chase, stated


def test_the_notice_assumes_no_cause(db):
    """Notice without assuming: no fatigue, no excuse, no disappointment. The whole
    point — the coach cannot tell logistics from tired legs and must not guess."""
    _session(db, _TODAY - timedelta(days=1))
    text, _ = missed.pending_notice(db, _TODAY)
    lowered = text.lower()
    for word in ("tired", "fatigue", "recovery", "rest", "sorry", "should", "skipped",
                 "struggling", "why did"):
        assert word not in lowered, f"the notice implies a cause: {word!r}"


def test_the_notice_makes_no_claim_about_his_markers(db):
    """The coach's draft asserted markers were at baseline — true the morning they wrote
    it, but this text ships EVERY morning and must not assert an unchecked fact."""
    _session(db, _TODAY - timedelta(days=1))
    text, _ = missed.pending_notice(db, _TODAY)
    assert "baseline" not in text.lower()
    assert "marker" not in text.lower()


def test_one_run_reads_as_singular(db):
    _session(db, _TODAY - timedelta(days=1))
    text, _ = missed.pending_notice(db, _TODAY)
    assert text.startswith("One run sitting empty")


def test_a_crowded_week_asks_one_open_question_instead_of_a_roll_call(db):
    """More than NAME_LIMIT outstanding is a life problem, not a session problem — the
    right response is 'how's the week?', not a list of grievances."""
    for i in range(1, missed.NAME_LIMIT + 2):
        _session(db, _TODAY - timedelta(days=i), title=f"Run {i}")
    text, sessions = missed.pending_notice(db, _TODAY)
    assert len(sessions) == missed.NAME_LIMIT + 1
    assert "How's the week looking on your end?" in text
    assert "Run 1" not in text                       # no roll-call
    assert "sitting empty over the past" in text


def test_a_week_old_run_is_dated_not_named_by_weekday(db):
    """At exactly 7 days the weekday collides with today's and would read as a
    different session."""
    _session(db, _TODAY - timedelta(days=7), title="Long Run — 2h00 Z2")
    text, _ = missed.pending_notice(db, _TODAY)
    assert "Wednesday's" not in text                 # _TODAY is a Wednesday
    assert "29 Jul's" in text


# ------------------------------------------------------------------ brief integration

def test_the_notice_rides_on_the_brief_and_marks_only_after_sending(db, monkeypatch):
    sent = []
    monkeypatch.setattr(brief, "build_brief", lambda db, today=None: "Morning. Easy 40 today.")
    monkeypatch.setattr("app.telegram.send_message_sync", lambda text, **kw: sent.append(text))
    monkeypatch.setattr("app.coach.adapt.refresh_from_garmin", lambda db: None)
    s = _session(db, _TODAY - timedelta(days=1))

    out = brief.send_brief(db, _TODAY)
    assert len(sent) == 1                            # ONE message, not two pings
    assert sent[0].startswith("Morning. Easy 40 today.")
    assert "One run sitting empty" in sent[0]
    assert out == sent[0]
    db.refresh(s)
    assert s.missed_asked_at is not None


def test_the_notice_still_sends_when_the_brief_is_unavailable(db, monkeypatch):
    from app.llm import LLMNotConfigured

    sent = []

    def _boom(db, today=None):
        raise LLMNotConfigured("no key")

    monkeypatch.setattr(brief, "build_brief", _boom)
    monkeypatch.setattr("app.telegram.send_message_sync", lambda text, **kw: sent.append(text))
    monkeypatch.setattr("app.coach.adapt.refresh_from_garmin", lambda db: None)
    _session(db, _TODAY - timedelta(days=1))

    brief.send_brief(db, _TODAY)
    assert len(sent) == 1 and "One run sitting empty" in sent[0]


def test_a_quiet_morning_sends_nothing_extra(db, monkeypatch):
    sent = []
    monkeypatch.setattr(brief, "build_brief", lambda db, today=None: "Morning.")
    monkeypatch.setattr("app.telegram.send_message_sync", lambda text, **kw: sent.append(text))
    monkeypatch.setattr("app.coach.adapt.refresh_from_garmin", lambda db: None)
    brief.send_brief(db, _TODAY)
    assert sent == ["Morning."]


@pytest.mark.parametrize("nothing_at_all", [True])
def test_no_brief_and_no_notice_sends_nothing(db, monkeypatch, nothing_at_all):
    from app.llm import LLMNotConfigured

    sent = []

    def _boom(db, today=None):
        raise LLMNotConfigured("no key")

    monkeypatch.setattr(brief, "build_brief", _boom)
    monkeypatch.setattr("app.telegram.send_message_sync", lambda text, **kw: sent.append(text))
    monkeypatch.setattr("app.coach.adapt.refresh_from_garmin", lambda db: None)
    assert brief.send_brief(db, _TODAY) is None
    assert sent == []
