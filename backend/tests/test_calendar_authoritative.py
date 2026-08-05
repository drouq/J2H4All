"""Calendar-authoritative reconcile (the bug behind "the calendar shows sessions
the plan doesn't have"): reconcile must make the live calendar EXACTLY mirror the
planned sessions — force-correcting carried-over events that still show old content,
deleting ghost events with no backing session, and leaving already-correct events
alone. Uses an in-memory fake CalendarClient (no network)."""
from datetime import date, timedelta

import pytest

from app.calendar import sync as cal_sync
from app.calendar.client import EventGone, build_event_body, event_emoji
from app.models import Session as PlanSession
from app.util import utcnow


class FakeCalendar:
    def __init__(self, events=None):
        self.events = dict(events or {})   # id -> {"summary","start_date"}
        self._n = 0

    def list_events(self, cal_id, time_min_date):
        return [{"id": i, "summary": e["summary"], "start_date": e["start_date"]}
                for i, e in self.events.items() if e["start_date"] >= time_min_date]

    def insert_event(self, cal_id, body):
        self._n += 1
        eid = f"new{self._n}"
        self.events[eid] = {"summary": body["summary"], "start_date": body["start"]["date"]}
        return eid

    def update_event(self, cal_id, eid, body):
        if eid not in self.events:
            raise EventGone(eid)
        self.events[eid] = {"summary": body["summary"], "start_date": body["start"]["date"]}

    def delete_event(self, cal_id, eid):
        self.events.pop(eid, None)


def _mk(db, d, type_, title, eid=None, status="planned"):
    db.add(PlanSession(date=d, type=type_, title=title, purpose="p", status=status,
                       calendar_event_id=eid, created_at=utcnow(), updated_at=utcnow()))
    db.commit()


@pytest.fixture
def patched(monkeypatch):
    fake = FakeCalendar()
    monkeypatch.setattr(cal_sync.oauth, "access_token", lambda db: "tok")
    monkeypatch.setattr(cal_sync, "CalendarClient", lambda tok: fake)
    monkeypatch.setattr(cal_sync, "ensure_calendar", lambda db, client: "cal1")
    return fake


def _summ(type_, title):
    return f"{event_emoji(type_)} {title}"


def test_stale_carried_over_event_is_corrected(db, patched):
    d = (date.today() + timedelta(days=3))
    # The exact bug: a planned Long Run whose stored event still shows "Recovery Jog".
    patched.events["E1"] = {"summary": "🚶 Recovery Jog", "start_date": d.isoformat()}
    _mk(db, d, "long_run", "Long Run — 2h30 Z2", eid="E1")

    cal_sync.reconcile(db)

    summaries = [e["summary"] for e in patched.events.values()]
    assert _summ("long_run", "Long Run — 2h30 Z2") in summaries
    assert "🚶 Recovery Jog" not in summaries   # the stale content is gone


def test_ghost_event_with_no_session_is_deleted(db, patched):
    d = (date.today() + timedelta(days=2)).isoformat()
    patched.events["GHOST"] = {"summary": "🏃 Easy Z2 Run", "start_date": d}  # no backing row
    cal_sync.reconcile(db)
    assert "GHOST" not in patched.events
    assert patched.events == {}


def test_correct_event_is_left_in_place(db, patched):
    d = (date.today() + timedelta(days=4))
    body = build_event_body({"date": d.isoformat(), "type": "easy", "title": "Easy Aerobic — 55 min Z2",
                             "purpose": "p"})
    patched.events["E2"] = {"summary": body["summary"], "start_date": d.isoformat()}
    _mk(db, d, "easy", "Easy Aerobic — 55 min Z2", eid="E2")

    cal_sync.reconcile(db)

    assert "E2" in patched.events  # unchanged id — not needlessly recreated
    assert patched.events["E2"]["summary"] == body["summary"]


def test_missing_event_is_inserted_and_id_stored(db, patched):
    d = (date.today() + timedelta(days=5))
    _mk(db, d, "strength", "Gym — Push", eid=None)
    cal_sync.reconcile(db)
    s = db.scalar(cal_sync.select(PlanSession).where(PlanSession.date == d))
    assert s.calendar_event_id is not None
    assert patched.events[s.calendar_event_id]["summary"] == _summ("strength", "Gym — Push")


def test_past_ghost_from_a_revision_is_deleted_but_current_done_event_kept(db, patched):
    """The bug behind "two ✅ events on a day I ran once": a plan revision renamed a
    past session, orphaning its old event (store row lost the link). Reconcile must
    delete that orphaned PAST ghost while keeping the current session's event."""
    from app.models import SessionResult
    yesterday = date.today() - timedelta(days=1)
    # Current session for that past day, already completed → its event is legit.
    good = build_event_body({"date": yesterday.isoformat(), "type": "easy",
                             "title": "Easy Aerobic — 55 min Z2", "purpose": "p"})
    patched.events["GOOD"] = {"summary": good["summary"], "start_date": yesterday.isoformat()}
    _mk(db, yesterday, "easy", "Easy Aerobic — 55 min Z2", eid="GOOD")
    sess = db.scalar(cal_sync.select(PlanSession).where(PlanSession.calendar_event_id == "GOOD"))
    db.add(SessionResult(session_id=sess.id, activity_id=None, completed=True,
                         actual_duration_min=55.0, created_at=utcnow()))
    # Orphaned ghost from the earlier version of the same day — no backing session row.
    patched.events["GHOST"] = {"summary": "🏃 Easy Z2 + 6 Strides",
                               "start_date": yesterday.isoformat()}
    db.commit()

    cal_sync.reconcile(db)

    assert "GHOST" not in patched.events          # orphaned past ghost swept
    assert "GOOD" in patched.events               # current session's event kept
    assert patched.events["GOOD"]["summary"].startswith("✅")  # and ✅-marked


def test_unrun_past_planned_session_event_is_preserved(db, patched):
    """A past planned session that was never run (a missed workout) still has a
    backing row, so its event must survive the past sweep — only true orphans go."""
    yesterday = date.today() - timedelta(days=1)
    body = build_event_body({"date": yesterday.isoformat(), "type": "long_run",
                             "title": "Long Run — 2h00 Z2", "purpose": "p"})
    patched.events["MISSED"] = {"summary": body["summary"], "start_date": yesterday.isoformat()}
    _mk(db, yesterday, "long_run", "Long Run — 2h00 Z2", eid="MISSED")

    cal_sync.reconcile(db)

    assert "MISSED" in patched.events  # backed by a planned row → not swept


def test_rest_days_get_no_event_and_end_state_mirrors_plan(db, patched):
    base = date.today() + timedelta(days=1)
    # A realistic week: 2 runs, 1 gym, 1 rest — plus a leftover ghost from an old plan.
    _mk(db, base, "rest", "Rest")
    _mk(db, base + timedelta(days=1), "easy", "Easy 55", eid=None)
    _mk(db, base + timedelta(days=2), "strength", "Gym Pull", eid=None)
    _mk(db, base + timedelta(days=3), "long_run", "Long Run 2h30", eid=None)
    patched.events["OLD"] = {"summary": "🏃 Old dropped run",
                             "start_date": base.isoformat()}  # ghost on the rest day
    res = cal_sync.reconcile(db)
    # Exactly the 3 non-rest sessions remain; the ghost and the rest day have no event.
    assert len(patched.events) == 3
    assert "OLD" not in patched.events
    assert res["deleted"] >= 1
