"""The monthly Drive export — the athlete's data in their own hands, and the
only copy of the coach's state that doesn't live on Render.

Untested until 2026-08-03, and it had been failing since its first scheduled run: the
`j2h4all-export` cron never had the Google creds, so 2026-08-01 raised, alerted once, and
nothing else happened for two days. These cover what the cron actually depends on —
that a full state dump is assembled, uploaded under a dated name, stamped on success,
and that a permission failure degrades LOUDLY rather than writing an empty backup.
"""
import json
from datetime import date, timedelta

import pytest

from app import backup
from app.models import Checkin, Goal, Note, Preference, Session, SessionResult
from app.util import utcnow


class _Resp:
    def __init__(self, status=200, payload=None):
        self.status_code = status
        self._payload = payload if payload is not None else {}

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


@pytest.fixture
def drive(monkeypatch):
    """Fake Drive: records the multipart upload so we can inspect what was sent."""
    calls = {"get": [], "post": [], "uploaded": None}

    def _get(url, **kw):
        calls["get"].append((url, kw))
        return _Resp(200, {"files": [{"id": "folder123"}]})

    def _post(url, **kw):
        calls["post"].append((url, kw))
        if url == backup.DRIVE_UPLOAD:
            calls["uploaded"] = kw.get("content", b"").decode("utf-8")
            return _Resp(200, {"id": "file456"})
        return _Resp(200, {"id": "folder123"})

    monkeypatch.setattr(backup.httpx, "get", _get)
    monkeypatch.setattr(backup.httpx, "post", _post)
    monkeypatch.setattr(backup.oauth, "access_token", lambda db: "tok")
    return calls


def _seed(db):
    # Dates are RELATIVE to today, never literals: a fixture pinned to a fixed date
    # silently goes red the day it ages past a window the code cares about.
    today = date.today()
    db.add(Goal(format="backyard-ultra", race_date=today + timedelta(days=90), loop_km=6.706,
                target_laps=24, status="active", floor_note="Test Backyard",
                created_at=utcnow()))
    db.add(Session(date=today + timedelta(days=3), type="long_run", title="Long Run", purpose="p",
                   status="planned", created_at=utcnow(), updated_at=utcnow()))
    db.add(Checkin(date=today - timedelta(days=3), energy=5, created_at=utcnow()))
    db.add(Note(text="test coaching note", created_at=utcnow()))
    db.commit()


# ------------------------------------------------------------------ state assembly

def test_assemble_state_covers_every_table_the_coach_depends_on(db):
    _seed(db)
    state = backup.assemble_state(db)
    for table in ("goal", "session", "session_result", "checkin", "note", "preference",
                  "blood_marker", "injury_log", "macro_plan", "proposal", "message",
                  "user_state", "dietary_profile", "availability_window", "fitness_marker"):
        assert table in state, f"{table} missing from the backup"
    assert state["schema"] == "j2h4all-state-v1" and state["exported_at"]
    assert state["goal"][0]["format"] == "backyard-ultra"
    assert state["note"][0]["text"] == "test coaching note"


def test_assemble_state_is_json_serializable(db):
    """It's uploaded via json.dumps — a stray date/datetime must not blow up the
    cron a month after the column was added."""
    _seed(db)
    db.add(SessionResult(session_id=1, activity_id=1, completed=True,
                         deviation_asked_at=utcnow(), deviation_reason="logistics",
                         created_at=utcnow()))
    db.commit()
    json.dumps(backup.assemble_state(db), default=str)


def test_garmin_bulk_is_counted_not_dumped(db):
    """Activity/wellness raw payloads are megabytes; the backup carries counts."""
    state = backup.assemble_state(db)
    assert state["activity_count"] == 0 and state["wellness_daily_count"] == 0
    assert "activity" not in state and "wellness_daily" not in state


# ------------------------------------------------------------------ the upload path

def test_run_export_uploads_a_dated_file_and_stamps_success(db, drive):
    _seed(db)
    out = backup.run_export(db, today=date(2026, 8, 3))
    assert out == {"file_id": "file456", "name": "j2h4all-state-2026-08-03.json"}
    assert drive["uploaded"] and "j2h4all-state-2026-08-03.json" in drive["uploaded"]
    # The stamp is what `status()` and the ops check read to prove a run happened.
    assert db.scalar(
        __import__("sqlalchemy").select(Preference).where(
            Preference.key == backup.LAST_EXPORT_PREF)).value


def test_the_upload_actually_carries_the_state(db, drive):
    _seed(db)
    backup.run_export(db, today=date(2026, 8, 3))
    assert "Test Backyard" in drive["uploaded"]
    assert "test coaching note" in drive["uploaded"]


def test_folder_id_is_cached_after_the_first_run(db, drive):
    _seed(db)
    backup.run_export(db, today=date(2026, 8, 3))
    first = len(drive["get"])
    backup.run_export(db, today=date(2026, 9, 1))
    assert len(drive["get"]) == first, "folder lookup should be cached in Preference"


# ------------------------------------------------------------------ degrade loudly

def test_a_drive_403_raises_rather_than_silently_skipping(db, monkeypatch):
    """The whole point of the backup is an off-host copy; a quiet no-op is worse than a
    crash, because the cron's alert is the only thing that surfaces it."""
    monkeypatch.setattr(backup.oauth, "access_token", lambda db: "tok")
    monkeypatch.setattr(backup.httpx, "get", lambda url, **kw: _Resp(403))
    with pytest.raises(backup.DriveNotAuthorized):
        backup.run_export(db, today=date(2026, 8, 3))


def test_a_403_on_the_upload_itself_also_raises(db, monkeypatch):
    monkeypatch.setattr(backup.oauth, "access_token", lambda db: "tok")
    monkeypatch.setattr(backup.httpx, "get", lambda url, **kw: _Resp(200, {"files": [{"id": "f"}]}))
    monkeypatch.setattr(backup.httpx, "post", lambda url, **kw: _Resp(403))
    with pytest.raises(backup.DriveNotAuthorized):
        backup.run_export(db, today=date(2026, 8, 3))


def test_a_failed_export_does_not_stamp_success(db, monkeypatch):
    """The stamp is the evidence a backup exists — it must never outrun reality."""
    monkeypatch.setattr(backup.oauth, "access_token", lambda db: "tok")
    monkeypatch.setattr(backup.httpx, "get", lambda url, **kw: _Resp(403))
    with pytest.raises(backup.DriveNotAuthorized):
        backup.run_export(db, today=date(2026, 8, 3))
    assert backup.status(db)["last_export"] is None


def test_status_reports_never_backed_up_before_the_first_run(db, monkeypatch):
    monkeypatch.setattr(backup.oauth, "is_connected", lambda db: True)
    monkeypatch.setattr(backup.oauth, "drive_authorized", lambda db: True)
    assert backup.status(db)["last_export"] is None
