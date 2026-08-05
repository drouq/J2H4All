"""Job entrypoints for schedulers (Render cron later; Windows Task Scheduler or
manual runs while developing locally).

    python -m app.jobs daily_sync     # incremental Garmin pull (PRD §7 daily-morning)
    python -m app.jobs full_import    # one-off ~2-year bootstrap (PRD §14)
    python -m app.jobs tick           # local-clock dispatcher — run frequently (~hourly)
    python -m app.jobs morning_brief  # force a beat now (manual/testing)
    python -m app.jobs daily_debrief  # feel + lifestyle (aliases: daily_checkin, lifestyle_log)
    python -m app.jobs weekly_review
    python -m app.jobs monthly_export # dump full state to Google Drive (PRD §15)
"""

import logging
import sys

from sqlalchemy import text

from .db import engine

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def noop() -> None:
    with engine.connect() as conn:
        conn.execute(text("SELECT 1"))
    logger.info("J2H4All cron placeholder ran; DB reachable.")


def _push_calendar_after_daily_sync() -> None:
    """After the daily Garmin sync has linked completed runs (PRD §9), reflect them
    on the Google Calendar — mark ✅ done and re-assert the approved plan. Runs on
    the j2h4all-cron service, which therefore needs the GOOGLE_* env (client id/secret +
    refresh token). Best-effort: never fails the sync job. Degrades loudly (PRD §4)
    with a one-a-day Telegram ping if the reconcile skipped/errored — most likely
    those creds are missing on the cron, so completed runs aren't being marked.

    PRODUCTION-ONLY — hard guard, learned the hard way (2026-07-23): a forgotten
    local Scheduled Task ran daily_sync against the STALE dev-mirror DB, and because
    the calendar token resolves from the DB's oauth_credential row, its reconcile
    rewrote the PROD Google Calendar from a weeks-old plan every morning — deleting
    current events and reviving old ones. A dev-environment sync must never be able
    to touch the live calendar, whatever DB or task invokes it."""
    from .config import get_settings
    if not get_settings().is_production:
        logger.info("Post-daily-sync calendar reconcile skipped: not production "
                    "(a dev-run sync must never write the live calendar)")
        return
    from .calendar.sync import safe_reconcile
    from .db import SessionLocal
    db = SessionLocal()
    try:
        result = safe_reconcile(db)
        logger.info("Post-daily-sync calendar reconcile: %s", result)
        if "error" in result or "skipped" in result:
            reason = result.get("error") or result.get("skipped")
            try:
                from .telegram import send_message_sync
                send_message_sync(
                    "⚠️ Daily calendar sync didn't run after the Garmin sync "
                    f"({reason}). Completed runs may not be marked ✅ on the calendar."
                )
            except Exception:
                logger.exception("Failed to send calendar-reconcile alert")
    except Exception:
        logger.exception("Post-daily-sync calendar reconcile crashed (non-fatal)")
    finally:
        db.close()


def _run_coach(job: str) -> int:
    from .coach import adapt, schedule
    from .db import SessionLocal
    db = SessionLocal()
    try:
        if job == "tick":
            fired = schedule.run_tick(db)
            logger.info("tick fired: %s", fired or "nothing due")
        elif job == "morning_brief":
            adapt.send_morning_brief(db, schedule.local_today(db))  # their local day, not UTC
        elif job in ("daily_debrief", "daily_checkin", "lifestyle_log"):
            adapt.send_daily_debrief(db)  # old names alias the merged debrief
        elif job == "weekly_review":
            adapt.run_weekly_review(db, schedule.local_today(db))
    finally:
        db.close()
    return 0


def main() -> int:
    job = sys.argv[1] if len(sys.argv) > 1 else "noop"
    if job == "noop":
        noop()
        return 0
    if job in ("daily_sync", "full_import"):
        from .garmin.sync import run_sync

        run = run_sync("incremental" if job == "daily_sync" else "full")
        logger.info("Job %s finished: %s %s", job, run.status, run.stats)
        if job == "daily_sync" and run.status == "success":
            _push_calendar_after_daily_sync()
        return 0 if run.status == "success" else 1
    if job == "backfill_eval":
        from .garmin.sync import backfill_self_eval

        logger.info("Backfill self-eval: %s", backfill_self_eval())
        return 0
    if job in ("backfill_weather", "backfill_streams"):
        # Optional window override, e.g. `python -m app.jobs backfill_streams 730` to reach
        # the full 2-year history (defaults cover a shorter recent window). Both are idempotent.
        days = int(sys.argv[2]) if len(sys.argv) > 2 else None
        if job == "backfill_weather":
            from .garmin.sync import backfill_weather

            result = backfill_weather(days=days) if days else backfill_weather()
            logger.info("Backfill weather (days=%s): %s", days or "default", result)
        else:
            from .garmin.sync import backfill_streams

            result = backfill_streams(days=days) if days else backfill_streams()
            logger.info("Backfill streams (days=%s): %s", days or "default", result)
        return 0
    if job in ("tick", "morning_brief", "daily_debrief", "daily_checkin", "lifestyle_log", "weekly_review"):
        return _run_coach(job)
    if job == "monthly_export":
        from .backup import run_export
        from .db import SessionLocal
        db = SessionLocal()
        try:
            info = run_export(db)
            logger.info("Monthly export: %s", info)
        except Exception as exc:
            logger.exception("Monthly export failed")
            from . import monitor
            monitor.alert_cron_failure(db, "monthly_export", exc)
            return 1
        finally:
            db.close()
        return 0
    logger.error("Unknown job: %s", job)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
