"""Five completion states and the question that goes with them.

planned 🏃 / done ✅ / partial ⚠️ / missed 🏃 / abandoned ❌ — measured off the
PRESCRIPTION (duration and/or distance), never an execution score. And the rule the
2026-08-01 long run exposed: a session >20% off plan is asked about, not diagnosed.

`missed` vs `abandoned` is the athlete's distinction (2026-08-05) and the tests below are
written to hold the line: a session they haven't done YET keeps its type icon and can
still become ✅ — only one past the grace window is crossed out.
"""
from datetime import date, timedelta

import pytest

from app.calendar import sync as cal_sync
from app.calendar.client import EventGone, build_event_body, event_emoji
from app.coach import completion, postrun, redflag, signals
from app.models import Session as PlanSession
from app.models import SessionResult
from app.util import utcnow

_TODAY = date.today()


def _session(db, d, type_="long_run", title="Long Run — 3h00 Z2", duration=180,
             distance=None, eid=None):
    s = PlanSession(date=d, type=type_, title=title, purpose="p", status="planned",
                    duration_min=duration, distance_km=distance, calendar_event_id=eid,
                    created_at=utcnow(), updated_at=utcnow())
    db.add(s)
    db.commit()
    return s


def _result(db, s, duration=None, distance=None, completed=True, **kw):
    r = SessionResult(session_id=s.id, activity_id=1, completed=completed,
                      actual_duration_min=duration, actual_distance_km=distance,
                      created_at=utcnow(), **kw)
    db.add(r)
    db.commit()
    return r


# ----------------------------------------------------------------- the classifier

def test_within_tolerance_is_done(db):
    s = _session(db, _TODAY - timedelta(days=1), duration=180)
    r = _result(db, s, duration=165)           # 8% under
    assert completion.classify(s, r, _TODAY) == completion.DONE


def test_more_than_20_percent_short_is_partial(db):
    """The real case: 3h00 planned, 2h01 run."""
    s = _session(db, _TODAY - timedelta(days=2), duration=180)
    r = _result(db, s, duration=121)
    assert completion.classify(s, r, _TODAY) == completion.PARTIAL
    d = completion.delta(s, r)
    assert d.metric == "duration"
    assert d.fraction == pytest.approx(-0.328, abs=0.01)
    assert d.gap == pytest.approx(-59.0)
    assert completion.delta_line(s, r) == "121 min against 180 min planned — 33% short of plan"


# --------------------------------------------- the absolute floor (MIN_GAP)

def test_a_big_percentage_on_a_short_session_is_not_off_plan(db):
    """Their easy runs ran 8-25% over plan all through July (50→59, 55→65, 55→69 min)
    because they run a loop, not a stopwatch. Percentage alone would have flagged the
    69-vs-55 as a deviation and asked them why — noise, on the day the feature landed."""
    s = _session(db, _TODAY - timedelta(days=1), title="Easy Aerobic — 55 min", duration=55)
    r = _result(db, s, duration=69)                    # +25%, but only 14 minutes
    assert completion.delta(s, r).fraction > completion.TOLERANCE   # still measured...
    assert completion.off_plan(s, r) is False                       # ...but not remarkable
    assert completion.classify(s, r, _TODAY) == completion.DONE


def test_the_same_percentage_on_a_long_session_is_off_plan(db):
    """Identical relative miss, real absolute cost — this is the one worth asking about."""
    s = _session(db, _TODAY - timedelta(days=1), duration=180)
    r = _result(db, s, duration=135)                   # -25%, and 45 minutes
    assert completion.off_plan(s, r) is True
    assert completion.classify(s, r, _TODAY) == completion.PARTIAL


def test_the_floor_applies_to_distance_too(db):
    s = _session(db, _TODAY - timedelta(days=1), duration=None, distance=6.5)
    near = _result(db, s, distance=8.0)                # +23%, 1.5 km — under the floor
    assert completion.classify(s, near, _TODAY) == completion.DONE


