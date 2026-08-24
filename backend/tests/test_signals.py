"""Signal extraction from the training_status marker raw — training-load balance
(80/20) and heat acclimation. Both must walk back past Garmin's frequent NULL
snapshots to the most recent real one."""
from datetime import date, timedelta

from app.models import FitnessMarker
from app.plan.summary import heat_acclimation, training_load_balance
from app.util import utcnow as _utcnow


def _marker(db, d, value):
    db.add(FitnessMarker(date=d, kind="training_status", value=value, synced_at=_utcnow()))


def _load_value():
    return {
        "mostRecentTrainingLoadBalance": {
            "metricsTrainingLoadBalanceDTOMap": {
                "dev1": {
                    "primaryTrainingDevice": True,
                    "trainingBalanceFeedbackPhrase": "AEROBIC_LOW_SHORTAGE",
                    "monthlyLoadAnaerobic": 49, "monthlyLoadAnaerobicTargetMin": 0, "monthlyLoadAnaerobicTargetMax": 87,
                    "monthlyLoadAerobicLow": 186, "monthlyLoadAerobicLowTargetMin": 452, "monthlyLoadAerobicLowTargetMax": 671,
                    "monthlyLoadAerobicHigh": 587, "monthlyLoadAerobicHighTargetMin": 291, "monthlyLoadAerobicHighTargetMax": 510,
                }
            }
        }
    }


def _heat_value():
    return {"mostRecentVO2Max": {"heatAltitudeAcclimation": {
        "heatTrend": "ACCLIMATIZING", "heatAcclimationPercentage": 25,
        "previousHeatAcclimationPercentage": 0, "heatAcclimationDate": "2026-07-12",
    }}}


def test_load_balance_walks_past_null_snapshot(db):
    today = date.today()
    _marker(db, today, {"mostRecentTrainingLoadBalance": None})  # today's snapshot is empty
    _marker(db, today - timedelta(days=1), _load_value())
    db.commit()

    lb = training_load_balance(db, today)
    assert lb is not None
    assert lb["feedback"] == "AEROBIC_LOW_SHORTAGE"
    assert lb["aerobic_low"]["status"] == "under"
    assert lb["aerobic_high"]["status"] == "over"
    assert lb["anaerobic"]["status"] == "in_range"


def test_heat_acclimation_walks_past_null_and_reads_nested(db):
    today = date.today()
    _marker(db, today, {"mostRecentVO2Max": None})
    _marker(db, today - timedelta(days=1), _heat_value())
    db.commit()

    h = heat_acclimation(db, today)
    assert h is not None
    assert h["heat_acclimation_pct"] == 25
    assert h["previous_pct"] == 0
    assert h["trend"] == "ACCLIMATIZING"


def test_signals_none_when_no_data(db):
    assert training_load_balance(db, date.today()) is None
    assert heat_acclimation(db, date.today()) is None


# ---- peak_4wk counts training, not races ------------------------------------
# The peak block is read as "volume this athlete has already absorbed" — a capacity
# benchmark for how hard they can be ramped. A race week is not that. In the reference
# deployment the peak read 84 km/wk from a 4-week window ENDING in a 170 km race week,
# counting the race as training volume; the true training peak was 72, and the coach
# cited the inflated figure while justifying a ramp past its own 10%/week ceiling.
#
# There is deliberately no hard-coded race list in this build: races are recognised from
# a race-type session, or from Garmin's own eventType tag.

from app.models import Activity, Session, SessionResult  # noqa: E402
from app.plan.summary import _race_activity_ids, garmin_summary  # noqa: E402


def _run(db, aid, d, km, atype="running", raw=None):
    start = _utcnow().replace(year=d.year, month=d.month, day=d.day, hour=8, minute=0)
    db.add(Activity(
        id=aid, start_time_utc=start, start_time_local=start.replace(tzinfo=None),
        activity_type=atype, name="run", duration_s=km * 360, distance_m=km * 1000,
        raw=raw or {}, synced_at=_utcnow(), detail_synced=False, streams_synced=False))
    db.commit()


def _steady_weeks(db, today, km, start_id=700):
    """Four consecutive full weeks of steady training, ending last week."""
    last_monday = today - timedelta(days=today.weekday() + 7)
    for i in range(4):
        _run(db, start_id + i, last_monday - timedelta(days=7 * i), km)


def _raced(db, aid, day, km, *, planned=True):
    """A race effort — via a race-type session if planned, else Garmin's own tag."""
    if planned:
        s = Session(date=day, type="race", title="Target race", purpose="",
                    status="planned", created_at=_utcnow(), updated_at=_utcnow())
        db.add(s)
        db.flush()
        _run(db, aid, day, km, atype="ultra_run")
        db.add(SessionResult(session_id=s.id, activity_id=aid, completed=True,
                             created_at=_utcnow()))
        db.commit()
    else:
        _run(db, aid, day, km, atype="ultra_run",
             raw={"eventType": {"typeId": 1, "typeKey": "race"}})


def test_a_planned_race_does_not_count_toward_the_peak(db):
    today = date.today()
    _steady_weeks(db, today, km=40.0)
    _raced(db, 5555, today - timedelta(days=today.weekday() + 7), 170.6)
    g = garmin_summary(db, today)
    assert g["peak_4wk_avg_km"] == 40.0
    assert "training only" in g["peak_4wk_basis"]


def test_a_race_tagged_in_garmin_does_not_count_either(db):
    """How history from BEFORE adopting the app is excluded: tag it in Garmin Connect."""
    today = date.today()
    _steady_weeks(db, today, km=40.0)
    _raced(db, 5556, today - timedelta(days=today.weekday() + 7), 170.6, planned=False)
    assert 5556 in _race_activity_ids(db)
    assert garmin_summary(db, today)["peak_4wk_avg_km"] == 40.0


def test_the_race_week_still_shows_in_the_weekly_history(db):
    """Excluded from the benchmark, NOT hidden — the coach should see they raced."""
    today = date.today()
    _steady_weeks(db, today, km=40.0)
    _raced(db, 5557, today - timedelta(days=today.weekday() + 7), 170.6)
    g = garmin_summary(db, today)
    raced = [w for w in g["weekly_running"] if w.get("includes_race")]
    assert len(raced) == 1
    assert raced[0]["km"] == 210.6          # the full truth, race included


def test_a_long_training_run_is_not_a_race(db):
    """A 63 km long run is training and must keep counting — the fix must not become
    "anything long is a race", which would delete real weeks from the history."""
    _run(db, 5558, date.today() - timedelta(days=10), 63.3, atype="trail_running")
    assert _race_activity_ids(db) == set()
