"""Adaptation signals (PRD §12): everything the coach weighs to read the athlete —
planned-vs-actual, load, recovery trend, subjective check-ins, and the calendar
reality ahead (treadmill windows, B-race proximity). Compact for prompts.
"""

from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession

from ..util import RUN_TYPES, as_dt as _as_dt

from ..models import (
    Activity,
    Checkin,
    Goal,
    SecondaryRace,
    Session,
    SessionResult,
    WellnessDaily,
)
from ..plan.summary import context_for_prompt, garmin_summary


def _pace(avg_speed_mps: float | None) -> str | None:
    if not avg_speed_mps:
        return None
    # Round to whole seconds first so 59.5s carries to the next minute (no "5:60").
    sec_per_km = round(1000.0 / avg_speed_mps)
    return f"{sec_per_km // 60}:{sec_per_km % 60:02d}/km"


# Garmin self-evaluation scales (from the activity detail endpoint).
_FEEL_LABELS = {0: "Very Weak", 25: "Weak", 50: "Normal", 75: "Strong", 100: "Very Strong"}


def _feel_label(feel: int | None) -> str | None:
    if feel is None:
        return None
    # Snap to the nearest 25 in case of odd values.
    return _FEEL_LABELS.get(min(_FEEL_LABELS, key=lambda k: abs(k - feel)))


def _rpe_label(rpe: int | None) -> str | None:
    return f"{round(rpe / 10)}/10" if rpe is not None else None


def _secs_to_pace(sec_per_km: float | None) -> str | None:
    if not sec_per_km:
        return None
    s = round(sec_per_km)
    return f"{s // 60}:{s % 60:02d}/km"


def activity_metrics(raw: dict | None) -> dict:
    """Extract the coaching-useful fields Garmin already returns in the activity
    summary but we don't store as columns (running power, grade-adjusted pace,
    running dynamics, real training load, HR-zone time, sweat, fastest splits).
    Only non-empty values are returned. Units per Garmin: power W, GCT ms, stride
    & vertical-osc cm, vertical-ratio %, zone time s, intensity min, water ml."""
    r = raw or {}

    def num(key):
        v = r.get(key)
        if not isinstance(v, (int, float)) or v in (None, 0):
            return None
        return round(v, 1) if isinstance(v, float) else v

    gas = num("avgGradeAdjustedSpeed")
    hr_zone_min = {
        f"z{i}": round(r[f"hrTimeInZone_{i}"] / 60.0, 1)
        for i in range(1, 6) if isinstance(r.get(f"hrTimeInZone_{i}"), (int, float)) and r[f"hrTimeInZone_{i}"]
    }
    dyn = {
        "ground_contact_ms": num("avgGroundContactTime"),
        "stride_length_cm": num("avgStrideLength"),
        "vertical_oscillation_cm": num("avgVerticalOscillation"),
        "vertical_ratio_pct": num("avgVerticalRatio"),
    }
    dyn = {k: v for k, v in dyn.items() if v is not None}
    fastest = {
        "1k": _secs_to_pace(num("fastestSplit_1000")),
        "1mi": _secs_to_pace(num("fastestSplit_1609")),
        "5k": _secs_to_pace(num("fastestSplit_5000") and num("fastestSplit_5000") / 5),
    }
    fastest = {k: v for k, v in fastest.items() if v}

    out = {
        "avg_power_w": num("avgPower"),
        "norm_power_w": num("normPower"),
        "training_load": round(num("activityTrainingLoad"), 1) if num("activityTrainingLoad") else None,
        "te_label": r.get("trainingEffectLabel"),
        "grade_adjusted_pace": _pace(gas) if gas else None,
        "hr_zone_minutes": hr_zone_min or None,
        "running_dynamics": dyn or None,
        "moderate_intensity_min": num("moderateIntensityMinutes"),
        "vigorous_intensity_min": num("vigorousIntensityMinutes"),
        "elevation_loss_m": num("elevationLoss"),
        "moving_min": round(num("movingDuration") / 60.0, 1) if num("movingDuration") else None,
        "sweat_loss_ml": num("waterEstimated"),
        "fastest_splits": fastest or None,
    }
    return {k: v for k, v in out.items() if v is not None}


