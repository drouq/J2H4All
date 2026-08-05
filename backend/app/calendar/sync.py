"""Reconcile the dedicated J2H4All Training calendar to the store (PRD §10).

The store is the source of truth; this makes Google Calendar *match* it for the
future window. One event per planned (non-rest) session, updated in place via the
stable event ID — never duplicated. Events for dates that no longer hold a workout
(superseded, deleted, or turned into a rest day) are removed.

Triggers: a proposal approval (PRD §11.4), the manual "sync to calendar" button,
AND — via `safe_reconcile` — the daily Garmin sync cron, so runs Garmin confirmed
get marked ✅ done without waiting for the next approval. This does NOT weaken the
approval gate (PRD §2.3): reconcile only ever mirrors sessions that are already
`planned` in the store (unapproved changes live in the `proposal` table, never in
`Session` rows) and marks completed reality — it can't introduce a plan change.
"""

import logging
from datetime import date, timedelta

from sqlalchemy import and_, select
from sqlalchemy.orm import Session as DbSession

from ..coach import completion
from ..context.store import get_or_create_state
from ..models import Session, SessionResult
from . import oauth
from .client import CalendarClient, EventGone, build_event_body

logger = logging.getLogger(__name__)

CALENDAR_SUMMARY = "J2H4All Training"
# How far back the past sweep re-marks completed events. Bounded so a reconcile
# doesn't re-PUT the whole history every time; anything older was already marked
# by an earlier reconcile (they run at least weekly, on Sunday-review approval).
PAST_COMPLETED_WINDOW_DAYS = 45
CALENDAR_DESCRIPTION = "Training sessions from J2H4All (Journey to Hundred, for All). Managed by the coach — don't edit here."


def ensure_calendar(db: DbSession, client: CalendarClient) -> str:
    """Return the J2H4All calendar id, creating (or self-healing) it as needed."""
    state = get_or_create_state(db)
    if state.training_calendar_id:
        if client.get_calendar(state.training_calendar_id) is not None:
            return state.training_calendar_id
        logger.warning("Stored J2H4All calendar %s is gone; recreating", state.training_calendar_id)
    cal_id = client.create_calendar(CALENDAR_SUMMARY, CALENDAR_DESCRIPTION)
    state.training_calendar_id = cal_id
    db.commit()
    return cal_id


def _session_dict(s: Session) -> dict:
    return {
        "date": s.date.isoformat(), "type": s.type, "title": s.title,
        "duration_min": s.duration_min, "distance_km": s.distance_km,
        "target_zone": s.target_zone, "target_pace": s.target_pace,
        "purpose": s.purpose, "fueling_note": s.fueling_note,
        "structure": s.structure,
    }


