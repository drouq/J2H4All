"""Local-clock dispatcher: a fixed-interval cron can't know the user's
local morning, so a frequent `tick` computes their local time (from user_state.timezone)
and fires each beat at its configured local hour, at most once per local day.

- morning brief  → daily at morning_brief_hour:morning_brief_minute (10:00 local —
  after the daily sync lands, so it reads last night's recovery)
- daily debrief  → daily at daily_debrief_hour (22:00 — feel + life factors, merged)
- weekly review  → Sunday at weekly_review_hour (23:00)

(If a beat is ever configured on a half-hour, run the tick cron denser than hourly.)
"""

import logging
from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession

from ..config import get_settings
from ..context.store import get_or_create_state
from ..models import ScheduledJobRun
from ..util import utcnow as _utcnow

logger = logging.getLogger(__name__)


def _already_ran(db: DbSession, job: str, local_day: date) -> bool:
    return db.scalar(
        select(ScheduledJobRun).where(ScheduledJobRun.job == job, ScheduledJobRun.ran_on == local_day)
    ) is not None


def _mark_ran(db: DbSession, job: str, local_day: date) -> bool:
    """Claim the (job, day) slot. Returns False if another tick already claimed it."""
    db.add(ScheduledJobRun(job=job, ran_on=local_day, created_at=_utcnow()))
    try:
        db.commit()
        return True
    except Exception:  # unique-constraint race → someone else fired it
        db.rollback()
        return False


def local_tz(db: DbSession) -> ZoneInfo:
    """The athlete's configured zone (set by chat — 'I'm in London').
    Falls back to UTC on an unset/bogus value rather than raising."""
    tz = get_or_create_state(db).timezone or "UTC"
    try:
        return ZoneInfo(tz)
    except Exception:
        logger.warning("Unknown timezone %r in user_state; falling back to UTC", tz)
        return ZoneInfo("UTC")


def local_now(db: DbSession) -> datetime:
    return _utcnow().astimezone(local_tz(db))


def to_local(db: DbSession, dt: datetime) -> datetime:
    """Render a stored UTC timestamp on the athlete's clock (store UTC,
    render local). Any time shown to them must go through this — a bare UTC hour
    reads up to a day wrong in a far-from-UTC zone. Naive input is assumed UTC (SQLite
    round-trips DateTime(timezone=True) as naive)."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(local_tz(db))


def fmt_local(db: DbSession, dt: datetime) -> str:
    """Human/LLM-facing local timestamp, e.g. '2026-07-16 09:58 (+08)'."""
    loc = to_local(db, dt)
    return f"{loc.strftime('%Y-%m-%d %H:%M')} ({loc.strftime('%z')[:3]})"


def local_today(db: DbSession) -> date:
    """The user's LOCAL calendar date. Any coaching surface that reasons
    about 'today' must use this, not date.today() — on Render (UTC) the server day
    lags the athlete's evening by several hours."""
    return local_now(db).date()


# How many hours past its scheduled local time a beat may still fire. Exact-hour
# matching used to mean a single skipped or delayed tick silently lost that day's
# beat forever (Render crons can slip). A beat now fires on the FIRST tick at or
# after its slot, within this window — the once-per-local-day claim (`_already_ran`
# / `_mark_ran`) still prevents a repeat. Bounded, and never allowed to cross
# midnight (see `_due_now`): a long outage must not deliver a "morning" brief in
# the evening, and the once-per-day claim must stay keyed to the right local date.
CATCHUP_HOURS = 3


def _due_now(now: datetime, hour: int, minute: int = 0) -> bool:
    """True when `now` is at/after the scheduled local time and still inside the
    catch-up window. `hour + CATCHUP_HOURS` is not wrapped, so a late-evening beat
    simply runs out of day rather than spilling into tomorrow."""
    return (now.hour, now.minute) >= (hour, minute) and now.hour < hour + CATCHUP_HOURS


def run_tick(db: DbSession, now_local: datetime | None = None) -> list[str]:
    """Fire any beats due at (or shortly after) their scheduled local time.
    Returns the jobs fired."""
    settings = get_settings()
    now = now_local or local_now(db)
    today = now.date()
    fired: list[str] = []

    due = []
    if _due_now(now, settings.morning_brief_hour, settings.morning_brief_minute):
        due.append("morning_brief")
    if _due_now(now, settings.daily_debrief_hour):
        due.append("daily_debrief")
    if now.weekday() == 6 and _due_now(now, settings.weekly_review_hour):  # Sunday
        due.append("weekly_review")

    from .. import monitor
    from . import adapt
    for job in due:
        if _already_ran(db, job, today) or not _mark_ran(db, job, today):
            continue
        try:
            if job == "morning_brief":
                adapt.send_morning_brief(db, today)
            elif job == "daily_debrief":
                adapt.send_daily_debrief(db)
            elif job == "weekly_review":
                adapt.run_weekly_review(db, today)
            fired.append(job)
            logger.info("Fired scheduled beat: %s (local %s)", job, now.isoformat())
        except Exception as exc:
            logger.exception("Scheduled beat %s failed", job)
            monitor.alert_cron_failure(db, job, exc)

    # Independent staleness watchdog: the tick fires regardless of sync health, so this
    # catches a dead sync cron (not just a failed sync attempt). Never break the tick.
    try:
        monitor.check_stale(db)
    except Exception:
        logger.exception("Staleness watchdog failed")
    return fired