def recent_runs(db: DbSession, today: date, limit: int = 12, days: int = 21) -> list[dict]:
    """Per-run detail for the most recent runs — so the coach can actually discuss
    a specific run (e.g. 'yesterday'), not just weekly rollups."""
    since = today - timedelta(days=days)
    rows = db.scalars(
        select(Activity).where(Activity.start_time_utc >= _as_dt(since))
        .order_by(Activity.start_time_utc.desc())
    ).all()
    out: list[dict] = []
    for a in rows:
        if not a.activity_type or not any(a.activity_type.startswith(p) for p in RUN_TYPES):
            continue
        when = (a.start_time_local or a.start_time_utc)
        out.append({
            "date": when.date().isoformat() if when else None,
            "type": a.activity_type,
            "name": a.name,
            "distance_km": round((a.distance_m or 0) / 1000.0, 2) if a.distance_m else None,
            "duration_min": round((a.duration_s or 0) / 60.0, 1) if a.duration_s else None,
            "avg_pace": _pace(a.avg_speed_mps),
            "avg_hr": a.avg_hr, "max_hr": a.max_hr,
            "elevation_gain_m": a.elevation_gain_m,
            "aerobic_training_effect": a.aerobic_te,
            # The athlete's own self-evaluation logged on the activity.
            "self_eval_feel": _feel_label(a.feel),
            "self_eval_rpe": _rpe_label(a.rpe),
            # Rich metrics Garmin already returned in the summary (power, GAP,
            # running dynamics, real training load, HR-zone time, sweat, ...).
            **activity_metrics(a.raw),
            # Weather at the run's start, if outdoors (context for the read).
            **({"weather": {"temp_c": a.weather_temp_c, "humidity": a.weather_humidity,
                            "feels_c": a.weather_feels_c}} if a.weather_temp_c is not None else {}),
            # Durability from the per-second stream: aerobic decoupling / HR drift /
            # pace consistency — the metronomic-loop signal for a backyard ultra.
            **({"durability": a.stream_metrics} if a.stream_metrics else {}),
        })
        if len(out) >= limit:
            break
    return out


def recent_checkins(db: DbSession, today: date, days: int = 10) -> list[dict]:
    since = today - timedelta(days=days)
    rows = db.scalars(
        select(Checkin).where(Checkin.date >= since).order_by(Checkin.date)
    ).all()
    return [
        {"date": c.date.isoformat(), "energy": c.energy, "soreness": c.soreness,
         "motivation": c.motivation, "life_stress": c.life_stress, "note": c.note}
        for c in rows
    ]


def recent_lifestyle(db: DbSession, today: date, days: int = 10) -> list[dict]:
    """End-of-day lifestyle logs (alcohol/illness/sleep/stress/nutrition) — the life
    factors Garmin can't see, for attributing a poor overnight reading. Only the
    fields they actually flagged are surfaced; a 'nothing to report' day carries no
    flags but still shows they were clear."""
    from ..models import LifestyleLog
    since = today - timedelta(days=days)
    rows = db.scalars(
        select(LifestyleLog).where(LifestyleLog.date >= since).order_by(LifestyleLog.date)
    ).all()
    out = []
    for r in rows:
        data = r.data or {}
        flags = {k: v for k, v in data.items() if v and k != "summary"}
        out.append({
            "date": r.date.isoformat(),
            "summary": data.get("summary") or r.raw_text,
            **flags,
        })
    return out


def upcoming_window(db: DbSession, today: date, days: int = 21) -> dict:
    """Calendar reality ahead: planned sessions, treadmill windows, race proximity."""
    ctx = context_for_prompt(db)
    sessions = db.scalars(
        select(Session).where(
            Session.status == "planned", Session.date >= today,
            Session.date <= today + timedelta(days=days),
        ).order_by(Session.date)
    ).all()
    goal = db.scalar(select(Goal).where(Goal.status == "active").limit(1))
    races = db.scalars(select(SecondaryRace).order_by(SecondaryRace.date)).all()
    return {
        "planned_sessions": [
            {"date": s.date.isoformat(), "type": s.type, "title": s.title, "purpose": s.purpose}
            for s in sessions
        ],
        "availability_windows": ctx["availability_windows"],
        "injuries": ctx["injuries"],
        "days_to_A_race": (goal.race_date - today).days if goal else None,
        "days_to_next_secondary": next(
            ((r.date - today).days for r in races if r.date >= today), None
        ),
    }