def test_a_deviation_must_clear_BOTH_bars(db):
    """A large absolute gap on a huge session is still proportionally normal — 20 min
    off a 5-hour run is execution, not a missed session."""
    s = _session(db, _TODAY - timedelta(days=1), duration=300)
    r = _result(db, s, duration=280)                   # -20 min but only -7%
    assert abs(r.actual_duration_min - s.duration_min) > completion.MIN_GAP["duration"]
    assert completion.off_plan(s, r) is False


def test_more_than_20_percent_long_is_also_partial(db):
    s = _session(db, _TODAY - timedelta(days=2), duration=60)
    r = _result(db, s, duration=95)
    assert completion.classify(s, r, _TODAY) == completion.PARTIAL
    assert "over plan" in completion.delta_line(s, r)


def test_the_worst_deviation_across_metrics_wins(db):
    s = _session(db, _TODAY - timedelta(days=1), duration=60, distance=12)
    r = _result(db, s, duration=58, distance=7)   # duration fine, distance 42% short
    assert completion.delta(s, r).metric == "distance"
    assert completion.classify(s, r, _TODAY) == completion.PARTIAL


def test_gym_duration_is_nominal_so_never_partial(db):
    """Their 45-min gym sessions log 64-81 min routinely — the watch timer includes
    rest between sets, so a delta there measures nothing."""
    s = _session(db, _TODAY - timedelta(days=1), type_="strength", title="Gym — Push",
                 duration=45)
    r = _result(db, s, duration=81)               # +80%
    assert completion.delta(s, r) is None
    assert completion.classify(s, r, _TODAY) == completion.DONE


def test_a_session_with_nothing_comparable_is_done(db):
    s = _session(db, _TODAY - timedelta(days=1), duration=None)
    r = _result(db, s, duration=55)
    assert completion.classify(s, r, _TODAY) == completion.DONE


def test_todays_unrun_session_is_still_planned(db):
    """The day hasn't closed — they run at 5-6pm and sometimes 21:00."""
    assert completion.classify(_session(db, _TODAY), None, _TODAY) == completion.PLANNED
    assert completion.classify(_session(db, _TODAY + timedelta(days=1)), None, _TODAY) == completion.PLANNED


def test_unrun_session_is_missed_once_the_day_closes(db):
    s = _session(db, _TODAY - timedelta(days=1))
    assert completion.classify(s, None, _TODAY) == completion.MISSED


def test_missed_stays_missed_right_up_to_the_grace_boundary(db):
    s = _session(db, _TODAY - timedelta(days=completion.ABANDONED_AFTER_DAYS))
    assert completion.classify(s, None, _TODAY) == completion.MISSED


def test_unrun_session_is_abandoned_once_past_the_grace_window(db):
    s = _session(db, _TODAY - timedelta(days=completion.ABANDONED_AFTER_DAYS + 1))
    assert completion.classify(s, None, _TODAY) == completion.ABANDONED


def test_a_missed_session_carries_no_glyph_so_it_keeps_its_type_icon(db):
    """The whole point of the split: only abandonment is a cross."""
    assert completion.MISSED not in completion.STATUS_EMOJI
    assert completion.STATUS_EMOJI[completion.ABANDONED] == "❌"


def test_a_missed_session_can_still_become_done(db):
    """They shifts a run by a day or two and a late run links by its watch workout id —
    so `missed` must never be a terminal state."""
    s = _session(db, _TODAY - timedelta(days=2), duration=60)
    assert completion.classify(s, None, _TODAY) == completion.MISSED
    r = _result(db, s, duration=58)
    assert completion.classify(s, r, _TODAY) == completion.DONE


# ----------------------------------------------------------------- calendar rendering

