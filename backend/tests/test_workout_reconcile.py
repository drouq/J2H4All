"""The workout reconcile's SCHEDULE-AUTHORITATIVE sweep.

Ported from the Google Calendar reconcile after the identical failure showed up here:
trusting store-side ids alone means a scheduled workout whose `Session` row lost its
link - or whose id was never committed because a reconcile died mid-run - is invisible.
It can never be cleaned up, and the next push adds a duplicate beside it. Both happened in the
reference deployment: a ghost no row referenced, and eleven stale schedule ids.

`reconcile` had no coverage at all before this file.
"""
from datetime import date, timedelta

import pytest

from app.garmin import workouts
from app.models import Session
from app.util import utcnow


@pytest.fixture
def push_on(monkeypatch):
    from app.config import get_settings
    s = get_settings()
    monkeypatch.setattr(s, "garmin_workout_push_enabled", True)
    monkeypatch.setattr(s, "garmin_sync_enabled", True)
    return s


class _FakeGarmin:
    """Stands in for Garmin: serves a month grid and records every write."""

    def __init__(self, items=None, **kw):
        self.items = items or []
        self.writes = []
        self._next = 9000

    def api(self, path, **params):
        return {"calendarItems": list(self.items)}

    def api_write(self, method, path, payload=None):
        self.writes.append((method, path))
        self._next += 1
        if method == "POST" and path == "/workout-service/workout":
            return {"workoutId": self._next}
        if method == "POST" and path.startswith("/workout-service/schedule/"):
            return {"workoutScheduleId": self._next}
        return None

    def deleted_workouts(self):
        return [p.rsplit("/", 1)[-1] for m, p in self.writes
                if m == "DELETE" and p.startswith("/workout-service/workout/")]

    def deleted_schedules(self):
        return [p.rsplit("/", 1)[-1] for m, p in self.writes
                if m == "DELETE" and p.startswith("/workout-service/schedule/")]


def _item(day, workout_id, schedule_id, title="J2H: Easy Aerobic - 60 min Z2"):
    return {"itemType": "workout", "date": day.isoformat(), "workoutId": workout_id,
            "id": schedule_id, "title": title}


def _session(db, day, **kw):
    s = Session(date=day, type=kw.pop("type", "easy"), title=kw.pop("title", "Easy Aerobic - 60 min Z2"),
                purpose="", status=kw.pop("status", "planned"), created_at=utcnow(),
                updated_at=utcnow(), duration_min=kw.pop("duration_min", 60), **kw)
    db.add(s)
    db.commit()
    return s


def _install(monkeypatch, fake):
    monkeypatch.setattr(workouts, "GarminClient", lambda **kw: fake)


def test_sweeps_a_ghost_no_session_row_references(db, push_on, monkeypatch):
    """The Aug 25 case: Garmin holds a scheduled J2H workout, nothing in the store
    points at it. Store-side cleanup cannot see it; the sweep must remove it."""
    tomorrow = date.today() + timedelta(days=1)
    ghost = _item(tomorrow + timedelta(days=1), 1658593310, 1738756431, "J2H: Easy Aerobic - 80 min Z2")
    fake = _FakeGarmin(items=[ghost])
    _install(monkeypatch, fake)
    _session(db, tomorrow)  # a desired session, unrelated to the ghost

    result = workouts.reconcile(db)
    assert result["ghosts_swept"] == 1
    assert "1658593310" in fake.deleted_workouts()
    assert "1738756431" in fake.deleted_schedules()


def test_keeps_a_live_workout_that_backs_a_desired_session(db, push_on, monkeypatch):
    tomorrow = date.today() + timedelta(days=1)
    s = _session(db, tomorrow, garmin_workout_id="555", garmin_schedule_id="777")
    fake = _FakeGarmin(items=[_item(tomorrow, 555, 777)])
    _install(monkeypatch, fake)

    result = workouts.reconcile(db)
    assert result.get("ghosts_swept") is None
    assert "555" not in fake.deleted_workouts()   # updated in place, never swept
    assert s.garmin_workout_id == "555"


def test_adopts_the_live_schedule_id_instead_of_duplicating(db, push_on, monkeypatch):
    """A stale stored schedule id would 404 on delete and the reschedule would then ADD
    a second entry for the same workout and date - a duplicate on the watch. The live
    id has to win."""
    tomorrow = date.today() + timedelta(days=1)
    _session(db, tomorrow, garmin_workout_id="555", garmin_schedule_id="1733370690")  # dead
    fake = _FakeGarmin(items=[_item(tomorrow, 555, 1738756131)])                          # real
    _install(monkeypatch, fake)

    workouts.reconcile(db)
    assert "1738756131" in fake.deleted_schedules()      # unscheduled the one that exists
    assert "1733370690" not in fake.deleted_schedules()  # never chased the dead id


def test_past_scheduled_workouts_are_left_alone(db, push_on, monkeypatch):
    """Garmin links a recorded activity to its scheduled workout; deleting a past one
    would erase that. The window starts at today."""
    yesterday = date.today() - timedelta(days=1)
    fake = _FakeGarmin(items=[_item(yesterday, 1234, 5678)])
    _install(monkeypatch, fake)

    result = workouts.reconcile(db)
    assert result.get("ghosts_swept") is None
    assert "1234" not in fake.deleted_workouts()


def test_only_our_own_workouts_are_ever_touched(db, push_on, monkeypatch):
    """A workout he built himself in Garmin Connect is not ours to delete."""
    tomorrow = date.today() + timedelta(days=1)
    fake = _FakeGarmin(items=[_item(tomorrow, 4242, 4243, "My own tempo session")])
    _install(monkeypatch, fake)

    result = workouts.reconcile(db)
    assert result.get("ghosts_swept") is None
    assert "4242" not in fake.deleted_workouts()


def test_a_failed_listing_degrades_to_a_normal_push(db, push_on, monkeypatch):
    """The sweep is a diagnostic read. If it fails, he must still get his plan on the
    watch - the old store-side behaviour, just blind to ghosts."""
    class _NoCalendar(_FakeGarmin):
        def api(self, path, **params):
            raise RuntimeError("calendar-service unavailable")

    tomorrow = date.today() + timedelta(days=1)
    fake = _NoCalendar()
    _install(monkeypatch, fake)
    _session(db, tomorrow)

    result = workouts.reconcile(db)
    assert result["created"] == 1                      # the push still happened
    assert "skipped" in result["ghost_sweep"]


def test_one_unsweepable_ghost_does_not_block_the_others(db, push_on, monkeypatch):
    """The lesson from the 16-day outage, applied to the new loop as well."""
    d1 = date.today() + timedelta(days=1)
    d2 = date.today() + timedelta(days=2)

    class _OneBadDelete(_FakeGarmin):
        def api_write(self, method, path, payload=None):
            if method == "DELETE" and path.endswith("/1111"):
                raise RuntimeError("boom")
            return super().api_write(method, path, payload)

    fake = _OneBadDelete(items=[_item(d1, 1111, 2222), _item(d2, 3333, 4444)])
    _install(monkeypatch, fake)

    result = workouts.reconcile(db)
    assert result["ghosts_swept"] == 1        # the healthy one still went
    assert result["orphan_errors"] == 1       # and the failure is reported, not hidden
    assert "3333" in fake.deleted_workouts()