def recent_plan_execution(db: DbSession, today: date, days: int = 14) -> list[dict]:
    """Planned-vs-done for the recent past, INCLUDING what never happened: the
    weekly review is told not to chase missed sessions, so it must be able to
    see them. Skipped '[Optional]' runs are labelled distinctly — skipping those
    is expected behaviour, never something to compensate for."""
    sessions = db.scalars(
        select(Session).where(
            Session.status == "planned",
            Session.date < today, Session.date >= today - timedelta(days=days),
        ).order_by(Session.date)
    ).all()
    from .completion import PARTIAL, classify, delta_line
    # One query for the whole window, not one per session. This ran inside the loop
    # (~20 round-trips on the weekly-review path); the twin in `plan_view` was batched
    # in the 2026-08-03 review and this one was missed. Earliest result per session,
    # which is what the old `.limit(1)` returned in practice — but deterministically.
    result_by_session: dict[int, SessionResult] = {}
    if sessions:
        for r in db.scalars(
            select(SessionResult)
            .where(SessionResult.session_id.in_([s.id for s in sessions]))
            .order_by(SessionResult.id)
        ):
            result_by_session.setdefault(r.session_id, r)
    out = []
    for s in sessions:
        r = result_by_session.get(s.id)
        if r is not None:
            # `completed_off_plan` is a distinct outcome: it happened, but not as
            # prescribed. The cause is only ever what they said (`deviation_reason`).
            status = "completed_off_plan" if classify(s, r, today) == PARTIAL else "completed"
        elif s.type in ("rest", "strength"):
            status = "no_data"  # rest days and gym sessions aren't Garmin-tracked
        elif "[optional]" in (s.title or "").lower():
            status = "skipped_optional"
        else:
            # `missed` (the day closed, still liveable — they shift runs by a day or two)
            # vs `abandoned` (past the grace window, gone). Two different coaching
            # situations, so the coach gets two different words; see completion.py.
            # The query already restricts to `date < today`, so this is never PLANNED.
            status = classify(s, None, today)
        entry = {
            "date": s.date.isoformat(), "type": s.type, "title": s.title,
            "planned_km": s.distance_km, "planned_min": s.duration_min,
            "status": status,
        }
        if r is not None:
            entry["actual"] = {
                "distance_km": r.actual_distance_km, "duration_min": r.actual_duration_min,
                "avg_hr": r.actual_avg_hr, "read": r.read_summary,
            }
            if status == "completed_off_plan":
                entry["deviation"] = delta_line(s, r)
                entry["deviation_reason"] = r.deviation_reason  # their words, or null = not asked/answered
        out.append(entry)
    return out


def gather(db: DbSession, today: date) -> dict:
    """The full signal bundle the weekly review / red-flag reason over."""
    return {
        "as_of": today.isoformat(),
        "garmin": garmin_summary(db, today),
        "recovery_deep": deep_recovery(db, today),
        "data_freshness": data_freshness(db, today),
        "checkins": recent_checkins(db, today),
        "recent_lifestyle": recent_lifestyle(db, today, days=10),
        "recent_plan_execution": recent_plan_execution(db, today),
        "ahead": upcoming_window(db, today),
        "context": context_for_prompt(db),
    }


def latest_markers(db: DbSession) -> dict:
    """Latest Training Readiness (flagship recovery score), Endurance & Hill scores."""
    from ..models import FitnessMarker

    def latest(kind: str):
        return db.scalar(
            select(FitnessMarker).where(FitnessMarker.kind == kind)
            .order_by(FitnessMarker.date.desc()).limit(1)
        )

    tr = latest("training_readiness")
    endur = latest("endurance_score")
    hill = latest("hill_score")
    readiness = None
    if tr and tr.value:
        readiness = {
            "score": tr.value.get("score"), "level": tr.value.get("level"),
            "feedback": tr.value.get("feedbackShort"), "date": tr.date.isoformat(),
        }
    return {
        "training_readiness": readiness,
        "endurance_score": endur.value_num if endur else None,
        "hill_score": hill.value_num if hill else None,
    }


