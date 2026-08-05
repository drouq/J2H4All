"""Rolled-up Garmin + context summary for lean Opus prompts (token strategy).

The store keeps the full ~2 years, but the coach reasons over weekly/monthly
rollups plus recent detail — not raw dumps. This builds that compact picture.
"""

from collections import defaultdict
from datetime import date, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session as DbSession

from ..context.store import snapshot as context_snapshot
from ..models import Activity, FitnessMarker, WellnessDaily
from ..util import RUN_TYPES


def _iso_week(d: date) -> str:
    y, w, _ = d.isocalendar()
    return f"{y}-W{w:02d}"


def training_load_balance(db: DbSession, today: date) -> dict | None:
    """Garmin's monthly training-load balance — anaerobic / low-aerobic / high-aerobic
    load vs its OWN target ranges — a direct 80/20 intensity-distribution read (signals). Garmin often returns a null snapshot, so take the most recent training_status
    marker in the last ~45 days that actually carries balance data, and read the primary
    training device."""
    rows = db.scalars(
        select(FitnessMarker)
        .where(FitnessMarker.kind == "training_status", FitnessMarker.date >= today - timedelta(days=45))
        .order_by(FitnessMarker.date.desc())
    ).all()
    for m in rows:
        balance = (m.value or {}).get("mostRecentTrainingLoadBalance")
        dmap = (balance or {}).get("metricsTrainingLoadBalanceDTOMap") or {}
        dev = next((d for d in dmap.values() if d.get("primaryTrainingDevice")), None) or next(iter(dmap.values()), None)
        if dev is None:
            continue

        def band(load, lo, hi):
            load = round(load) if load is not None else None
            status = None
            if load is not None and lo is not None and hi is not None:
                status = "under" if load < lo else "over" if load > hi else "in_range"
            return {"load": load, "target_min": lo, "target_max": hi, "status": status}

        return {
            "as_of": dev.get("calendarDate"),
            "feedback": dev.get("trainingBalanceFeedbackPhrase"),
            "anaerobic": band(dev.get("monthlyLoadAnaerobic"), dev.get("monthlyLoadAnaerobicTargetMin"), dev.get("monthlyLoadAnaerobicTargetMax")),
            "aerobic_low": band(dev.get("monthlyLoadAerobicLow"), dev.get("monthlyLoadAerobicLowTargetMin"), dev.get("monthlyLoadAerobicLowTargetMax")),
            "aerobic_high": band(dev.get("monthlyLoadAerobicHigh"), dev.get("monthlyLoadAerobicHighTargetMin"), dev.get("monthlyLoadAerobicHighTargetMax")),
        }
    return None


def heat_acclimation(db: DbSession, today: date) -> dict | None:
    """Garmin's heat-acclimation read. Relevant when the athlete trains or races in
    heat, and simply noise when they don't — the doctrine tells the coach to check the
    race's actual conditions before reasoning from it, rather than treating the number
    as a mandate.

    Lives NESTED at `mostRecentVO2Max.heatAltitudeAcclimation` of the training_status
    marker (the top-level `heatAltitudeAcclimationDTO` stays null). The altitude half is
    ignored: a formal acclimatization protocol is out of scope either way. Most recent
    non-null snapshot in ~30 days."""
    rows = db.scalars(
        select(FitnessMarker)
        .where(FitnessMarker.kind == "training_status", FitnessMarker.date >= today - timedelta(days=30))
        .order_by(FitnessMarker.date.desc())
    ).all()
    for m in rows:
        heat = ((m.value or {}).get("mostRecentVO2Max") or {}).get("heatAltitudeAcclimation") or {}
        if heat.get("heatAcclimationPercentage") is None:
            continue
        return {
            "as_of": heat.get("heatAcclimationDate") or heat.get("calendarDate"),
            "heat_acclimation_pct": heat.get("heatAcclimationPercentage"),
            "previous_pct": heat.get("previousHeatAcclimationPercentage"),
            "trend": heat.get("heatTrend"),
        }
    return None


