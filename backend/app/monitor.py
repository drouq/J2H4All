"""Operational monitoring (reliability hardening). J2H4All runs unattended on Render, so
a silent failure — a dead cron, an expired garth token, a broken backup — must surface
to Telegram rather than just quietly stop the coaching.

Design: alerts are cooldown-throttled (per-kind) via Preference keys so a persistent
fault pings at most once per window, not every tick. A weekly heartbeat rides the
Sunday review card so that *silence reliably means healthy*, not "notifications dead".
"""

import logging
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session as DbSession

from .models import Preference, ScheduledJobRun, SyncRun
from .util import as_utc as _as_utc
from .util import utcnow as _utcnow

logger = logging.getLogger(__name__)

# No successful sync in this long → data is stale enough to speak up.
STALE_ALERT_HOURS = 30


def _on_cooldown(db: DbSession, key: str, hours: float) -> bool:
    """True if we already alerted `key` within `hours`; otherwise stamp now and return
    False (i.e. 'go ahead and alert'). Cooldown state lives in the Preference table so
    it survives restarts and needs no schema change."""
    pref = db.scalar(select(Preference).where(Preference.key == key))
    now = _utcnow()
    if pref and pref.value:
        try:
            if (now - _as_utc(datetime.fromisoformat(pref.value))) < timedelta(hours=hours):
                return True
        except ValueError:
            pass  # corrupt stamp → treat as expired, re-stamp below
    if pref is None:
        db.add(Preference(key=key, value=now.isoformat(), updated_at=now))
    else:
        pref.value = now.isoformat()
        pref.updated_at = now
    db.commit()
    return False


def _last_success_at(db: DbSession) -> datetime | None:
    return _as_utc(
        db.scalar(select(func.max(SyncRun.finished_at)).where(SyncRun.status == "success")))


def sync_stale_hours(db: DbSession) -> float | None:
    """Hours since the last successful sync, or None if there's never been one."""
    last = _last_success_at(db)
    return None if last is None else (_utcnow() - last).total_seconds() / 3600


def _send(text: str) -> None:
    from .telegram import send_message_sync  # lazy: avoid import cycle
    send_message_sync(text)


def check_stale(db: DbSession) -> bool:
    """Independent staleness watchdog. Runs from the hourly tick (which fires regardless
    of sync health), so it catches a *dead cron* — not just a failed sync attempt. Pings
    at most once/day. Returns True if it alerted."""
    hrs = sync_stale_hours(db)
    if hrs is None:
        # Cold-start gap: syncs have been ATTEMPTED but none has EVER succeeded.
        # Without this branch a from-first-deploy failure is permanently silent.
        first = _as_utc(db.scalar(select(func.min(SyncRun.started_at))))
        if first is None:
            return False  # brand-new install, nothing attempted yet — not a fault
        attempted_hrs = (_utcnow() - first).total_seconds() / 3600
        if attempted_hrs < STALE_ALERT_HOURS or _on_cooldown(db, "alert_stale", 24):
            return False
        _send(
            f"⚠️ Garmin sync has NEVER completed successfully (first attempt {attempted_hrs:.0f}h ago). "
            f"I'm coaching without any synced data — the sync config or the Garmin token needs a look."
        )
        logger.warning("Never-succeeded sync alert sent (%.0fh since first attempt)", attempted_hrs)
        return True
    if hrs < STALE_ALERT_HOURS:
        return False
    if _on_cooldown(db, "alert_stale", 24):
        return False
    last = _last_success_at(db)
    from .coach.schedule import fmt_local
    when = fmt_local(db, last) if last else "?"  # their local clock, never raw server UTC
    _send(
        f"⚠️ No successful Garmin sync since {when} ({hrs:.0f}h ago). Recovery/activity data is "
        f"stale — I'll keep working off stored numbers and flag anything that leans on them. If this "
        f"keeps up, the sync cron or the Garmin token likely needs a look."
    )
    logger.warning("Stale-sync alert sent (%.0fh)", hrs)
    return True


def alert_garmin_auth(db: DbSession) -> bool:
    """Actionable garth re-auth ping — distinct from the generic staleness message, so a
    dead token tells you exactly what to do. Pings at most once/day."""
    if _on_cooldown(db, "alert_garmin_auth", 24):
        return False
    _send(
        "🔑 Garmin sign-in was rejected — the stored garth token needs refreshing. Run "
        "`python -m app.garmin.login` locally and update GARTH_TOKEN on Render so syncs resume."
    )
    logger.warning("Garmin-auth alert sent")
    return True


