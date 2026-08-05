"""Web trend series: load (acute:chronic), HRV/resting-HR, weekly volume
vs plan, blood-marker history, VO2max / race-predictor curve. Pure read/rollup —
no LLM. The frontend renders these as lightweight inline SVG charts."""

from collections import defaultdict
from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession

from .models import Activity, BloodMarker, FitnessMarker, Session, WellnessDaily
from .util import RUN_TYPES, as_dt as _as_dt


def _iso_week_start(d: date) -> date:
    return d - timedelta(days=d.weekday())


# Session types that count as running volume (a gym/rest day is not run volume).
_RUN_SESSION_TYPES = ("long_run", "easy", "recovery", "intervals", "tempo", "race")


def weekly_volume(db: DbSession, today: date, past_weeks: int = 12, future_weeks: int = 4) -> list[dict]:
    """Weekly running volume, actual (Garmin) vs planned (the plan), in both km and
    minutes. Since the coach now prescribes by DURATION (HR/time in the heat), planned
    sessions usually carry no distance — so planned_km is empty and the web chart plots
    time-on-feet (planned_min vs actual_min), which is real on both sides and is the
    doctrine's KPI anyway. km is retained for the acute:chronic proxy."""
    start = _iso_week_start(today) - timedelta(weeks=past_weeks)
    actual_km: dict[date, float] = defaultdict(float)
    actual_min: dict[date, float] = defaultdict(float)
    for start_local, atype, dist_m, dur_s in db.execute(
        select(Activity.start_time_local, Activity.activity_type, Activity.distance_m, Activity.duration_s)
        .where(Activity.start_time_utc >= _as_dt(start))
    ).all():
        if not atype or not any(atype.startswith(p) for p in RUN_TYPES):
            continue
        wk = _iso_week_start((start_local or _as_dt(start)).date())
        if dist_m:
            actual_km[wk] += dist_m / 1000.0
        if dur_s:
            actual_min[wk] += dur_s / 60.0

    planned_km: dict[date, float] = defaultdict(float)
    planned_min: dict[date, float] = defaultdict(float)
    for d, dist, dur, stype, title in db.execute(
        select(Session.date, Session.distance_km, Session.duration_min, Session.type, Session.title).where(
            Session.status == "planned", Session.date >= today, Session.type.in_(_RUN_SESSION_TYPES)
        )
    ).all():
        if "[optional]" in (title or "").lower():
            continue  # skipping an optional run is expected — not a plan shortfall
        wk = _iso_week_start(d)
        if dist is not None:
            planned_km[wk] += dist
        if dur is not None:
            planned_min[wk] += dur

    weeks = []
    wk = start
    end = _iso_week_start(today) + timedelta(weeks=future_weeks)
    this_week = _iso_week_start(today)
    while wk <= end:
        is_past = wk <= this_week
        weeks.append({
            "week": wk.isoformat(),
            "actual_km": round(actual_km.get(wk, 0), 1) if is_past else None,
            "planned_km": round(planned_km.get(wk, 0), 1) if planned_km.get(wk) else None,
            "actual_min": round(actual_min.get(wk, 0)) if is_past else None,
            "planned_min": round(planned_min.get(wk, 0)) if planned_min.get(wk) else None,
        })
        wk += timedelta(weeks=1)
    return weeks


def acwr_proxy(db: DbSession, today: date, weeks: int = 12) -> list[dict]:
    """Volume-based acute:chronic proxy — acute (this week) / chronic (trailing 4-wk avg).
    A proxy, not Garmin's device figure; good enough to watch ramp rate."""
    vol = weekly_volume(db, today, past_weeks=weeks + 4, future_weeks=0)
    kms = [(w["week"], w["actual_km"] or 0) for w in vol]
    out = []
    for i in range(4, len(kms)):
        chronic = sum(k for _, k in kms[i - 4:i]) / 4.0
        acute = kms[i][1]
        out.append({"week": kms[i][0], "ratio": round(acute / chronic, 2) if chronic else None})
    return out[-weeks:]


def recovery_series(db: DbSession, today: date, days: int = 60) -> list[dict]:
    since = today - timedelta(days=days)
    rows = db.execute(
        select(WellnessDaily.date, WellnessDaily.hrv_last_night_avg, WellnessDaily.resting_hr)
        .where(WellnessDaily.date >= since).order_by(WellnessDaily.date)
    ).all()
    return [{"date": d.isoformat(), "hrv": hrv, "rhr": rhr} for d, hrv, rhr in rows]


def vo2max_curve(db: DbSession) -> list[dict]:
    rows = db.execute(
        select(FitnessMarker.date, FitnessMarker.value_num)
        .where(FitnessMarker.kind == "vo2max_running", FitnessMarker.value_num.isnot(None))
        .order_by(FitnessMarker.date)
    ).all()
    # thin to monthly points
    out, seen = [], set()
    for d, v in rows:
        key = (d.year, d.month)
        if key not in seen:
            seen.add(key)
            out.append({"date": d.isoformat(), "vo2max": round(v, 1)})
    return out


def durability_series(db: DbSession, today: date, days: int = 120) -> list[dict]:
    """Per-run durability from the stream rollup: aerobic decoupling and per-km
    pace CV over time — evenness under fatigue. Lower is better on both;
    the trend (is decoupling falling as the base builds?) is what matters."""
    since = today - timedelta(days=days)
    rows = db.scalars(
        select(Activity).where(
            Activity.start_time_utc >= _as_dt(since),
            Activity.stream_metrics.isnot(None),
        ).order_by(Activity.start_time_utc)
    ).all()
    out = []
    for a in rows:
        m = a.stream_metrics or {}
        when = a.start_time_local or a.start_time_utc
        out.append({
            "date": when.date().isoformat() if when else None,
            "decoupling_pct": m.get("aerobic_decoupling_pct"),
            "pace_cv_pct": m.get("pace_cv_pct"),
        })
    return out


def blood_history(db: DbSession) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = defaultdict(list)
    for b in db.scalars(select(BloodMarker).order_by(BloodMarker.name, BloodMarker.measured_on)).all():
        out[b.name].append({"date": b.measured_on.isoformat(), "value": b.value, "unit": b.unit})
    return dict(out)


def build(db: DbSession, today: date | None = None) -> dict:
    from .plan.summary import heat_acclimation, training_load_balance

    today = today or date.today()
    return {
        "weekly_volume": weekly_volume(db, today),
        "acwr": acwr_proxy(db, today),
        "recovery": recovery_series(db, today),
        "vo2max": vo2max_curve(db),
        "durability": durability_series(db, today),
        # Coach signals also surfaced on the web (were prompt-only before):
        "training_load_balance": training_load_balance(db, today),
        "heat_acclimation": heat_acclimation(db, today),
        "blood_markers": blood_history(db),
    }
