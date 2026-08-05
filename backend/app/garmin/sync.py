"""Garmin sync engine.

Full ~2-year bootstrap import + incremental daily sync, idempotent upserts,
sync_run audit trail, and loud-not-silent failure alerts via Telegram.
"""

import logging
import threading
import time
import traceback
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..db import SessionLocal
from ..models import Activity, FitnessMarker, SyncRun, WellnessDaily
from ..util import utcnow as _utcnow
from . import endpoints
from .client import GarminClient
from .weather import fetch_weather

logger = logging.getLogger(__name__)

FULL_IMPORT_DAYS = 730          # ~2 years
DETAIL_WINDOW_DAYS = 120        # splits/HR-zones fetched per-activity inside this window
INCREMENTAL_WELLNESS_DAYS = 7   # overnight data keeps updating; re-pull a week
ACTIVITY_PAGE_SIZE = 100

RUN_TYPE_PREFIXES = ("running", "trail_running", "treadmill_running", "track_running", "ultra_run")

_sync_lock = threading.Lock()


def _parse_gmt(s: str | None) -> datetime | None:
    if not s:
        return None
    return datetime.strptime(s, "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)


def _parse_local(s: str | None) -> datetime | None:
    if not s:
        return None
    return datetime.strptime(s, "%Y-%m-%d %H:%M:%S")


# ---------------------------------------------------------------- activities

def _upsert_activity(db: Session, item: dict, client: GarminClient, detail_cutoff: date) -> str:
    """Upsert one activity from the search-list payload. Returns 'new'|'updated'."""
    activity_id = item["activityId"]
    start_utc = _parse_gmt(item.get("startTimeGMT"))
    existing = db.get(Activity, activity_id)
    is_new = existing is None
    act = existing or Activity(id=activity_id)

    act.start_time_utc = start_utc or act.start_time_utc
    act.start_time_local = _parse_local(item.get("startTimeLocal")) or act.start_time_local
    act.activity_type = (item.get("activityType") or {}).get("typeKey") or act.activity_type or "unknown"
    act.name = item.get("activityName") or act.name
    act.distance_m = item.get("distance")
    act.duration_s = item.get("duration")
    act.elevation_gain_m = item.get("elevationGain")
    act.avg_hr = _as_int(item.get("averageHR"))
    act.max_hr = _as_int(item.get("maxHR"))
    act.avg_speed_mps = item.get("averageSpeed")
    act.avg_run_cadence = item.get("averageRunningCadenceInStepsPerMinute")
    act.calories = item.get("calories")
    act.aerobic_te = item.get("aerobicTrainingEffect")
    act.anaerobic_te = item.get("anaerobicTrainingEffect")
    act.vo2max = item.get("vO2MaxValue")
    act.raw = item
    act.synced_at = _utcnow()

    is_run = any(act.activity_type.startswith(p) for p in RUN_TYPE_PREFIXES)
    recent = start_utc is not None and start_utc.date() >= detail_cutoff
    if recent and not act.detail_synced:
        try:
            # The detail endpoint carries the self-evaluation (feel + RPE) — logged
            # on every activity type, so fetch it for all, not just runs.
            detail = client.api(endpoints.ACTIVITY_DETAIL.format(activity_id=activity_id))
            summary = (detail or {}).get("summaryDTO", {})
            act.feel = summary.get("directWorkoutFeel")
            act.rpe = summary.get("directWorkoutRpe")
            if is_run:
                act.laps = client.api(endpoints.ACTIVITY_SPLITS.format(activity_id=activity_id))
                act.hr_zones = client.api(endpoints.ACTIVITY_HR_ZONES.format(activity_id=activity_id))
            act.detail_synced = True
        except Exception as exc:
            # Detail is enrichment — never fail the whole sync over one activity.
            logger.warning("Detail fetch failed for activity %s: %s", activity_id, exc)
        # Weather at the run's start (outdoor/GPS activities only) — separate source,
        # best-effort, never blocks the sync.
        lat, lon = item.get("startLatitude"), item.get("startLongitude")
        if lat and lon and act.start_time_local and act.weather_temp_c is None:
            w = fetch_weather(lat, lon, act.start_time_local)
            if w:
                act.weather_temp_c, act.weather_humidity, act.weather_feels_c = (
                    w["temp_c"], w["humidity"], w["feels_c"])

    # Durability rollup from the per-second stream (runs only) — separate flag from
    # detail_synced so it can backfill existing runs independently. Best-effort.
    if is_run and recent and not act.streams_synced:
        try:
            from .streams import compute_stream_metrics
            details = client.api(endpoints.ACTIVITY_DETAILS.format(activity_id=activity_id))
            act.stream_metrics = compute_stream_metrics(details)
            act.streams_synced = True
        except Exception as exc:
            logger.warning("Stream metrics failed for activity %s: %s", activity_id, exc)

    if is_new:
        db.add(act)
    return "new" if is_new else "updated"


def _sync_activities(db: Session, client: GarminClient, since: date, detail_cutoff: date) -> dict:
    stats = {"new": 0, "updated": 0}
    start = 0
    while True:
        page = client.api(endpoints.ACTIVITIES_SEARCH, start=start, limit=ACTIVITY_PAGE_SIZE) or []
        if not page:
            break
        for item in page:
            start_utc = _parse_gmt(item.get("startTimeGMT"))
            if start_utc and start_utc.date() < since:
                db.flush()
                return stats
            stats[_upsert_activity(db, item, client, detail_cutoff)] += 1
        db.flush()
        if len(page) < ACTIVITY_PAGE_SIZE:
            break
        start += ACTIVITY_PAGE_SIZE
    return stats


# ------------------------------------------------------------------ wellness

def _as_int(v) -> int | None:
    return int(v) if isinstance(v, (int, float)) and v >= 0 else None


def _sync_wellness_day(db: Session, client: GarminClient, day: date, skip_existing: bool) -> bool:
    """Pull summary + sleep + HRV for one day. Returns True if a row was written."""
    existing = db.get(WellnessDaily, day)
    if existing is not None and skip_existing:
        return False

    day_s = day.isoformat()
    raw: dict = {}
    try:
        raw["summary"] = client.api(endpoints.DAILY_SUMMARY, calendarDate=day_s)
        raw["sleep"] = client.api(
            endpoints.SLEEP_DAILY.format(username=client.username),
            date=day_s, nonSleepBufferMinutes=60,
        )
        raw["hrv"] = client.api(endpoints.HRV_DAILY.format(date=day_s))
    except Exception as exc:
        # A single bad day must not sink a 730-day backfill.
        logger.warning("Wellness pull failed for %s: %s", day_s, exc)
        if not raw:
            return False

    summary = raw.get("summary") or {}
    sleep_dto = (raw.get("sleep") or {}).get("dailySleepDTO") or {}
    hrv_summary = (raw.get("hrv") or {}).get("hrvSummary") or {}

    row = existing or WellnessDaily(date=day)
    row.resting_hr = _as_int(summary.get("restingHeartRate"))
    row.stress_avg = _as_int(summary.get("averageStressLevel"))
    row.steps = _as_int(summary.get("totalSteps"))
    row.body_battery_high = _as_int(summary.get("bodyBatteryHighestValue"))
    row.body_battery_low = _as_int(summary.get("bodyBatteryLowestValue"))
    row.sleep_seconds = _as_int(sleep_dto.get("sleepTimeSeconds"))
    row.sleep_score = _as_int(((sleep_dto.get("sleepScores") or {}).get("overall") or {}).get("value"))
    row.sleep_stages = {
        k: sleep_dto.get(k)
        for k in ("deepSleepSeconds", "lightSleepSeconds", "remSleepSeconds", "awakeSleepSeconds")
        if sleep_dto.get(k) is not None
    } or None
    row.hrv_last_night_avg = _as_int(hrv_summary.get("lastNightAvg"))
    row.hrv_status = hrv_summary.get("status")
    # weight is filled by the range-based pass; keep whatever is already there
    row.raw = {**(existing.raw if existing else {}), **{k: v for k, v in raw.items() if v}}
    row.synced_at = _utcnow()
    if existing is None:
        db.add(row)
    return True


def _sync_weight_range(db: Session, client: GarminClient, start: date, end: date) -> int:
    """Weight/body-comp via range endpoint, applied onto wellness_daily rows."""
    count = 0
    payload = client.api(
        endpoints.WEIGHT_RANGE.format(start=start.isoformat(), end=end.isoformat()), includeAll=True
    ) or {}
    for day_summary in payload.get("dailyWeightSummaries") or []:
        day_s = day_summary.get("summaryDate")
        latest = day_summary.get("latestWeight") or {}
        if not day_s:
            continue
        day = date.fromisoformat(day_s)
        row = db.get(WellnessDaily, day)
        if row is None:
            row = WellnessDaily(date=day, raw={}, synced_at=_utcnow())
            db.add(row)
        weight_g = latest.get("weight")
        row.weight_kg = round(weight_g / 1000.0, 2) if isinstance(weight_g, (int, float)) else row.weight_kg
        bf = latest.get("bodyFat")
        row.body_fat_pct = bf if isinstance(bf, (int, float)) else row.body_fat_pct
        row.raw = {**(row.raw or {}), "weight": latest}
        row.synced_at = _utcnow()
        count += 1
    return count


# ------------------------------------------------------------ fitness markers

def _upsert_marker(db: Session, day: date, kind: str, value_num: float | None, value) -> None:
    row = db.scalar(
        select(FitnessMarker).where(FitnessMarker.date == day, FitnessMarker.kind == kind)
    )
    if row is None:
        row = FitnessMarker(date=day, kind=kind)
        db.add(row)
    row.value_num = value_num
    row.value = value
    row.synced_at = _utcnow()


def _sync_fitness_markers(db: Session, client: GarminClient, start: date, end: date) -> dict:
    stats = {"vo2max": 0, "race_prediction": 0, "training_status": 0}

    # VO2max curve — chunked range calls
    chunk_start = start
    while chunk_start <= end:
        chunk_end = min(chunk_start + timedelta(days=29), end)
        try:
            payload = client.api(
                endpoints.MAXMET_DAILY_RANGE.format(
                    start=chunk_start.isoformat(), end=chunk_end.isoformat()
                )
            ) or []
            for item in payload:
                generic = item.get("generic") or {}
                day_s = generic.get("calendarDate") or item.get("calendarDate")
                value = generic.get("vo2MaxPreciseValue") or generic.get("vo2MaxValue")
                if day_s and value:
                    _upsert_marker(db, date.fromisoformat(day_s), "vo2max_running", float(value), None)
                    stats["vo2max"] += 1
        except Exception as exc:
            logger.warning("VO2max range %s..%s failed: %s", chunk_start, chunk_end, exc)
        chunk_start = chunk_end + timedelta(days=1)

    # Race predictor — latest snapshot
    try:
        payload = client.api(
            endpoints.RACE_PREDICTIONS_LATEST.format(display_name=client.display_name)
        )
        if payload:
            _upsert_marker(db, date.today(), "race_prediction", None, payload)
            stats["race_prediction"] = 1
    except Exception as exc:
        logger.warning("Race predictions fetch failed: %s", exc)

    # Training status/load — current snapshot (date is a path segment)
    try:
        payload = client.api(endpoints.TRAINING_STATUS_AGGREGATED.format(date=end.isoformat()))
        if payload:
            _upsert_marker(db, end, "training_status", None, payload)
            stats["training_status"] = 1
    except Exception as exc:
        logger.warning("Training status fetch failed: %s", exc)

    # Training Readiness — daily 0-100 score (recent window only; it's a short-trend metric)
    stats["training_readiness"] = 0
    for offset in range(min(14, (end - start).days + 1)):
        day = end - timedelta(days=offset)
        try:
            payload = client.api(endpoints.TRAINING_READINESS.format(date=day.isoformat()))
            item = payload[0] if isinstance(payload, list) and payload else None
            if item and item.get("score") is not None:
                _upsert_marker(db, day, "training_readiness", float(item["score"]), item)
                stats["training_readiness"] += 1
        except Exception as exc:
            logger.warning("Training readiness %s failed: %s", day, exc)

    # Endurance & Hill scores — latest snapshot (ultra/trail-relevant fitness)
    for kind, path in (("endurance_score", endpoints.ENDURANCE_SCORE), ("hill_score", endpoints.HILL_SCORE)):
        try:
            payload = client.api(path.format(date=end.isoformat()))
            if payload and payload.get("overallScore") is not None:
                _upsert_marker(db, end, kind, float(payload["overallScore"]), payload)
                stats[kind] = 1
        except Exception as exc:
            logger.warning("%s fetch failed: %s", kind, exc)

    return stats


# ------------------------------------------------------------------ orchestration

def run_sync(kind: str = "incremental") -> SyncRun:
    """Entry point for all surfaces (API, Telegram, jobs). kind: 'full' | 'incremental'."""
    if not get_settings().garmin_sync_enabled:
        # Hybrid deploy: Render's IP is 429'd by Garmin, so ingestion runs from the home
        # machine. Every Garmin surface on Render becomes an honest no-op. Return a
        # TRANSIENT (unpersisted) run so this neither trips the staleness watchdog nor
        # logs a false failure — the home sync is the only thing that writes SyncRun rows.
        logger.info("Garmin sync disabled (GARMIN_SYNC_ENABLED=false) — skipping; home machine ingests.")
        return SyncRun(kind=kind, status="skipped", started_at=_utcnow(),
                       finished_at=_utcnow(), stats={"skipped": "garmin_sync_disabled"})
    if not _sync_lock.acquire(blocking=False):
        raise RuntimeError("A sync is already running")
    db = SessionLocal()
    run = SyncRun(kind=kind, status="running", started_at=_utcnow())
    db.add(run)
    db.commit()
    try:
        client = GarminClient(db=db)  # db → rotating OAuth2 refresh token is persisted
        today = date.today()
        stats: dict = {}

        if kind == "full":
            since = today - timedelta(days=FULL_IMPORT_DAYS)
            detail_cutoff = today - timedelta(days=DETAIL_WINDOW_DAYS)
            stats["activities"] = _sync_activities(db, client, since, detail_cutoff)
            db.commit()
            written = 0
            for offset in range(FULL_IMPORT_DAYS + 1):  # newest first — recent data lands early
                day = today - timedelta(days=offset)
                if _sync_wellness_day(db, client, day, skip_existing=True):
                    written += 1
                if offset % 30 == 29:
                    db.commit()
                    logger.info("Wellness backfill: %d/%d days", offset + 1, FULL_IMPORT_DAYS)
            stats["wellness_days"] = written
            db.commit()
            stats["weight_days"] = _sync_weight_range(db, client, since, today)
            stats["markers"] = _sync_fitness_markers(db, client, since, today)
        else:
            since = today - timedelta(days=INCREMENTAL_WELLNESS_DAYS)
            detail_cutoff = today - timedelta(days=DETAIL_WINDOW_DAYS)
            stats["activities"] = _sync_activities(db, client, since, detail_cutoff)
            written = 0
            for offset in range(INCREMENTAL_WELLNESS_DAYS + 1):
                if _sync_wellness_day(db, client, today - timedelta(days=offset), skip_existing=False):
                    written += 1
            stats["wellness_days"] = written
            stats["weight_days"] = _sync_weight_range(db, client, since, today)
            stats["markers"] = _sync_fitness_markers(db, client, today - timedelta(days=30), today)

        db.commit()
        run.status = "success"
        run.finished_at = _utcnow()
        run.stats = stats
        db.commit()
        # Link freshly-synced runs to planned sessions (layer 3). Best-effort:
        # a linking hiccup must never fail the sync itself.
        try:
            from ..plan.store import link_results
            linked = link_results(db)
            if linked:
                stats["results_linked"] = linked
                run.stats = stats
                db.commit()
        except Exception:
            logger.exception("Result linking failed (non-fatal)")
        # Fill weather for freshly-synced outdoor runs BEFORE the post-run read, so the coach
        # reads each run in its conditions. Best-effort, keyless, no Garmin — only
        # fetches recent GPS runs still missing weather, so it's a handful of calls at most.
        try:
            wx = backfill_weather(days=14)
            if wx.get("updated"):
                db.expire_all()  # let the post-run read see the just-written weather
                logger.info("Post-sync weather: %s", wx)
        except Exception:
            logger.exception("Post-sync weather backfill failed (non-fatal)")
        # Coaching layer: post-activity read + red-flag proactivity. All
        # best-effort — a coaching hiccup must never fail the underlying sync.
        try:
            from ..coach.adapt import post_sync_coaching
            post_sync_coaching(db)
        except Exception:
            logger.exception("Post-sync coaching failed (non-fatal)")
        logger.info("Sync %s finished: %s", kind, stats)
        return run
    except Exception as exc:
        db.rollback()
        run.status = "failure"
        run.finished_at = _utcnow()
        run.detail = f"{exc}\n{traceback.format_exc()[-1500:]}"
        db.commit()
        logger.exception("Sync %s failed", kind)
        # Degrade loudly, routed by cause: a rejected garth token gets an
        # actionable re-auth ping; anything else defers to the staleness watchdog.
        try:
            from .. import monitor
            from .client import GarminAuthError
            if isinstance(exc, GarminAuthError):
                monitor.alert_garmin_auth(db)
            else:
                monitor.check_stale(db)
        except Exception:
            logger.exception("Failure alerting itself failed")
        return run
    finally:
        db.close()
        _sync_lock.release()


def backfill_self_eval(days: int = 400, limit: int | None = None) -> dict:
    """One-off: pull feel/RPE from the detail endpoint for existing activities that
    predate self-eval capture. Idempotent — skips activities that already have it."""
    from datetime import date as _date
    db = SessionLocal()
    client = GarminClient()
    cutoff = _date.today() - timedelta(days=days)
    rows = db.scalars(
        select(Activity).where(
            Activity.start_time_utc >= datetime(cutoff.year, cutoff.month, cutoff.day, tzinfo=UTC),
            Activity.feel.is_(None), Activity.rpe.is_(None),
        ).order_by(Activity.start_time_utc.desc())
    ).all()
    updated = 0
    for a in rows:
        if limit and updated >= limit:
            break
        try:
            detail = client.api(endpoints.ACTIVITY_DETAIL.format(activity_id=a.id))
            summary = (detail or {}).get("summaryDTO", {})
            feel, rpe = summary.get("directWorkoutFeel"), summary.get("directWorkoutRpe")
            if feel is not None or rpe is not None:
                a.feel, a.rpe = feel, rpe
                updated += 1
        except Exception as exc:
            logger.warning("Self-eval backfill failed for activity %s: %s", a.id, exc)
    db.commit()
    db.close()
    logger.info("Self-eval backfill: %d activities updated", updated)
    return {"scanned": len(rows), "updated": updated}


def backfill_weather(days: int = 150, limit: int | None = None) -> dict:
    """One-off: fetch start-of-run weather for existing outdoor activities (those
    with GPS) that don't have it yet. Idempotent."""
    from datetime import date as _date
    db = SessionLocal()
    cutoff = _date.today() - timedelta(days=days)
    rows = db.scalars(
        select(Activity).where(
            Activity.start_time_utc >= datetime(cutoff.year, cutoff.month, cutoff.day, tzinfo=UTC),
            Activity.weather_temp_c.is_(None),
        ).order_by(Activity.start_time_utc.desc())
    ).all()
    updated = 0
    for a in rows:
        if limit and updated >= limit:
            break
        raw = a.raw or {}
        lat, lon = raw.get("startLatitude"), raw.get("startLongitude")
        if not lat or not lon or not a.start_time_local:
            continue
        w = fetch_weather(lat, lon, a.start_time_local)
        if w:
            a.weather_temp_c, a.weather_humidity, a.weather_feels_c = w["temp_c"], w["humidity"], w["feels_c"]
            updated += 1
            if updated % 20 == 0:
                db.commit()  # commit in batches — a 2-year run holds the DB connection
                             # open across many HTTP calls; a managed pooler would drop it
        time.sleep(0.3)  # be gentle on the free weather API
    db.commit()
    db.close()
    logger.info("Weather backfill: %d activities updated", updated)
    return {"scanned": len(rows), "updated": updated}


def backfill_streams(days: int = 400, limit: int | None = None) -> dict:
    """One-off: compute the durability rollup (decoupling / HR drift / pace CV) from the
    per-second stream for existing runs that predate stream capture. Idempotent — skips
    runs already marked streams_synced. Streams are large, so it's paced and capped."""
    from datetime import date as _date

    from .streams import compute_stream_metrics
    db = SessionLocal()
    client = GarminClient(db=db)
    cutoff = _date.today() - timedelta(days=days)
    rows = db.scalars(
        select(Activity).where(
            Activity.start_time_utc >= datetime(cutoff.year, cutoff.month, cutoff.day, tzinfo=UTC),
            Activity.streams_synced.is_(False),
        ).order_by(Activity.start_time_utc.desc())
    ).all()
    runs = [a for a in rows if a.activity_type and any(a.activity_type.startswith(p) for p in RUN_TYPE_PREFIXES)]
    updated = 0
    for a in runs:
        if limit and updated >= limit:
            break
        try:
            details = client.api(endpoints.ACTIVITY_DETAILS.format(activity_id=a.id))
            a.stream_metrics = compute_stream_metrics(details)
            a.streams_synced = True
            if a.stream_metrics:
                updated += 1
            if updated % 25 == 0:
                db.commit()
        except Exception as exc:
            logger.warning("Stream backfill failed for activity %s: %s", a.id, exc)
        time.sleep(0.3)  # streams are heavy — be gentle on Garmin
    db.commit()
    db.close()
    logger.info("Stream backfill: %d runs with metrics", updated)
    return {"scanned": len(runs), "updated": updated}


def sync_status_summary(db: Session) -> dict:
    """For GET /api/sync/status and the Telegram /status command."""
    now = _utcnow()
    last_success_at = db.scalar(
        select(func.max(SyncRun.finished_at)).where(SyncRun.status == "success")
    )
    latest = db.scalar(select(SyncRun).order_by(SyncRun.id.desc()).limit(1))
    return {
        "running": latest.status == "running" if latest else False,
        "last_run": {
            "kind": latest.kind,
            "status": latest.status,
            "started_at": latest.started_at.isoformat(),
            "finished_at": latest.finished_at.isoformat() if latest.finished_at else None,
            "stats": latest.stats,
            "detail": latest.detail,
        }
        if latest
        else None,
        "last_success_at": last_success_at.isoformat() if last_success_at else None,
        "staleness_hours": round((now - last_success_at).total_seconds() / 3600, 1)
        if last_success_at
        else None,
        # False on Render (hybrid deploy): ingestion runs from the home PC, so the web
        # "Sync now" button is informational — the UI reads this to say so honestly.
        "garmin_sync_enabled": get_settings().garmin_sync_enabled,
    }
