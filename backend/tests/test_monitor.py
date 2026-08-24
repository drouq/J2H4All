"""The watchdog. Untested until 2026-08-03, which is the wrong way round for this
module in particular: its output IS silence, so a broken monitor is indistinguishable
from a healthy system until something has been quietly dead for weeks. (The monthly
Drive backup failed on 2026-08-01 and nobody noticed for two days — and that one DID
alert.) These lock the two properties everything else assumes: it fires when it should,
and the cooldown stops it firing again.
"""
from datetime import timedelta

import pytest

from app import monitor
from app.models import Preference, ScheduledJobRun, SyncRun
from app.util import utcnow


@pytest.fixture
def sent(monkeypatch):
    out = []
    monkeypatch.setattr(monitor, "_send", out.append)
    return out


def _sync(db, hours_ago, status="success"):
    t = utcnow() - timedelta(hours=hours_ago)
    db.add(SyncRun(kind="incremental", status=status, started_at=t, finished_at=t))
    db.commit()


# ------------------------------------------------------------------ staleness watchdog

def test_silent_when_a_sync_succeeded_recently(db, sent):
    _sync(db, hours_ago=2)
    assert monitor.check_stale(db) is False
    assert sent == []


def test_alerts_once_a_sync_goes_stale(db, sent):
    _sync(db, hours_ago=monitor.STALE_ALERT_HOURS + 1)
    assert monitor.check_stale(db) is True
    assert len(sent) == 1 and "No successful Garmin sync" in sent[0]


def test_cooldown_stops_a_persistent_fault_pinging_every_tick(db, sent):
    """The tick runs hourly; without the cooldown a dead cron would send 24 messages
    a day and get muted, which is the failure mode this whole module exists to avoid."""
    _sync(db, hours_ago=monitor.STALE_ALERT_HOURS + 1)
    assert monitor.check_stale(db) is True
    for _ in range(5):
        assert monitor.check_stale(db) is False
    assert len(sent) == 1


def test_cooldown_expires_so_a_lasting_fault_is_re_raised(db, sent):
    _sync(db, hours_ago=monitor.STALE_ALERT_HOURS + 1)
    monitor.check_stale(db)
    stamp = db.scalar(
        __import__("sqlalchemy").select(Preference).where(Preference.key == "alert_stale"))
    stamp.value = (utcnow() - timedelta(hours=25)).isoformat()   # yesterday's alert
    db.commit()
    assert monitor.check_stale(db) is True
    assert len(sent) == 2


def test_a_brand_new_install_is_not_a_fault(db, sent):
    """Nothing attempted yet — must not cry wolf on first deploy."""
    assert monitor.check_stale(db) is False
    assert sent == []


def test_a_sync_that_has_never_succeeded_alerts(db, sent):
    """The gap that would otherwise be permanently silent: syncs are ATTEMPTED and
    all fail, so there's no `finished_at` to measure staleness from."""
    _sync(db, hours_ago=monitor.STALE_ALERT_HOURS + 5, status="error")
    assert monitor.check_stale(db) is True
    assert "NEVER completed successfully" in sent[0]


def test_a_recent_run_of_failures_waits_before_shouting(db, sent):
    _sync(db, hours_ago=3, status="error")
    assert monitor.check_stale(db) is False
    assert sent == []


def test_a_corrupt_cooldown_stamp_does_not_suppress_the_alert(db, sent):
    """Fail open: an unparseable stamp must not silence the watchdog forever."""
    db.add(Preference(key="alert_stale", value="not-a-timestamp", updated_at=utcnow()))
    db.commit()
    _sync(db, hours_ago=monitor.STALE_ALERT_HOURS + 1)
    assert monitor.check_stale(db) is True


# ------------------------------------------------------------------ other alerts

def test_cron_failure_alerts_per_job(db, sent):
    assert monitor.alert_cron_failure(db, "monthly_export", RuntimeError("drive 403")) is True
    assert "monthly_export" in sent[0] and "drive 403" in sent[0]
    # Same job again inside the window: quiet. A DIFFERENT job still gets through.
    assert monitor.alert_cron_failure(db, "monthly_export", RuntimeError("drive 403")) is False
    assert monitor.alert_cron_failure(db, "weekly_review", RuntimeError("boom")) is True
    assert len(sent) == 2


def test_cron_failure_alerting_never_raises(db, monkeypatch):
    """It's called from inside exception handlers — if it can throw, it converts a
    contained failure into a lost job."""
    def _boom(_text):
        raise RuntimeError("telegram down")
    monkeypatch.setattr(monitor, "_send", _boom)
    assert monitor.alert_cron_failure(db, "tick", RuntimeError("x")) is False


def test_garmin_auth_alert_is_actionable_and_throttled(db, sent):
    assert monitor.alert_garmin_auth(db) is True
    assert "garth" in sent[0] and "app.garmin.login" in sent[0]
    assert monitor.alert_garmin_auth(db) is False


# ------------------------------------------------------------------ weekly heartbeat

def test_heartbeat_reports_sync_age_and_beat_count(db):
    _sync(db, hours_ago=5)
    for i in range(3):
        db.add(ScheduledJobRun(job=f"j{i}", ran_on=(utcnow() - timedelta(days=i)).date(),
                               created_at=utcnow()))
    db.commit()
    line = monitor.health_summary(db)
    assert "5h ago" in line and "3 beats" in line and "STALE" not in line


