"""apply_sessions: today-or-later clamp, drop-today-when-done, and the per-date
1:1 stable-id carry-over. These guard the paths that corrupt the live plan.
Plus link_results: match a completed run to the session it fulfilled."""
from datetime import UTC, date, datetime, timedelta

from app.models import Activity, Session, SessionResult
from app.plan import store
from app.util import utcnow as _utcnow


def _sess(d, type="easy", **kw):
    return {"date": d.isoformat(), "type": type, "title": kw.pop("title", type), "purpose": "p", **kw}


def _run(act_id, d, *, workout_id=None, dur_min=120.0, dist_km=19.0):
    start = datetime(d.year, d.month, d.day, 18, 0, tzinfo=UTC)
    return Activity(
        id=act_id, start_time_utc=start, start_time_local=start.replace(tzinfo=None),
        activity_type="running", name="run", duration_s=dur_min * 60, distance_m=dist_km * 1000,
        raw={"workoutId": int(workout_id)} if workout_id is not None else {},
        synced_at=_utcnow(), detail_synced=False, streams_synced=False,
    )


def _strength(act_id, d, *, atype="strength_training", dur_min=65.0):
    start = datetime(d.year, d.month, d.day, 18, 0, tzinfo=UTC)
    return Activity(
        id=act_id, start_time_utc=start, start_time_local=start.replace(tzinfo=None),
        activity_type=atype, name="Strength", duration_s=dur_min * 60, distance_m=None,
        raw={}, synced_at=_utcnow(), detail_synced=False, streams_synced=False,
    )


def test_norm_type_aliases_and_unknown():
    assert store._norm_type("easy_run") == "easy"
    assert store._norm_type("Rest Day") == "rest"
    assert store._norm_type("recovery-jog") == "recovery"
    assert store._norm_type("gym") == "strength"
    assert store._norm_type("mystery") == "mystery"  # kept as-is, logged


def test_apply_drops_past_keeps_today_and_future(db):
    today = date.today()
    n = store.apply_sessions(
        db,
        [_sess(today - timedelta(days=1)), _sess(today), _sess(today + timedelta(days=1))],
        macro_plan_id=None,
    )
    db.commit()
    assert n == 2  # yesterday dropped
    dates = {s.date for s in db.query(Session).all()}
    assert dates == {today, today + timedelta(days=1)}


def test_apply_drops_today_when_already_run(db):
    today = date.today()
    done = Session(date=today, type="easy", title="done", purpose="", status="planned",
                   created_at=_utcnow(), updated_at=_utcnow())
    db.add(done)
    db.flush()
    db.add(SessionResult(session_id=done.id, activity_id=None, completed=True, created_at=_utcnow()))
    db.commit()

    n = store.apply_sessions(db, [_sess(today), _sess(today + timedelta(days=1))], macro_plan_id=None)
    db.commit()
    assert n == 1  # today dropped because it already has a result
    new = {s.date for s in db.query(Session).filter(Session.status == "planned",
                                                    Session.title != "done").all()}
    assert new == {today + timedelta(days=1)}


def test_carryover_is_one_to_one_per_date(db):
    d = date.today() + timedelta(days=3)
    run_old = Session(date=d, type="easy", title="old run", purpose="", status="planned",
                      calendar_event_id="evt_run", garmin_workout_id="w1", garmin_schedule_id="s1",
                      created_at=_utcnow(), updated_at=_utcnow())
    gym_old = Session(date=d, type="strength", title="old gym", purpose="", status="planned",
                      calendar_event_id="evt_gym", created_at=_utcnow(), updated_at=_utcnow())
    db.add_all([run_old, gym_old])
    db.commit()

    store.apply_sessions(db, [_sess(d, "easy", title="new run"), _sess(d, "strength", title="new gym")],
                         macro_plan_id=None)
    db.commit()

    new = db.query(Session).filter(Session.status == "planned", Session.date == d).all()
    assert len(new) == 2
    events = [s.calendar_event_id for s in new]
    # Both non-rest sessions get a distinct event id — never shared.
    assert set(events) == {"evt_run", "evt_gym"}
    assert len(set(events)) == 2
    # Only the run (a PUSH type) claims the workout id; the gym does not.
    run = next(s for s in new if s.type == "easy")
    gym = next(s for s in new if s.type == "strength")
    assert run.garmin_workout_id == "w1" and run.garmin_schedule_id == "s1"
    assert gym.garmin_workout_id is None
    # Old sessions superseded; claimed donors had their ids moved (nulled).
    old = db.query(Session).filter(Session.status == "superseded").all()
    assert len(old) == 2
    assert all(o.calendar_event_id is None for o in old)


