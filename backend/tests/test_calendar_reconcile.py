"""safe_reconcile — the best-effort wrapper used by the approval path AND the daily
Garmin-sync cron (so completed runs get marked ✅ without waiting for an approval).
It must never raise and must short-circuit when the calendar isn't connected."""
from app.calendar import oauth as cal_oauth
from app.calendar import sync as cal_sync


def test_skips_when_not_connected(db, monkeypatch):
    called = {"reconcile": False}

    def _reconcile(_db):
        called["reconcile"] = True
        return {"created": 1}

    monkeypatch.setattr(cal_oauth, "is_connected", lambda _db: False)
    monkeypatch.setattr(cal_sync, "reconcile", _reconcile)
    result = cal_sync.safe_reconcile(db)
    assert result == {"skipped": "calendar not connected"}
    assert called["reconcile"] is False  # never hit Google when disconnected


def test_passes_through_reconcile_result(db, monkeypatch):
    monkeypatch.setattr(cal_oauth, "is_connected", lambda _db: True)
    monkeypatch.setattr(cal_sync, "reconcile",
                        lambda _db: {"created": 2, "updated": 3, "completed_marked": 1})
    result = cal_sync.safe_reconcile(db)
    assert result["created"] == 2 and result["completed_marked"] == 1


def test_calendar_not_connected_midflight_is_caught(db, monkeypatch):
    def _raise(_db):
        raise cal_oauth.CalendarNotConnected("Refresh token rejected — reconnect Google Calendar")

    monkeypatch.setattr(cal_oauth, "is_connected", lambda _db: True)
    monkeypatch.setattr(cal_sync, "reconcile", _raise)
    result = cal_sync.safe_reconcile(db)
    assert "error" in result and "reconnect" in result["error"].lower()


def test_generic_error_is_swallowed_not_raised(db, monkeypatch):
    def _boom(_db):
        raise RuntimeError("google 500")

    monkeypatch.setattr(cal_oauth, "is_connected", lambda _db: True)
    monkeypatch.setattr(cal_sync, "reconcile", _boom)
    result = cal_sync.safe_reconcile(db)  # must not raise
    assert "error" in result and "google 500" in result["error"]
