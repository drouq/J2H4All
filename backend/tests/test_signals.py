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