def test_heartbeat_flags_a_stale_sync(db):
    _sync(db, hours_ago=monitor.STALE_ALERT_HOURS + 2)
    assert "STALE" in monitor.health_summary(db)


def test_heartbeat_survives_a_system_that_has_never_synced(db):
    assert "no successful sync yet" in monitor.health_summary(db)


# ------------------------------------------------------------- the watch
# When the workout push broke in the reference deployment it stayed broken for weeks:
# its only signal was a parenthetical on the approval card, so every plan change
# silently stopped reaching the watch while plan, calendar and app all looked right. Two alerts
# cover the two ways that happens - a push that FAILS, and a push never ATTEMPTED.

@pytest.fixture
def push_on(monkeypatch):
    """The workout push is feature-flagged OFF by default, so without this every
    watch-staleness assertion would pass for the wrong reason (lag returns None on a
    disabled push, which looks identical to a healthy watch)."""
    from app.config import get_settings
    s = get_settings()
    monkeypatch.setattr(s, "garmin_workout_push_enabled", True)
    monkeypatch.setattr(s, "garmin_sync_enabled", True)
    return s


def _planned_run(db, *, days_ahead=2, created_hours_ago=0.0, title="Easy Aerobic - 60 min Z2"):
    from datetime import date as _date

    from app.models import Session as PlannedSession
    t = utcnow() - timedelta(hours=created_hours_ago)
    s = PlannedSession(date=_date.today() + timedelta(days=days_ahead), type="easy",
                       title=title, purpose="", status="planned", created_at=t, updated_at=t)
    db.add(s)
    db.commit()
    return s


def _pushed_at(db, hours_ago):
    t = utcnow() - timedelta(hours=hours_ago)
    db.add(Preference(key="last_push_garmin_at", value=t.isoformat(), updated_at=t))
    db.commit()


def test_push_failure_alerts_and_then_holds_its_cooldown(db, sent):
    assert monitor.alert_workout_push_failed(db, "HTTP Error 404: ") is True
    assert len(sent) == 1
    assert "watch" in sent[0].lower() and "404" in sent[0]
    # A second approval minutes later must not ping again for the same fault.
    assert monitor.alert_workout_push_failed(db, "HTTP Error 404: ") is False
    assert len(sent) == 1


def test_push_failure_alert_never_raises(db, sent, monkeypatch):
    """It runs inside an approval that has ALREADY been applied — it must not be able
    to turn a successful approval into an exception."""
    def _boom(_):
        raise RuntimeError("telegram down")
    monkeypatch.setattr(monitor, "_send", _boom)
    assert monitor.alert_workout_push_failed(db, "anything") is False


def test_silent_when_the_watch_has_the_current_plan(db, sent, push_on):
    _pushed_at(db, hours_ago=1)                      # pushed AFTER the session was created
    _planned_run(db, created_hours_ago=30)
    assert monitor.workout_push_lag(db) is None
    assert monitor.check_workout_push_stale(db) is False
    assert sent == []


def test_silent_during_the_grace_window(db, sent, push_on):
    """A session approved minutes ago is not yet a fault — its own push may still be
    landing, or retrying."""
    _pushed_at(db, hours_ago=48)
    _planned_run(db, created_hours_ago=monitor.WORKOUT_PUSH_LAG_HOURS - 1)
    assert monitor.check_workout_push_stale(db) is False
    assert sent == []


def test_alerts_when_the_plan_moved_but_the_watch_did_not(db, sent, push_on):
    _pushed_at(db, hours_ago=48)
    _planned_run(db, created_hours_ago=monitor.WORKOUT_PUSH_LAG_HOURS + 2,
                 title="Long Run - 2h30 Z2")
    assert monitor.check_workout_push_stale(db) is True
    assert len(sent) == 1
    assert "Long Run - 2h30 Z2" in sent[0]
    assert monitor.check_workout_push_stale(db) is False   # once a day, not every tick
    assert len(sent) == 1


def test_alerts_when_there_has_never_been_a_push(db, sent, push_on):
    """No stamp at all is the from-scratch version of the same fault."""
    _planned_run(db, created_hours_ago=monitor.WORKOUT_PUSH_LAG_HOURS + 2)
    assert monitor.check_workout_push_stale(db) is True
    assert len(sent) == 1


def test_gym_and_rest_never_count_as_unpushed(db, sent, push_on):
    """Only run sessions are pushed, so a gym-only week must not read as a stale watch."""
    from datetime import date as _date

    from app.models import Session as PlannedSession
    t = utcnow() - timedelta(hours=monitor.WORKOUT_PUSH_LAG_HOURS + 5)
    for stype in ("strength", "rest"):
        db.add(PlannedSession(date=_date.today() + timedelta(days=1), type=stype, title=stype,
                              purpose="", status="planned", created_at=t, updated_at=t))
    db.commit()
    _pushed_at(db, hours_ago=48)
    assert monitor.workout_push_lag(db) is None
    assert sent == []


def test_quiet_when_the_push_is_switched_off(db, sent, monkeypatch):
    """A deliberate flag-off already says so on the approval card; a daily ping about
    a setting they chose would just train them to ignore the alert."""
    from app.config import get_settings
    s = get_settings()
    monkeypatch.setattr(s, "garmin_workout_push_enabled", False)
    _planned_run(db, created_hours_ago=monitor.WORKOUT_PUSH_LAG_HOURS + 2)
    assert monitor.workout_push_lag(db) is None
    assert sent == []