def test_event_titles_carry_the_status_glyph(db):
    s = _session(db, _TODAY - timedelta(days=1), duration=180)
    sd = {"date": s.date, "type": s.type, "title": s.title, "duration_min": 180}

    planned = build_event_body(sd)
    done = build_event_body(sd, result={"duration_min": 175}, status=completion.DONE)
    partial = build_event_body(sd, result={"duration_min": 121, "delta_line": "121 min against 180 min planned — 33% short of plan"},
                               status=completion.PARTIAL)
    missed = build_event_body(sd, status=completion.MISSED)
    abandoned = build_event_body(sd, status=completion.ABANDONED)

    assert planned["summary"].startswith(event_emoji("long_run"))
    assert done["summary"].startswith("✅")
    assert partial["summary"].startswith("⚠️")
    assert abandoned["summary"].startswith("❌")
    # A missed session renders EXACTLY like a planned one — same icon, no "not
    # completed" note. It can still be run.
    assert missed["summary"] == planned["summary"]
    assert "❌" not in missed["description"]
    assert "33% short of plan" in partial["description"]
    assert "not logged yet" in partial["description"]      # no reason captured yet
    assert "❌ Not completed." in abandoned["description"]


def test_partial_event_shows_his_stated_reason_when_known(db):
    sd = {"date": _TODAY, "type": "long_run", "title": "Long Run", "duration_min": 180}
    body = build_event_body(sd, status=completion.PARTIAL, result={
        "duration_min": 121, "delta_line": "121 min against 180 min planned — 33% short of plan",
        "deviation_reason": "Why: had to get back for the kids",
    })
    assert "had to get back for the kids" in body["description"]


class FakeCalendar:
    def __init__(self):
        self.events = {}
        self._n = 0

    def list_events(self, cal_id, time_min_date):
        return [{"id": i, "summary": e["summary"], "start_date": e["start_date"]}
                for i, e in self.events.items() if e["start_date"] >= time_min_date]

    def insert_event(self, cal_id, body):
        self._n += 1
        eid = f"new{self._n}"
        self.events[eid] = {"summary": body["summary"], "start_date": body["start"]["date"]}
        return eid

    def update_event(self, cal_id, eid, body):
        if eid not in self.events:
            raise EventGone(eid)
        self.events[eid] = {"summary": body["summary"], "start_date": body["start"]["date"]}

    def delete_event(self, cal_id, eid):
        self.events.pop(eid, None)


@pytest.fixture
def patched(monkeypatch):
    fake = FakeCalendar()
    monkeypatch.setattr(cal_sync.oauth, "access_token", lambda db: "tok")
    monkeypatch.setattr(cal_sync, "CalendarClient", lambda tok: fake)
    monkeypatch.setattr(cal_sync, "ensure_calendar", lambda db, client: "cal1")
    return fake


def test_reconcile_marks_done_partial_and_abandoned(db, patched):
    on_plan = _session(db, _TODAY - timedelta(days=3), title="Easy 60", duration=60, eid="E1")
    _result(db, on_plan, duration=62)
    off_plan = _session(db, _TODAY - timedelta(days=2), title="Long Run", duration=180, eid="E2")
    _result(db, off_plan, duration=121)
    never_done = _session(db, _TODAY - timedelta(days=12), title="Old Easy", duration=60, eid="E3")
    for eid, d in (("E1", on_plan.date), ("E2", off_plan.date), ("E3", never_done.date)):
        patched.events[eid] = {"summary": "🏃 x", "start_date": d.isoformat()}

    out = cal_sync.reconcile(db)

    assert patched.events["E1"]["summary"].startswith("✅")
    assert patched.events["E2"]["summary"].startswith("⚠️")
    assert patched.events["E3"]["summary"].startswith("❌")
    assert out["completed_marked"] == 2 and out["abandoned_marked"] == 1


def test_a_missed_session_keeps_its_planned_event(db, patched):
    """The rule the athlete asked for: yesterday's unrun session keeps its 🏃 and stays
    liveable. They logs late, shifts runs by a day, and a late run links by its watch
    workout id — so the grace window has to pass before anything is crossed out."""
    for days in (1, 4, completion.ABANDONED_AFTER_DAYS):
        s = _session(db, _TODAY - timedelta(days=days), title=f"Easy {days}",
                     duration=45, eid=f"E{days}")
        patched.events[f"E{days}"] = {"summary": f"{event_emoji('easy')} Easy {days}",
                                      "start_date": s.date.isoformat()}
    cal_sync.reconcile(db)
    for days in (1, 4, completion.ABANDONED_AFTER_DAYS):
        assert patched.events[f"E{days}"]["summary"].startswith(event_emoji("easy"))