def test_link_matches_selected_workout_when_run_a_day_late(db):
    """A run done a day late links to the session it fulfilled (by the workout id
    the athlete selected on the watch), not to whatever sits on the day they ran."""
    sat = date.today() - timedelta(days=2)
    sun = sat + timedelta(days=1)
    planned = Session(date=sat, type="long_run", title="Long Run — 2h00 Z2", purpose="",
                      status="planned", garmin_workout_id="1627542620",
                      created_at=_utcnow(), updated_at=_utcnow())
    # A gym session sits on the day they actually ran — a run must never link to it,
    # and here it also must not steal the completed run from the Saturday long run.
    gym = Session(date=sun, type="strength", title="Gym — Pull", purpose="",
                  status="planned", created_at=_utcnow(), updated_at=_utcnow())
    db.add_all([planned, gym])
    db.flush()
    db.add(_run(23653415243, sun, workout_id="1627542620"))  # ran Sat's workout on Sun
    db.commit()

    assert store.link_results(db) == 1
    r = db.query(SessionResult).filter(SessionResult.activity_id == 23653415243).one()
    assert r.session_id == planned.id  # Saturday's long run, not the Sunday gym slot
    assert r.completed is True


def test_link_falls_back_to_same_day_without_workout_id(db):
    """A free run (no workout selected) still links to a planned run on its own day."""
    today = date.today() - timedelta(days=1)
    planned = Session(date=today, type="easy", title="Easy Z2", purpose="",
                      status="planned", created_at=_utcnow(), updated_at=_utcnow())
    db.add(planned)
    db.flush()
    db.add(_run(999, today, workout_id=None))
    db.commit()

    assert store.link_results(db) == 1
    r = db.query(SessionResult).filter(SessionResult.activity_id == 999).one()
    assert r.session_id == planned.id


def test_strength_activity_completes_same_day_gym_session(db):
    """A watch-logged strength session marks a planned gym session done (same day)."""
    d = date.today() - timedelta(days=1)
    gym = Session(date=d, type="strength", title="Gym — Pull (upper)", purpose="",
                  status="planned", created_at=_utcnow(), updated_at=_utcnow())
    db.add(gym)
    db.flush()
    db.add(_strength(5001, d, dur_min=65.0))
    db.commit()

    assert store.link_results(db) == 1
    r = db.query(SessionResult).filter(SessionResult.activity_id == 5001).one()
    assert r.session_id == gym.id and r.completed is True
    assert r.actual_duration_min == 65.0 and r.actual_distance_km is None


def test_strength_never_links_to_a_run_session(db):
    """A gym log on a run day must not complete the run (and vice versa is covered
    by the run path). With no gym session that day, it records nothing."""
    d = date.today() - timedelta(days=1)
    run = Session(date=d, type="easy", title="Easy Z2", purpose="",
                  status="planned", created_at=_utcnow(), updated_at=_utcnow())
    db.add(run)
    db.flush()
    db.add(_strength(5002, d))
    db.commit()

    assert store.link_results(db) == 0  # no gym session → no result created
    assert db.query(SessionResult).filter(SessionResult.activity_id == 5002).count() == 0
    assert db.query(SessionResult).filter(SessionResult.session_id == run.id).count() == 0


def test_mobility_activity_does_not_complete_a_gym_session(db):
    """Mobility/yoga are a separate routine — they must never mark the gym done."""
    d = date.today() - timedelta(days=1)
    gym = Session(date=d, type="strength", title="Gym — Push (upper)", purpose="",
                  status="planned", created_at=_utcnow(), updated_at=_utcnow())
    db.add(gym)
    db.flush()
    db.add(_strength(5003, d, atype="mobility"))
    db.commit()

    assert store.link_results(db) == 0
    assert db.query(SessionResult).filter(SessionResult.session_id == gym.id).count() == 0