def data_freshness(db: DbSession, today: date) -> dict:
    """How current the recovery inputs are, so the brief can state each reading's age
    and nudge a watch-sync when this morning's overnight data isn't in Garmin yet
    (the athlete typically syncs post-activity, not every morning — PRD §7)."""
    from ..models import FitnessMarker

    def latest_wellness_date(col):
        return db.scalar(
            select(WellnessDaily.date)
            .where(col.isnot(None)).order_by(WellnessDaily.date.desc()).limit(1)
        )

    def age(d):
        return (today - d).days if d else None

    def entry(d):
        return {"date": d.isoformat(), "days_ago": age(d)} if d else {"date": None, "days_ago": None}

    hrv_d = latest_wellness_date(WellnessDaily.hrv_last_night_avg)
    tr_d = db.scalar(
        select(FitnessMarker.date).where(FitnessMarker.kind == "training_readiness")
        .order_by(FitnessMarker.date.desc()).limit(1)
    )
    # WellnessDaily.synced_at is OUR backend's pull time (_utcnow() at sync), NOT
    # when the athlete's watch uploaded to Garmin — we have no such signal. Named and
    # rendered explicitly so the coach can't retell our 01:00-UTC cron as "your
    # watch synced at 2am" (it did that: a UTC hour quoted raw, 8h off in SGT).
    last_pull = db.scalar(
        select(WellnessDaily.synced_at).order_by(WellnessDaily.synced_at.desc()).limit(1)
    )
    from .schedule import fmt_local
    return {
        "today": today.isoformat(),
        "hrv": entry(hrv_d),
        "resting_hr": entry(latest_wellness_date(WellnessDaily.resting_hr)),
        "sleep": entry(latest_wellness_date(WellnessDaily.sleep_score)),
        "training_readiness": entry(tr_d),
        "last_backend_pull_local": fmt_local(db, last_pull) if last_pull else None,
        # True only when we actually have last night's overnight recovery (today's HRV).
        "overnight_recovery_is_current": age(hrv_d) == 0 if hrv_d is not None else False,
    }


def deep_recovery(db: DbSession, today: date) -> dict:
    """Illness early-warning + sleep-quality context mined from the wellness raw
    payloads we already store (zero extra Garmin calls): waking respiration,
    skin-temperature deviation (Garmin computes it vs their personal baseline —
    a rise is a classic pre-symptom fever signal), sleep restlessness (context
    for the RLS sleep-score rule — their restlessness is chronically high, so
    judge vs HIS baseline), and body battery at wake."""
    rows = db.scalars(
        select(WellnessDaily).where(
            WellnessDaily.date >= today - timedelta(days=30),
            WellnessDaily.date <= today,
            WellnessDaily.raw.isnot(None),
        ).order_by(WellnessDaily.date)
    ).all()
    per_day: dict[date, dict] = {}
    for w in rows:
        raw = w.raw or {}
        sl = raw.get("sleep") or {}
        sm = raw.get("summary") or {}
        per_day[w.date] = {
            "respiration": sm.get("avgWakingRespirationValue"),
            "skin_temp_dev_c": sl.get("avgSkinTempDeviationC"),
            "restless_moments": sl.get("restlessMomentsCount"),
            "bb_at_wake": sm.get("bodyBatteryAtWakeTime"),
        }
    if not per_day:
        return {"latest": None}

    def _avg(key, days_from, days_to, digits=1):
        lo, hi = today - timedelta(days=days_from), today - timedelta(days=days_to)
        vals = [v[key] for d, v in per_day.items() if lo <= d <= hi and v[key] is not None]
        return round(sum(vals) / len(vals), digits) if vals else None

    latest_date = max(per_day)
    return {
        "latest": {"date": latest_date.isoformat(), **per_day[latest_date]},
        "respiration_recent_3d": _avg("respiration", 2, 0),
        "respiration_baseline_28d": _avg("respiration", 30, 3),
        "skin_temp_dev_recent_3d": _avg("skin_temp_dev_c", 2, 0, digits=2),
        "restless_moments_recent_3d": _avg("restless_moments", 2, 0),
        "restless_moments_baseline_28d": _avg("restless_moments", 30, 3),
        "body_battery_at_wake_latest": per_day[latest_date]["bb_at_wake"],
    }


def recovery_baseline(db: DbSession, today: date) -> dict:
    """Short vs. longer HRV / resting-HR windows — the raw inputs a red-flag check needs.

    Windows are INCLUSIVE of today: this morning's overnight reading (the row
    data_freshness nudges the user to sync) must enter the recent average the
    same day, or an acute overnight crash isn't seen until tomorrow."""
    def _avg(col, days_from, days_to):
        lo = today - timedelta(days=days_from)
        hi = today - timedelta(days=days_to)
        vals = db.scalars(
            select(col).where(WellnessDaily.date >= lo, WellnessDaily.date <= hi, col.isnot(None))
        ).all()
        return round(sum(v for v in vals) / len(vals), 1) if vals else None

    return {
        "hrv_recent_3d": _avg(WellnessDaily.hrv_last_night_avg, 2, 0),      # [today-2, today]
        "hrv_baseline_28d": _avg(WellnessDaily.hrv_last_night_avg, 30, 3),  # [today-30, today-3]
        "rhr_recent_3d": _avg(WellnessDaily.resting_hr, 2, 0),
        "rhr_baseline_28d": _avg(WellnessDaily.resting_hr, 30, 3),
    }
