"""The morning brief must not announce a session he has already done.

The gap, exactly: a session is often run a DAY EARLY — the scheduled workout is
selected on the watch and done ahead of time, and `link_results` matches by `workoutId`
regardless of date, so today's session is already complete before the brief fires. The
brief read straight off the calendar square (`status == "planned" AND date == today`, with
no check for a result) and announced a session as "today's plan" the morning after it had
been run — while the previous evening's debrief had correctly acknowledged it.
The debrief follows the run; the brief followed the square.

These pin the FACTS handed to the prompt, not the prose: the prompt can be reworded, but
`status` must keep telling it the truth, and it must come from the shared classifier so the
brief, the calendar glyph and the web Plan panel cannot drift apart.
"""
from datetime import date, datetime, timedelta

from app.coach.brief import _session_facts
from app.models import Activity, SessionResult
from app.models import Session as PlanSession
from app.util import utcnow

_TODAY = date(2026, 9, 25)


def _session(db, d, *, title="Easy Aerobic — 75 min Z2", type_="easy", minutes=75):
    s = PlanSession(date=d, type=type_, title=title, purpose="aerobic base", status="planned",
                    duration_min=minutes, created_at=utcnow(), updated_at=utcnow())
    db.add(s)
    db.flush()
    db.commit()
    return s


def _ran(db, s, on_day, *, aid=8001, minutes=77.0, km=12.1, hr=136, read=None):
    start = datetime(on_day.year, on_day.month, on_day.day, 17, 30)
    db.add(Activity(id=aid, start_time_utc=utcnow(), start_time_local=start,
                    activity_type="running", name="run", duration_s=minutes * 60,
                    distance_m=km * 1000, avg_hr=hr, raw={}, synced_at=utcnow(),
                    detail_synced=False, streams_synced=False))
    db.add(SessionResult(session_id=s.id, activity_id=aid, completed=True,
                         actual_duration_min=minutes, actual_distance_km=km,
                         actual_avg_hr=hr, read_summary=read, created_at=utcnow()))
    db.commit()


def test_a_session_run_a_day_early_is_reported_as_done(db):
    """THE case: run yesterday, still dated today."""
    s = _session(db, _TODAY)
    _ran(db, s, _TODAY - timedelta(days=1), read="Clean Z2 throughout.")
    facts = _session_facts(db, s, _TODAY)
    assert facts["status"] == "done"
    assert facts["actually_done_on"] == (_TODAY - timedelta(days=1)).isoformat()
    assert facts["actual"]["duration_min"] == 77.0
    assert facts["actual"]["read"] == "Clean Z2 throughout."


def test_a_session_still_to_do_stays_planned(db):
    """Today's unrun session is PLANNED, not missed — the day hasn't closed."""
    s = _session(db, _TODAY)
    facts = _session_facts(db, s, _TODAY)
    assert facts["status"] == "planned"
    assert "actual" not in facts and "actually_done_on" not in facts


def test_a_session_done_today_needs_no_other_day_named(db):
    """Done on its own day: report it complete, but don't name the day in a brief
    written on that same day."""
    s = _session(db, _TODAY)
    _ran(db, s, _TODAY)
    facts = _session_facts(db, s, _TODAY)
    assert facts["status"] == "done"
    assert "actually_done_on" not in facts


def test_a_session_done_well_off_plan_reports_partial(db):
    """The status comes from the shared classifier, so the >20% AND >15 min off-plan rule
    applies here too — the brief shouldn't call a 30-min stand-in for a 75-min session
    'done' any more than the calendar does."""
    s = _session(db, _TODAY)
    _ran(db, s, _TODAY, minutes=30.0, km=4.6)
    assert _session_facts(db, s, _TODAY)["status"] == "partial"


def test_a_hand_marked_session_is_done_without_an_activity_date(db):
    """mark_session_done writes a result with no activity behind it — there is no day to
    name, and that must not crash the brief."""
    s = _session(db, _TODAY, type_="strength", title="Gym — Upper Body (Push)", minutes=45)
    db.add(SessionResult(session_id=s.id, activity_id=None, completed=True,
                         created_at=utcnow()))
    db.commit()
    facts = _session_facts(db, s, _TODAY)
    assert facts["status"] == "done"
    assert "actually_done_on" not in facts


def test_the_prompt_is_told_what_status_means(db):
    """A status the prompt never reads would be a silent no-op, which is exactly the class
    of bug this fixes."""
    from app.coach.brief import system_prompt
    text = system_prompt(db, _TODAY)
    assert "todays_sessions" in text and "status" in text
    assert "ALREADY" in text and "actually_done_on" in text