def test_the_past_ghost_sweep_does_not_delete_an_abandoned_event(db, patched):
    s = _session(db, _TODAY - timedelta(days=12), title="Old Easy", duration=60, eid="E1")
    patched.events["E1"] = {"summary": "🏃 Old Easy", "start_date": s.date.isoformat()}
    cal_sync.reconcile(db)
    assert "E1" in patched.events and patched.events["E1"]["summary"].startswith("❌")


# ----------------------------------------------------------------- ask, don't assume

def _no_telegram(monkeypatch, sent):
    monkeypatch.setattr("app.telegram.send_message_sync",
                        lambda text, chat_id=None: sent.append(text))


def test_asks_once_about_an_off_plan_session(db, monkeypatch):
    sent = []
    _no_telegram(monkeypatch, sent)
    s = _session(db, _TODAY - timedelta(days=1), duration=180)
    r = _result(db, s, duration=121)

    asked = postrun.ask_about_deviations(db, _TODAY)
    assert asked is not None and r.deviation_asked_at is not None
    assert "121 min against 180 min planned" in sent[0]
    # No cause suggested as fact — it opens the options and asks.
    assert "What happened?" in sent[0]

    # A second sync the same day must not ask again.
    assert postrun.ask_about_deviations(db, _TODAY) is None
    assert len(sent) == 1


def test_does_not_ask_about_a_session_hit_on_plan(db, monkeypatch):
    sent = []
    _no_telegram(monkeypatch, sent)
    s = _session(db, _TODAY - timedelta(days=1), duration=180)
    _result(db, s, duration=170)
    assert postrun.ask_about_deviations(db, _TODAY) is None
    assert sent == []


def test_does_not_interrogate_about_old_sessions(db, monkeypatch):
    """A first deploy or a backfill must not fire off questions about history."""
    sent = []
    _no_telegram(monkeypatch, sent)
    s = _session(db, _TODAY - timedelta(days=postrun.ASK_WITHIN_DAYS + 1), duration=180)
    _result(db, s, duration=100)
    assert postrun.ask_about_deviations(db, _TODAY) is None
    assert sent == []


def test_his_reply_is_stored_as_the_reason(db, monkeypatch):
    sent = []
    _no_telegram(monkeypatch, sent)
    s = _session(db, _TODAY - timedelta(days=1), duration=180)
    r = _result(db, s, duration=121)
    postrun.ask_about_deviations(db, _TODAY)

    rid = postrun.pending_ask(db)
    assert rid == r.id
    postrun.record_reason(db, rid, "ran out of time, had to be back by 11")
    db.refresh(r)
    assert r.deviation_reason == "ran out of time, had to be back by 11"
    assert postrun.pending_ask(db) is None          # consumed


def test_the_weekly_review_sees_missed_and_abandoned_as_different_words(db):
    """The coach-facing half of the split. Conflating these is what let a run they
    simply hadn't done yet read the same as one that was gone."""
    recent = _session(db, _TODAY - timedelta(days=1), title="Yesterday's Easy")
    old = _session(db, _TODAY - timedelta(days=completion.ABANDONED_AFTER_DAYS + 1),
                   title="Long Gone")
    rows = {e["title"]: e["status"] for e in signals.recent_plan_execution(db, _TODAY, days=20)}
    assert rows[recent.title] == completion.MISSED
    assert rows[old.title] == completion.ABANDONED


def test_an_optional_run_is_never_either_of_them(db):
    """`skipped_optional` outranks both — skipping the optional 4th run is expected
    behaviour, not a miss and certainly not an abandonment."""
    s = _session(db, _TODAY - timedelta(days=1), title="[Optional] Easy Aerobic")
    row = next(e for e in signals.recent_plan_execution(db, _TODAY, days=7)
               if e["title"] == s.title)
    assert row["status"] == "skipped_optional"