def garmin_summary(db: DbSession, today: date, weeks: int = 16) -> dict:
    """Weekly run volume (recent), monthly volume arc (full history), peak 4-week
    block, fitness-marker curve, acute:chronic, recent recovery trend.

    The monthly arc + peak block cover the FULL stored history (rollups):
    without them the coach only sees the recent-weeks window and misreads a big
    race week (e.g. the UTA Miler build) as an outlier instead of a peak the
    athlete trained up to — their real, already-absorbed base."""
    since = today - timedelta(weeks=weeks)

    # Running volume over the FULL history: weekly (keyed by ISO-week Monday, for
    # the recent window + peak-block math) and monthly (the long arc, compact).
    acts = db.execute(
        select(Activity.start_time_local, Activity.start_time_utc, Activity.activity_type,
               Activity.distance_m, Activity.duration_s, Activity.elevation_gain_m)
    ).all()
    week_km: dict[date, float] = defaultdict(float)
    week_runs: dict[date, int] = defaultdict(int)
    week_elev: dict[date, float] = defaultdict(float)
    monthly_km: dict[str, float] = defaultdict(float)
    monthly_runs: dict[str, int] = defaultdict(int)
    monthly_elev: dict[str, float] = defaultdict(float)
    longest_run_km = 0.0
    longest_run_date: date | None = None
    for start_local, start_utc, atype, dist_m, dur_s, elev_m in acts:
        if not atype or not any(atype.startswith(p) for p in RUN_TYPES):
            continue
        start = start_local or start_utc
        if start is None:
            continue
        d = start.date()
        km = (dist_m or 0) / 1000.0
        monday = d - timedelta(days=d.weekday())
        week_km[monday] += km
        week_runs[monday] += 1
        week_elev[monday] += elev_m or 0.0
        monthly_km[f"{d.year}-{d.month:02d}"] += km
        monthly_runs[f"{d.year}-{d.month:02d}"] += 1
        monthly_elev[f"{d.year}-{d.month:02d}"] += elev_m or 0.0
        if km > longest_run_km:
            longest_run_km, longest_run_date = km, d

    # Elevation is carried per week/month so trail blocks (e.g. the UTA Miler
    # build) are visible as vert, not just flat km — future ultra-trail goals
    # will reason over the same history.
    weekly = [
        {"week": _iso_week(monday), "km": round(week_km[monday], 1), "runs": week_runs[monday],
         "elev_m": round(week_elev[monday])}
        for monday in sorted(week_km) if monday >= since
    ]
    monthly = [
        {"month": m, "km": round(monthly_km[m], 1), "runs": monthly_runs[m],
         "elev_m": round(monthly_elev[m])}
        for m in sorted(monthly_km)
    ]

    # Peak 4-consecutive-week block over the full history (zero-filled gaps), so
    # the coach knows what volume this athlete has already successfully absorbed.
    # The series extends through the last FULL week so the recent average below
    # reflects a lull as zeros instead of skipping back to months-old volume.
    peak_4wk_avg_km = None
    peak_4wk_ending = None
    recent_4wk_avg_km = 0.0
    if week_km:
        this_monday = today - timedelta(days=today.weekday())
        lo = min(week_km)
        hi = max(max(week_km), this_monday - timedelta(days=7))
        mondays: list[date] = []
        cur = lo
        while cur <= hi:
            mondays.append(cur)
            cur += timedelta(days=7)
        series = [week_km.get(m, 0.0) for m in mondays]
        for i in range(3, len(series)):
            avg = sum(series[i - 3:i + 1]) / 4
            if peak_4wk_avg_km is None or avg > peak_4wk_avg_km:
                peak_4wk_avg_km, peak_4wk_ending = avg, mondays[i]
        peak_4wk_avg_km = round(peak_4wk_avg_km, 1)
        peak_4wk_ending = _iso_week(peak_4wk_ending)
        # Recent average = last 4 FULL weeks, zero-filled, excluding the
        # in-progress week — the honest "what is they actually running right now".
        full = [v for m, v in zip(mondays, series) if m < this_monday]
        if full:
            tail = full[-4:]
            recent_4wk_avg_km = round(sum(tail) / len(tail), 1)

    # VO2max curve (monthly latest)
    vo2 = db.execute(
        select(FitnessMarker.date, FitnessMarker.value_num)
        .where(FitnessMarker.kind == "vo2max_running")
        .order_by(FitnessMarker.date)
    ).all()
    vo2_curve = []
    seen_month = set()
    for d, v in vo2:
        key = (d.year, d.month)
        if key not in seen_month and v is not None:
            seen_month.add(key)
            vo2_curve.append({"month": f"{d.year}-{d.month:02d}", "vo2max": round(v, 1)})
    vo2_latest = round(vo2[-1][1], 1) if vo2 and vo2[-1][1] is not None else None

    # Training status / acute:chronic (latest snapshot)
    ts = db.scalar(
        select(FitnessMarker).where(FitnessMarker.kind == "training_status")
        .order_by(FitnessMarker.date.desc()).limit(1)
    )
    acwr = None
    if ts and ts.value:
        for dev in (ts.value.get("mostRecentTrainingStatus") or {}).get("latestTrainingStatusData", {}).values():
            acute = (dev.get("acuteTrainingLoadDTO") or {})
            if acute:
                acwr = {
                    "acute": acute.get("dailyTrainingLoadAcute"),
                    "chronic": acute.get("dailyTrainingLoadChronic"),
                    "ratio": acute.get("dailyAcuteChronicWorkloadRatio"),
                    "status": acute.get("acwrStatus"),
                }
                break

    # Recent recovery trend (last 14 days averages)
    rec_since = today - timedelta(days=14)
    rec = db.execute(
        select(
            func.avg(WellnessDaily.resting_hr),
            func.avg(WellnessDaily.hrv_last_night_avg),
            func.avg(WellnessDaily.sleep_seconds),
            func.avg(WellnessDaily.body_battery_high),
        ).where(WellnessDaily.date >= rec_since)
    ).one()
    recovery = {
        "avg_resting_hr": round(float(rec[0]), 1) if rec[0] is not None else None,
        "avg_hrv": round(float(rec[1]), 1) if rec[1] is not None else None,
        "avg_sleep_h": round(float(rec[2]) / 3600.0, 1) if rec[2] is not None else None,
        "avg_body_battery_high": round(float(rec[3]), 1) if rec[3] is not None else None,
    }

    # Race predictor (latest)
    rp = db.scalar(
        select(FitnessMarker).where(FitnessMarker.kind == "race_prediction")
        .order_by(FitnessMarker.date.desc()).limit(1)
    )

    return {
        "as_of": today.isoformat(),
        "weeks_covered": weeks,
        "weekly_running": weekly[-weeks:],
        "monthly_running_full_history": monthly,
        "recent_4wk_avg_km": recent_4wk_avg_km,
        "peak_4wk_avg_km": peak_4wk_avg_km,
        "peak_4wk_ending": peak_4wk_ending,
        "longest_run_km": round(longest_run_km, 1),
        "longest_run_date": longest_run_date.isoformat() if longest_run_date else None,
        "vo2max_latest": vo2_latest,
        "vo2max_curve": vo2_curve,
        "acute_chronic": acwr,
        "training_load_balance": training_load_balance(db, today),
        "heat_acclimation": heat_acclimation(db, today),
        "recovery_14d": recovery,
        "race_prediction": rp.value if rp else None,
    }


def context_for_prompt(db: DbSession) -> dict:
    """Phase 2 context, trimmed for the plan prompt."""
    from ..coach.schedule import local_today
    snap = context_snapshot(db)
    today_iso = local_today(db).isoformat()  # their local day, not server UTC
    return {
        "timezone": snap["timezone"],
        "diet": snap["diet"],
        "blood_markers": snap["blood_markers"],
        # Only windows still in effect — expired treadmill windows would otherwise
        # accumulate in every prompt forever (web Context panel still shows all).
        "availability_windows": [
            w for w in snap["availability_windows"]
            if w["end_date"] is None or w["end_date"] >= today_iso
        ],
        "injuries": snap["injuries"],
        "preferences": snap["preferences"],
        # Free-text coaching memory — e.g. training-history blocks
        # (previous coach's methods, past race builds). Was previously dropped
        # here, so Note rows never reached any prompt.
        "coaching_notes": [
            {"text": n["text"], "recorded": n["created_at"][:10]} for n in snap["notes"]
        ],
    }
