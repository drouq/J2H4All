"""Garmin workout reconcile (garmin/workouts.py::reconcile) with an in-memory fake
GarminClient — no network. Mirrors the calendar harness: create+schedule a new run,
update-in-place with a schedule refresh, delete a superseded session's workout, and
the protective skips (flag off, non-push types, sessions with a linked result)."""
import types
from datetime import date, timedelta

import pytest

from app.garmin import workouts
from app.models import Session as PlanSession
from app.models import SessionResult
from app.util import utcnow


class FakeGarmin:
    """Records api_write calls and simulates the workout/schedule endpoints."""
    def __init__(self, db=None):
        self.calls = []           # (method, path)
        self._wid = 0
        self._sid = 0

    def api_write(self, method, path, payload=None):
        self.calls.append((method, path))
        if method == "POST" and path == "/workout-service/workout":
            self._wid += 1
            return {"workoutId": 1000 + self._wid}
        if method == "POST" and path.startswith("/workout-service/schedule/"):
            self._sid += 1
            return {"workoutScheduleId": 2000 + self._sid}
        return None  # PUT (update) and DELETE (unschedule/delete) → 204-ish

    def count(self, method, prefix):
        return sum(1 for m, p in self.calls if m == method and p.startswith(prefix))


@pytest.fixture
def gc(monkeypatch):
    fake = FakeGarmin()
    monkeypatch.setattr(workouts, "GarminClient", lambda db=None: fake)
    monkeypatch.setattr(workouts, "get_settings",
                        lambda: types.SimpleNamespace(garmin_workout_push_enabled=True,
                                                      garmin_sync_enabled=True))
    return fake


def _mk(db, d, type_="easy", title="Easy", status="planned", wid=None, sid=None):
    s = PlanSession(date=d, type=type_, title=title, purpose="p", status=status,
                    duration_min=50, target_zone="Z2",
                    garmin_workout_id=wid, garmin_schedule_id=sid,
                    created_at=utcnow(), updated_at=utcnow())
    db.add(s)
    db.commit()
    return s


def test_disabled_flag_skips(db, monkeypatch):
    monkeypatch.setattr(workouts, "GarminClient", lambda db=None: FakeGarmin())
    monkeypatch.setattr(workouts, "get_settings",
                        lambda: types.SimpleNamespace(garmin_workout_push_enabled=False,
                                                      garmin_sync_enabled=True))
    assert workouts.reconcile(db) == {"skipped": "workout push disabled"}


def test_new_run_is_created_and_scheduled(db, gc):
    s = _mk(db, date.today() + timedelta(days=2), "long_run", "Long Run")
    res = workouts.reconcile(db)
    assert res["created"] == 1
    assert gc.count("POST", "/workout-service/workout") == 1        # created
    assert gc.count("POST", "/workout-service/schedule/") == 1      # scheduled
    db.refresh(s)
    assert s.garmin_workout_id and s.garmin_schedule_id            # ids stored


def test_existing_workout_updates_and_refreshes_schedule(db, gc):
    # A schedule refresh (delete + recreate) is what actually delivers to the watch.
    _mk(db, date.today() + timedelta(days=3), "easy", "Easy", wid="777", sid="888")
    res = workouts.reconcile(db)
    assert res["updated"] == 1
    assert gc.count("PUT", "/workout-service/workout/") == 1        # content PUT
    assert gc.count("DELETE", "/workout-service/schedule/") == 1    # old schedule dropped
    assert gc.count("POST", "/workout-service/schedule/") == 1      # rescheduled


def test_superseded_session_workout_is_deleted(db, gc):
    s = _mk(db, date.today() + timedelta(days=2), "easy", "Old", status="superseded",
            wid="55", sid="66")
    res = workouts.reconcile(db)
    assert res["deleted"] == 1
    db.refresh(s)
    assert s.garmin_workout_id is None and s.garmin_schedule_id is None


def test_non_push_types_are_ignored(db, gc):
    _mk(db, date.today() + timedelta(days=2), "strength", "Gym")
    _mk(db, date.today() + timedelta(days=3), "rest", "Rest")
    res = workouts.reconcile(db)
    assert res == {"created": 0, "updated": 0, "deleted": 0}
    assert gc.calls == []


def test_session_with_linked_result_is_not_repushed(db, gc):
    # Pushing over a completed day's workout would break Garmin's activity↔workout link.
    s = _mk(db, date.today(), "easy", "Done today", wid="111", sid="222")
    db.add(SessionResult(session_id=s.id, activity_id=1, completed=True, created_at=utcnow()))
    db.commit()
    res = workouts.reconcile(db)
    assert res["created"] == 0 and res["updated"] == 0
    assert gc.count("PUT", "/workout-service/workout/") == 0