def test_the_reason_reaches_the_weekly_review(db):
    s = _session(db, _TODAY - timedelta(days=2), duration=180)
    _result(db, s, duration=121, deviation_reason="logistics — kids pickup")
    row = next(e for e in signals.recent_plan_execution(db, _TODAY, days=7)
               if e["date"] == s.date.isoformat())
    assert row["status"] == "completed_off_plan"
    assert row["deviation_reason"] == "logistics — kids pickup"
    assert "33% short" in row["deviation"]


def test_unexplained_off_plan_run_does_not_raise_a_red_flag(db, monkeypatch):
    """The 2026-08-01 mistake: a logistical cutoff read as fatigue and turned into a
    proposal for an easier week."""
    monkeypatch.setattr(signals, "recovery_baseline", lambda db, t: {
        "hrv_recent_3d": None, "hrv_baseline_28d": None,
        "rhr_recent_3d": None, "rhr_baseline_28d": None})
    monkeypatch.setattr(signals, "deep_recovery", lambda db, t: {"latest": {}})
    s = _session(db, _TODAY - timedelta(days=1), duration=180)
    _result(db, s, duration=121, flagged=True, note="cut an hour short")

    assert redflag.detect(db, _TODAY) == []


def test_off_plan_reply_is_not_also_swallowed_by_the_debrief(db, monkeypatch):
    """The debrief's own pre-beat sync is what fires the off-plan question, so at 22:00
    both awaits are armed seconds apart. One message can't be both "why Saturday was
    short" and tonight's feel log — the specific question wins, and the coach still
    answers it (the debrief branch used to capture it AND return)."""
    from app import telegram as tg
    from app.coach import chat, checkin, debrief
    from app.models import Checkin

    sent = []
    _no_telegram(monkeypatch, sent)
    monkeypatch.setattr("app.db.SessionLocal", lambda: db)
    monkeypatch.setattr(tg, "send_message_sync", lambda text, chat_id=None: sent.append(text))
    monkeypatch.setattr(tg, "send_typing", lambda *a, **k: None)
    monkeypatch.setattr(tg, "send_proposal_card_sync", lambda *a, **k: None)
    monkeypatch.setattr(tg, "_offer_context_capture", lambda *a, **k: None)
    answered = []
    monkeypatch.setattr(chat, "ask_with_proposal",
                        lambda db, text, surface="telegram": (answered.append(text), ("Got it.", None))[1])

    s = _session(db, _TODAY - timedelta(days=2), duration=180)
    rid = _result(db, s, duration=121).id
    postrun.ask_about_deviations(db, _TODAY)          # arms the off-plan await
    checkin.set_awaiting(db, debrief.AWAITING_KEY)    # 22:00 card lands too

    tg._handle_free_text("ran out of time, had to be back for lunch")

    # The router closes its session, so re-query rather than refresh a detached row.
    assert db.get(SessionResult, rid).deviation_reason == "ran out of time, had to be back for lunch"
    # NOT parsed as tonight's debrief...
    assert db.query(Checkin).count() == 0
    # ...and the coach actually answered it, rather than replying "Logged — feel and
    # the details" and stopping.
    assert answered == ["ran out of time, had to be back for lunch"]
    assert not any("Logged — feel and the details" in m for m in sent)


def test_an_explained_off_plan_run_reaches_the_coach_with_the_reason(db, monkeypatch):
    monkeypatch.setattr(signals, "recovery_baseline", lambda db, t: {
        "hrv_recent_3d": None, "hrv_baseline_28d": None,
        "rhr_recent_3d": None, "rhr_baseline_28d": None})
    monkeypatch.setattr(signals, "deep_recovery", lambda db, t: {"latest": {}})
    s = _session(db, _TODAY - timedelta(days=1), duration=180)
    _result(db, s, duration=121, flagged=True, note="cut an hour short",
            deviation_reason="knee started niggling at 2h")

    reasons = redflag.detect(db, _TODAY)
    assert any("knee started niggling" in r for r in reasons)
