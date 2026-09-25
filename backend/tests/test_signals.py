"""Signal extraction from the training_status marker raw — training-load balance
(80/20) and heat acclimation. Both must walk back past Garmin's frequent NULL
snapshots to the most recent real one."""
from datetime import UTC, date, timedelta

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


# ---- per-night sleep volume -------------------------------------
# In the reference deployment a ~4.5h night reached the coach only as a 14-day average it moved by ~15 minutes, so
# nothing was said until the athlete's own tired-feeling logs arrived. And wherever sleep scores read wrong the
# stage-classified asleep-hours blur a restless full night into the same number as a
# genuinely short one — only the bedtime→wake WINDOW tells them apart.

from app.coach.signals import recent_sleep_nights  # noqa: E402
from app.models import WellnessDaily  # noqa: E402


def _night(db, d, *, start_ms=None, end_ms=None, asleep_s=None, score=None):
    db.add(WellnessDaily(
        date=d, sleep_seconds=asleep_s, sleep_score=score,
        raw={"sleep": {"dailySleepDTO": {
            "sleepStartTimestampLocal": start_ms, "sleepEndTimestampLocal": end_ms,
        }}} if start_ms else {},
        synced_at=_utcnow(),
    ))
    db.commit()


def _ms(day, hh, mm):
    """Epoch-millis for a LOCAL wall time, the way Garmin's *Local stamps encode it."""
    from datetime import datetime
    return int(datetime(day.year, day.month, day.day, hh, mm, tzinfo=UTC).timestamp() * 1000)


def test_the_short_night_is_visible_per_night(db):
    """The reference case: in bed 00:13, up 04:36 — invisible in any average, obvious here."""
    today = date.today()
    _night(db, today, start_ms=_ms(today, 0, 13), end_ms=_ms(today, 4, 36), asleep_s=14832)
    nights = recent_sleep_nights(db, today)
    assert nights == [{
        "date": today.isoformat(), "to_bed": "00:13", "woke": "04:36",
        "in_bed_h": 4.38, "asleep_h": 4.12,
    }]


def test_local_timestamps_are_formatted_without_conversion(db):
    """The *Local epoch already encodes wall time. Rendering it through the server's
    zone would shift the bedtime by the server-vs-athlete offset — on a +8 server a
    23:56 bedtime would read 07:56."""
    today = date.today()
    _night(db, today, start_ms=_ms(today, 23, 56), end_ms=_ms(today, 23, 59), asleep_s=60)
    assert recent_sleep_nights(db, today)[0]["to_bed"] == "23:56"


def test_a_night_without_timestamps_still_reports_asleep_hours(db):
    """Older rows predate the stored DTO or lack it; the volume signal degrades to
    what exists rather than dropping the night."""
    today = date.today()
    _night(db, today, asleep_s=7 * 3600)
    nights = recent_sleep_nights(db, today)
    assert nights == [{"date": today.isoformat(), "asleep_h": 7.0}]


def test_a_night_with_no_data_at_all_is_skipped(db):
    """A bare date tells the coach nothing — an unworn watch shouldn't render as a
    mysterious empty night."""
    today = date.today()
    _night(db, today, asleep_s=None)
    assert recent_sleep_nights(db, today) == []


def test_window_is_seven_nights_ordered(db):
    today = date.today()
    for i in range(10):  # 10 nights back; only the last 7 should return
        d = today - timedelta(days=i)
        _night(db, d, start_ms=_ms(d, 23, 0), end_ms=_ms(d, 23, 30) + 8 * 3600 * 1000,
               asleep_s=7 * 3600)
    nights = recent_sleep_nights(db, today)
    assert len(nights) == 7
    assert nights[0]["date"] == (today - timedelta(days=6)).isoformat()
    assert nights[-1]["date"] == today.isoformat()


def test_peak_block_survives_a_history_shorter_than_four_weeks(db):
    """A 4-week block needs four weeks. With fewer, the peak loop never runs and both
    values stay None — and `round(None)` raised a TypeError that took the whole weekly
    review down. Latent for an established account, a hard crash on a new install."""
    _run(db, 6001, date.today() - timedelta(days=2), 10.0)
    g = garmin_summary(db, date.today())
    assert g["peak_4wk_avg_km"] is None and g["peak_4wk_ending"] is None
    assert g["recent_4wk_avg_km"] is not None      # the rest of the bundle still builds