def alert_workout_push_failed(db: DbSession, detail: object) -> bool:
    """Ping when a Garmin workout push fails, at the moment it fails.

    The push is the one surface whose output is read on a device the app never sees: the
    watch. When it broke in the reference deployment the only signal was a parenthetical
    on the approval card ("Garmin workout push issue: HTTP Error 404:") appended to an
    otherwise successful message — so it went unnoticed for over two weeks while every
    plan change stopped reaching the watch. A failure that only whispers is a failure
    nobody hears.

    12h cooldown — a push happens per approval, so a bad afternoon could otherwise ping
    several times for one cause. Best-effort: never let alerting break an approval that
    has already been applied."""
    try:
        if _on_cooldown(db, "alert_workout_push", 12):
            return False
        first_line = str(detail).splitlines()[0][:200] if str(detail) else "unknown error"
        _send(
            f"⌚ Couldn't update your watch with the new plan: {first_line}\n"
            f"The plan, calendar and app are fine — it's only the watch that's behind, so "
            f"treat what's on it as the OLD plan until this clears."
        )
        logger.warning("Workout-push failure alert sent: %s", first_line)
        return True
    except Exception:
        logger.exception("alert_workout_push_failed itself failed")
        return False


# The plan may legitimately sit un-pushed for a while (no approvals, no changes), so a
# fixed staleness window like the sync's would be meaningless here. What IS meaningful:
# the stored plan moved and the watch never caught up. This grace lets an approval's own
# push land (and a retry follow) before we call it a problem.
WORKOUT_PUSH_LAG_HOURS = 6


def workout_push_lag(db: DbSession) -> tuple[float, str] | None:
    """(hours, session title) for the oldest future pushable session created after the
    last successful watch push — i.e. plan the watch has not been told about. None when
    the watch is current, when nothing is pushable, or when the push is switched off.

    Deliberately compares against the PLAN rather than the clock: it self-clears the
    moment a push succeeds, and stays quiet through a week with no plan changes."""
    from .config import get_settings
    from .garmin.workouts import PUSH_TYPES  # the one authoritative pushable set
    from .models import Session as PlannedSession

    settings = get_settings()
    if not (settings.garmin_workout_push_enabled and settings.garmin_sync_enabled):
        return None  # switched off on purpose — the approval card already says so
    from .coach.schedule import local_today
    pushable = sorted(PUSH_TYPES)
    stamp = db.scalar(select(Preference).where(Preference.key == "last_push_garmin_at"))
    try:
        pushed_at = _as_utc(datetime.fromisoformat(stamp.value)) if stamp and stamp.value else None
    except ValueError:
        pushed_at = None
    rows = db.scalars(
        select(PlannedSession).where(
            PlannedSession.status == "planned",
            PlannedSession.type.in_(pushable),
            PlannedSession.date >= local_today(db),
        ).order_by(PlannedSession.created_at)
    ).all()
    unpushed = [s for s in rows if pushed_at is None or _as_utc(s.created_at) > pushed_at]
    if not unpushed:
        return None
    oldest = unpushed[0]
    return (_utcnow() - _as_utc(oldest.created_at)).total_seconds() / 3600, oldest.title or "a session"


def check_workout_push_stale(db: DbSession) -> bool:
    """Watchdog for a plan the watch never received. Runs from the hourly tick, so it
    catches the case `alert_workout_push_failed` cannot: a push never ATTEMPTED (a flag
    drifted off, a path that silently skipped it). Once/day."""
    lag = workout_push_lag(db)
    if lag is None:
        return False
    hrs, title = lag
    if hrs < WORKOUT_PUSH_LAG_HOURS:
        return False
    if _on_cooldown(db, "alert_workout_push_stale", 24):
        return False
    _send(
        f"⌚ Your watch hasn't been sent the current plan — \"{title}\" has been waiting "
        f"{hrs:.0f}h. What's on the watch is the OLD plan; the app and calendar are right. "
        f"Re-approving or tapping \"Push plan\" should clear it."
    )
    logger.warning("Workout-push staleness alert sent (%.0fh, %s)", hrs, title)
    return True


def alert_cron_failure(db: DbSession, job: str, err: object) -> bool:
    """Ping when a scheduled job/beat fails unexpectedly. Per-job cooldown (12h) so a
    repeatedly-failing beat doesn't spam. Best-effort: never let alerting raise."""
    try:
        if _on_cooldown(db, f"alert_cron_{job}", 12):
            return False
        first_line = str(err).splitlines()[0][:200] if str(err) else "unknown error"
        _send(f"⚠️ J2H4All job '{job}' failed: {first_line}")
        logger.warning("Cron-failure alert sent for %s", job)
        return True
    except Exception:
        logger.exception("alert_cron_failure itself failed for %s", job)
        return False


def health_summary(db: DbSession) -> str:
    """One-line liveness string for the weekly heartbeat (appended to the Sunday review
    card). Silence-means-healthy only works if you periodically see proof of life."""
    hrs = sync_stale_hours(db)
    if hrs is None:
        sync_txt = "no successful sync yet"
    else:
        sync_txt = f"last sync {hrs:.0f}h ago" + (" — ⚠️ STALE" if hrs >= STALE_ALERT_HOURS else "")
    week_ago = (_utcnow() - timedelta(days=7)).date()
    beats = db.scalar(
        select(func.count(ScheduledJobRun.id)).where(ScheduledJobRun.ran_on >= week_ago)
    ) or 0
    return f"🩺 Systems OK — {sync_txt}; {beats} beats fired this week."