def reconcile(db: DbSession, window_start: date | None = None) -> dict:
    """Make the calendar match active planned non-rest sessions: mirror the future
    window, ✅-mark completed past sessions, and sweep stale events (future orphans
    and — bounded to the completed window — orphaned PAST ghosts left by revisions)."""
    token = oauth.access_token(db)  # raises CalendarNotConnected if not connected
    client = CalendarClient(token)
    cal_id = ensure_calendar(db, client)
    start = window_start or date.today()

    start_iso = start.isoformat()
    lo = start - timedelta(days=PAST_COMPLETED_WINDOW_DAYS)
    lo_iso = lo.isoformat()
    desired = db.scalars(
        select(Session).where(
            Session.status == "planned", Session.type != "rest", Session.date >= start
        ).order_by(Session.date)
    ).all()
    desired_ids = {s.id for s in desired}

    # CALENDAR-AUTHORITATIVE: the live calendar is ground truth for what events exist.
    # Store-side event-id links can drift out of sync (a carried-over event whose row
    # lost its link becomes a ghost the store can't see), so we enumerate the actual
    # events and reconcile against them, rather than trusting the store's ids. We list
    # from `lo` (not just `start`) so the bounded past window is in view for the
    # past-ghost sweep below; the future sweep still guards on `>= start_iso`.
    existing = {e["id"]: e for e in client.list_events(cal_id, lo_iso)}

    created = updated = deleted = 0
    kept: set[str] = set()
    for s in desired:
        body = build_event_body(_session_dict(s))
        eid = s.calendar_event_id
        ex = existing.get(eid) if eid else None
        if ex is not None:
            # Event is live. If it already shows this session (or is a ✅-done event the
            # completed-sweep owns), a plain update keeps the description fresh. If it
            # shows something else — a carried-over event still displaying the OLD
            # session — force correctness with delete+reinsert, so a silent no-op update
            # can never leave stale content on the calendar.
            marked = tuple(completion.STATUS_EMOJI.values())  # ✅ / ⚠️ / ❌ — the marking loop owns these
            fresh = ex["summary"].startswith(marked) or (
                ex["summary"] == body["summary"] and ex["start_date"] == body["start"]["date"])
            if fresh:
                try:
                    client.update_event(cal_id, eid, body)
                    updated += 1
                except EventGone:
                    s.calendar_event_id = eid = client.insert_event(cal_id, body)
                    created += 1
            else:
                client.delete_event(cal_id, eid)
                s.calendar_event_id = eid = client.insert_event(cal_id, body)
                created += 1
        elif eid:
            try:
                client.update_event(cal_id, eid, body)  # stored id but not in the listing window
                updated += 1
            except EventGone:
                s.calendar_event_id = eid = client.insert_event(cal_id, body)
                created += 1
        else:
            s.calendar_event_id = eid = client.insert_event(cal_id, body)
            created += 1
        kept.add(s.calendar_event_id)

    # Sweep: any FUTURE event not backing a desired session is stale — a superseded
    # session's leftover, or a ghost whose store row lost its link. Delete it (this
    # subsumes the old store-side orphan cleanup, which missed link-less ghosts).
    for eid, e in existing.items():
        if eid not in kept and e["start_date"] >= start_iso:
            client.delete_event(cal_id, eid)
            deleted += 1
    # Null store-side links on future non-desired sessions so they don't carry dead ids.
    for s in db.scalars(
        select(Session).where(
            Session.calendar_event_id.isnot(None), Session.date >= start,
            Session.id.notin_(desired_ids) if desired_ids else Session.id.isnot(None),
        )
    ).all():
        s.calendar_event_id = None

    # ---- past sweep (runs under the same explicit triggers — PRD §2.3 holds) ----
    # 1) Superseded past sessions still holding an event = workouts that were
    #    rescheduled away after their date passed; their events are stale.
    #    Unbounded on purpose: deleting nulls the id, so each is handled once.
    past_orphans = db.scalars(
        select(Session).where(
            Session.calendar_event_id.isnot(None), Session.date < start,
            Session.status != "planned",
        )
    ).all()
    for s in past_orphans:
        client.delete_event(cal_id, s.calendar_event_id)
        s.calendar_event_id = None
        deleted += 1

    # 2) Mark what actually happened: ✅ done as planned, ⚠️ done but >20% off, ❌
    #    ABANDONED — still not done after the grace window (coach/completion.py).
    #    A merely MISSED session (the day closed with nothing against it) is skipped
    #    alongside PLANNED: it keeps its type icon deliberately, because he shifts runs
    #    by a day or two and a late run still links via its watch workout id, so it can
    #    still become ✅. Only abandonment earns the cross.
    #    Includes TODAY (<= start): a session completed this morning would
    #    otherwise be rewritten as planned by the desired-loop above and only
    #    get its mark at the next reconcile, possibly a week later. Running after
    #    the desired loop, this body wins for the same-day case. A past session
    #    still inside the grace window stays `planned` and is left untouched — he
    #    logs late sometimes, and a run done on the day links via its watch workout.
    rows = db.execute(
        select(Session, SessionResult)
        .outerjoin(SessionResult, and_(SessionResult.session_id == Session.id,
                                       SessionResult.completed.is_(True)))
        .where(
            Session.status == "planned", Session.type != "rest",
            Session.date <= start, Session.date >= lo,
            Session.calendar_event_id.isnot(None),
        )
        .order_by(SessionResult.id)
    ).all()
    latest = {s.id: (s, r) for s, r in rows}  # last result wins if 2 runs matched one session
    completed = abandoned = 0
    for s, r in latest.values():
        status = completion.classify(s, r, start)
        if status in (completion.PLANNED, completion.MISSED):
            continue
        result_view = None
        if r is not None:
            result_view = {
                "distance_km": r.actual_distance_km,
                "duration_min": r.actual_duration_min,
                "avg_hr": r.actual_avg_hr,
                "delta_line": completion.delta_line(s, r),
                "deviation_reason": (f"Why: {r.deviation_reason}" if r.deviation_reason else None),
            }
        body = build_event_body(_session_dict(s), result=result_view, status=status)
        try:
            client.update_event(cal_id, s.calendar_event_id, body)
        except EventGone:
            s.calendar_event_id = client.insert_event(cal_id, body)
        if status == completion.ABANDONED:
            abandoned += 1
        else:
            completed += 1

    # 3) Past-ghost sweep (calendar-authoritative, bounded to the completed window).
    #    A plan revision can orphan a PAST event — its store row lost the event link
    #    during donor-queue carry-over — and the store-side `past_orphans` cleanup
    #    (#1) only sees events still linked to a superseded row. So enumerate the live
    #    past events and delete any not backed by a current planned non-rest session:
    #    the twin of the future sweep. A planned-but-unrun past session keeps its
    #    event (its id is still in the set). Computed AFTER the ✅-loop so any event it
    #    reinserted (EventGone) is counted as valid via the session's refreshed id.
    valid_past = {
        s.calendar_event_id for s in db.scalars(
            select(Session).where(
                Session.status == "planned", Session.type != "rest",
                Session.calendar_event_id.isnot(None),
                Session.date >= lo, Session.date < start,
            )
        ).all()
    }
    for eid, e in existing.items():
        if eid not in valid_past and lo_iso <= e["start_date"] < start_iso:
            client.delete_event(cal_id, eid)
            deleted += 1

    db.commit()
    from ..context.store import stamp_meta
    stamp_meta(db, "last_push_calendar_at")  # surfaced on the web Calendar panel
    result = {
        "created": created, "updated": updated, "deleted": deleted,
        "completed_marked": completed, "abandoned_marked": abandoned, "calendar_id": cal_id,
    }
    logger.info("Calendar reconcile: %s", result)
    return result


def safe_reconcile(db: DbSession) -> dict:
    """Best-effort reconcile for callers that must never raise — the approval path
    and the daily-sync cron. Guarded by connection state; a calendar outage returns
    an error dict instead of propagating (PRD §4: degrade loudly, don't roll back /
    fail the caller). Result shape matches `reconcile` on success, else
    {'skipped': ...} or {'error': ...}."""
    if not oauth.is_connected(db):
        return {"skipped": "calendar not connected"}
    try:
        return reconcile(db)
    except oauth.CalendarNotConnected as exc:
        return {"error": str(exc)}
    except Exception as exc:  # noqa: BLE001 — never fail the caller on a calendar error
        logger.exception("Calendar reconcile failed")
        return {"error": f"calendar sync failed: {exc}"}
