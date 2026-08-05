"""Garmin workout push (docs/garmin-workout-push-plan.md).

Every approved planned run session becomes a Garmin structured workout scheduled
on its date, so the watch proposes it with pace/HR-zone targets. Mirrors the
Google Calendar pattern exactly: store is truth, stable ids (`garmin_workout_id`
/ `garmin_schedule_id`), update-in-place never duplicate, and it only ever runs
from an explicit user action — proposal approval or the manual sync button
(extended to this surface; no silent writes to the Garmin account).

Feature-flagged via GARMIN_WORKOUT_PUSH_ENABLED (default off).
"""

import logging
import re
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession

from ..config import get_settings
from ..models import Session
from . import endpoints
from .client import GarminClient

logger = logging.getLogger(__name__)

# Session types that make sense as watch workouts. rest/race never push;
# strength is a different workout schema (out of v1 scope per the plan).
PUSH_TYPES = {"long_run", "easy", "recovery", "intervals", "tempo"}

_SPORT = {"sportTypeId": 1, "sportTypeKey": "running", "displayOrder": 1}
_STEP_TYPES = {
    "warmup": {"stepTypeId": 1, "stepTypeKey": "warmup"},
    "cooldown": {"stepTypeId": 2, "stepTypeKey": "cooldown"},
    "work": {"stepTypeId": 3, "stepTypeKey": "interval"},
    "recover": {"stepTypeId": 4, "stepTypeKey": "recovery"},
    "repeat": {"stepTypeId": 6, "stepTypeKey": "repeat"},
}
_END_LAP = {"conditionTypeId": 1, "conditionTypeKey": "lap.button", "displayOrder": 1, "displayable": True}
_END_TIME = {"conditionTypeId": 2, "conditionTypeKey": "time", "displayOrder": 2, "displayable": True}
_END_DIST = {"conditionTypeId": 3, "conditionTypeKey": "distance", "displayOrder": 3, "displayable": True}
_END_ITER = {"conditionTypeId": 7, "conditionTypeKey": "iterations", "displayOrder": 7, "displayable": False}
_TARGET_NONE = {"workoutTargetTypeId": 1, "workoutTargetTypeKey": "no.target", "displayOrder": 1}
_TARGET_HR_ZONE = {"workoutTargetTypeId": 4, "workoutTargetTypeKey": "heart.rate.zone", "displayOrder": 4}
_TARGET_PACE = {"workoutTargetTypeId": 6, "workoutTargetTypeKey": "pace.zone", "displayOrder": 6}


# ------------------------------------------------------------------ payload builder

def _pace_band_mps(pace: str) -> tuple[float, float] | None:
    """'4:30-4:50/km' → (slower, faster) speeds in m/s; single pace gets a ±10 s/km band."""
    secs = [int(m) * 60 + int(s) for m, s in re.findall(r"(\d{1,2}):(\d{2})", pace or "")]
    if not secs:
        return None
    slow, fast = (max(secs[:2]), min(secs[:2])) if len(secs) >= 2 else (secs[0] + 10, secs[0] - 10)
    return round(1000 / slow, 3), round(1000 / fast, 3)


def _zone_number(zone: str | None) -> int | None:
    """'Z2' → 2; a range like 'Z3-Z4' targets its lower zone (conservative)."""
    m = re.search(r"(\d)", zone or "")
    return int(m.group(1)) if m else None


def _target_fields(target_pace: str | None, target_zone: str | None) -> dict:
    """Pace preferred over HR zone when both are present (the athlete's call, plan Q1)."""
    band = _pace_band_mps(target_pace) if target_pace else None
    if band:
        return {"targetType": _TARGET_PACE, "targetValueOne": band[0],
                "targetValueTwo": band[1], "zoneNumber": None}
    zone = _zone_number(target_zone)
    if zone:
        return {"targetType": _TARGET_HR_ZONE, "targetValueOne": None,
                "targetValueTwo": None, "zoneNumber": zone}
    return {"targetType": _TARGET_NONE, "targetValueOne": None,
            "targetValueTwo": None, "zoneNumber": None}


def _end_fields(duration_min, distance_km) -> dict:
    """Distance preferred when both are set (plan §mapping)."""
    if distance_km:
        return {"endCondition": _END_DIST, "endConditionValue": round(float(distance_km) * 1000, 1)}
    if duration_min:
        return {"endCondition": _END_TIME, "endConditionValue": round(float(duration_min) * 60, 1)}
    return {"endCondition": _END_LAP, "endConditionValue": None}


# ------------------------------------------------------- target-free ease in / ease out
# A zone (or pace) target is wrong at BOTH ends of a session, for the same underlying
# reason: HR lags the effort, so the watch alerts them for the gap while their body is
# doing exactly the right thing.
#
#   START — the target used to arm at second one, so the watch alerted ~10 s in.
#           Nobody is in Z2 ten seconds into a run. A gentler target is NOT a fix:
#           HR starts BELOW Z1 too, so a Z1 warmup alerts identically.
#   END   — coming off 5x4 min at threshold their HR is 165-175 and takes minutes to
#           fall through a Z1 cooldown ceiling, so the cooldown step alerts them for
#           being ABOVE target. The coach rates this the worse of the two: at the
#           start they are fresh and can shrug at a beep; at the end they are tired and hot,
#           and the watch is teaching them that recovering is a failure state.
#
# So both ends get a TARGET-FREE step, always CARVED OUT of the step it belongs to,
# never added on top (the coach's call, 2026-08-03, asked with full doctrine + live
# prod state). Their prescription is a total-duration one — "55 min Z2" means 55 minutes
# of running, and the first five were never actually Z2 anyway — so carving changes the
# alerting, not the training. Adding on top would make every session run ~5 min long by
# design: a 3h00 long run filing as 3h05, invisible extra volume that never trips the
# >20% AND >15 min off-plan question, in a rebuild block where the ~10%/week ceiling
# only means anything if the numbers are honest. One number at both ends, deliberately:
# nothing for them to remember.
#
# NB the ease-out fires ONLY on a cooldown the coach actually prescribed. Plain easy and
# long runs get NO trailing free step — asked and declined explicitly: there is no alert
# to fix at the end of an easy run (they are already in zone), it would be a step they look
# at on every run forever to change nothing, and it costs most where it helps least — a
# 30 min recovery jog would become 5 free + 20 Z1 + 5 free, stripping the ceiling out of
# the one session whose entire purpose is holding it. On the long runs the coach also
# wants the final half-hour governed by HIM holding pace and HR flat (the durability KPI
# for this race, and their last two long runs drifted late) — a free step there would
# legitimise exactly the fade they's coaching out.
EASE_MIN = 5.0   # minutes — long enough that HR has arrived (or fallen), in their heat
EASE_KM = 0.8    # ≈ EASE_MIN at their easy pace, for a distance-ended step


def _has_target(step: dict) -> bool:
    """Derived from `_target_fields` rather than re-testing the raw keys, so the two
    can't drift — e.g. a target_zone of 'easy' carries no digit and yields no target."""
    fields = _target_fields(step.get("target_pace"), step.get("target_zone"))
    return fields["targetType"] is not _TARGET_NONE


def _carve(step: dict, head_kind: str) -> tuple[dict, dict] | None:
    """Split a targeted step into a target-free head + the targeted remainder.

    Never consumes more than half the step, so a short one degrades to a shorter free
    block instead of collapsing to a zero-length (or negative) step on the watch.
    Returns None for a step with no end value — nothing to carve from."""
    # Match `_end_fields`' precedence: distance wins when both are set.
    if step.get("distance_km"):
        carve = min(EASE_KM, float(step["distance_km"]) / 2)
        return ({"kind": head_kind, "distance_km": round(carve, 3)},
                {**step, "distance_km": round(float(step["distance_km"]) - carve, 3)})
    if step.get("duration_min"):
        carve = min(EASE_MIN, float(step["duration_min"]) / 2)
        return ({"kind": head_kind, "duration_min": round(carve, 2)},
                {**step, "duration_min": round(float(step["duration_min"]) - carve, 2)})
    return None


def _with_lead_in(source: list[dict]) -> list[dict]:
    """Open the workout with a target-free step, taken out of the first one.

    A coach-prescribed warmup keeps its place — the lead-in is carved from INSIDE it
    (a '15 min easy + 5x4 min' becomes 5 min free + 10 min easy + the work), because
    the opening minutes of a warmup shouldn't be zone-gated either."""
    if not source:
        return source
    first = source[0]
    if first.get("kind") == "repeat" or not _has_target(first):
        # A repeat block has nothing to carve from, and an untargeted opener is
        # already the coach's own free warmup. Prefix the first, leave the second.
        if first.get("kind") == "repeat":
            return [{"kind": "warmup", "duration_min": EASE_MIN}, *source]
        return source
    split = _carve(first, "warmup")
    if split is None:
        # Lap-button step: open-ended, so there is no total to preserve.
        return [{"kind": "warmup", "duration_min": EASE_MIN}, *source]
    return [*split, *source[1:]]


def _with_ease_out(source: list[dict]) -> list[dict]:
    """Make the first minutes of any PRESCRIBED cooldown target-free, carved out of it
    (a 15 min Z1 cooldown becomes 5 free + 10 Z1 — same 15 minutes).

    Only touches `kind == 'cooldown'` steps, so plain runs are untouched by design.
    Index 0 is skipped because `_with_lead_in` owns the opening step — which is also
    why this must run BEFORE it: a session opening on a cooldown would otherwise be
    shifted to index 1 by the lead-in and then carved a second time."""
    out: list[dict] = []
    for i, st in enumerate(source):
        split = (
            _carve(st, "cooldown")
            if i > 0 and st.get("kind") == "cooldown" and _has_target(st)
            else None
        )
        out.extend(split if split else (st,))
    return out


def _exe_step(order: int, kind: str, step: dict, child_id: int | None = None) -> dict:
    return {
        "type": "ExecutableStepDTO", "stepId": None, "stepOrder": order,
        "stepType": _STEP_TYPES.get(kind, _STEP_TYPES["work"]), "childStepId": child_id,
        **_end_fields(step.get("duration_min"), step.get("distance_km")),
        **_target_fields(step.get("target_pace"), step.get("target_zone")),
    }


def build_workout(session: dict) -> dict:
    """Session dict (same shape as calendar's _session_dict + 'structure') → Garmin payload.

    With coach-prescribed structure: one Garmin step per structure step, repeat blocks
    as RepeatGroupDTO (one level, matching plan/structure.py). Without: a single main
    step from the session-level end/target fields. Either way the result opens with a
    target-free lead-in, and a prescribed cooldown opens target-free too (see
    `_with_lead_in` / `_with_ease_out`) — the store's `structure` is NOT modified, so the
    plan, the calendar description and the completion check are untouched."""
    source = session.get("structure") or [{
        "kind": "work",
        "duration_min": session.get("duration_min"),
        "distance_km": session.get("distance_km"),
        "target_zone": session.get("target_zone"),
        "target_pace": session.get("target_pace"),
    }]
    steps: list[dict] = []
    order = 1
    for st in _with_lead_in(_with_ease_out(source)):
        if st.get("kind") == "repeat" and st.get("steps"):
            group_order = order
            order += 1
            children = []
            for child in st["steps"]:
                children.append(_exe_step(order, child.get("kind", "work"), child, child_id=1))
                order += 1
            steps.append({
                "type": "RepeatGroupDTO", "stepId": None, "stepOrder": group_order,
                "stepType": _STEP_TYPES["repeat"], "childStepId": 1,
                "numberOfIterations": int(st.get("times") or 1), "smartRepeat": False,
                "endCondition": _END_ITER, "endConditionValue": float(st.get("times") or 1),
                "workoutSteps": children,
            })
        else:
            steps.append(_exe_step(order, st.get("kind", "work"), st))
            order += 1

    return {
        "workoutName": f"J2H4All: {session['title']}"[:80],
        "description": (session.get("purpose") or "")[:1024],
        "sportType": _SPORT,
        "workoutSegments": [{"segmentOrder": 1, "sportType": _SPORT, "workoutSteps": steps}],
    }


# ------------------------------------------------------------------ reconcile

def _status_code(exc: Exception) -> int | None:
    resp = getattr(getattr(exc, "error", None), "response", None)
    return getattr(resp, "status_code", None)


def _delete_remote(gc: GarminClient, s: Session) -> None:
    """Unschedule + delete a session's pushed workout; 404/410 = already gone, fine."""
    for method, path, attr in (
        ("DELETE", endpoints.WORKOUT_UNSCHEDULE.format(schedule_id=s.garmin_schedule_id), "garmin_schedule_id"),
        ("DELETE", endpoints.WORKOUT_ITEM.format(workout_id=s.garmin_workout_id), "garmin_workout_id"),
    ):
        if not getattr(s, attr):
            continue
        try:
            gc.api_write(method, path)
        except Exception as exc:  # noqa: BLE001
            if _status_code(exc) not in (404, 410):
                raise
        setattr(s, attr, None)


def _session_dict(s: Session) -> dict:
    return {
        "title": s.title, "type": s.type, "purpose": s.purpose,
        "duration_min": s.duration_min, "distance_km": s.distance_km,
        "target_zone": s.target_zone, "target_pace": s.target_pace,
        "structure": s.structure,
    }


def reconcile(db: DbSession) -> dict:
    """Make Garmin's workout calendar match active planned run sessions from today
    forward. Same triggers as the Google Calendar reconcile — proposal approval or
    the manual sync button — never a cron."""
    settings = get_settings()
    if not settings.garmin_workout_push_enabled:
        return {"skipped": "workout push disabled"}
    if not settings.garmin_sync_enabled:
        return {"skipped": "garmin sync disabled"}

    gc = GarminClient(db=db)
    today = date.today()
    created = updated = deleted = 0

    # Orphans: superseded sessions (any date) still holding a pushed workout.
    # Past PLANNED sessions keep theirs — Garmin links the recorded activity to
    # the scheduled workout on its own; deleting would erase that linkage.
    orphans = db.scalars(
        select(Session).where(
            Session.garmin_workout_id.isnot(None), Session.status != "planned"
        )
    ).all()
    for s in orphans:
        _delete_remote(gc, s)
        deleted += 1

    # Exclude sessions that already have a linked result (run earlier today):
    # re-PUTting + delete/recreate of the schedule on a completed day would
    # destroy Garmin's activity↔workout linkage, same as the past-planned rule.
    from ..models import SessionResult
    resulted = select(SessionResult.session_id).where(SessionResult.session_id.isnot(None))
    desired = db.scalars(
        select(Session).where(
            Session.status == "planned", Session.type.in_(PUSH_TYPES),
            Session.date >= today, Session.id.not_in(resulted),
        ).order_by(Session.date)
    ).all()
    for s in desired:
        payload = build_workout(_session_dict(s))
        if s.garmin_workout_id:
            payload["workoutId"] = int(s.garmin_workout_id)  # PUT requires the id echoed
            try:
                gc.api_write("PUT", endpoints.WORKOUT_ITEM.format(workout_id=s.garmin_workout_id), payload)
                updated += 1
                # A bare content PUT does NOT trigger Garmin's device fan-out — only
                # scheduling events do (verified live 2026-07-11: a schedule refresh is
                # what finally delivered the plan to the watch). Refresh the schedule
                # (same date) so a revised session actually replaces the old one on
                # the watch at the next phone-app sync.
                if s.garmin_schedule_id:
                    try:
                        gc.api_write("DELETE", endpoints.WORKOUT_UNSCHEDULE.format(schedule_id=s.garmin_schedule_id))
                    except Exception as del_exc:  # noqa: BLE001
                        if _status_code(del_exc) not in (404, 410):
                            raise
                    s.garmin_schedule_id = None  # rescheduled below
            except Exception as exc:  # noqa: BLE001
                if _status_code(exc) not in (404, 410):
                    raise
                s.garmin_workout_id = None  # deleted in Connect — recreate below
                s.garmin_schedule_id = None
        if not s.garmin_workout_id:
            payload.pop("workoutId", None)
            resp = gc.api_write("POST", endpoints.WORKOUT_CREATE, payload)
            s.garmin_workout_id = str(resp["workoutId"])
            created += 1
        if not s.garmin_schedule_id:
            resp = gc.api_write(
                "POST", endpoints.WORKOUT_SCHEDULE.format(workout_id=s.garmin_workout_id),
                {"date": s.date.isoformat()},
            )
            s.garmin_schedule_id = str(resp.get("workoutScheduleId") or resp.get("id"))

    db.commit()
    from ..context.store import stamp_meta
    stamp_meta(db, "last_push_garmin_at")  # surfaced on the web Calendar panel
    result = {"created": created, "updated": updated, "deleted": deleted}
    logger.info("Garmin workout reconcile: %s", result)
    return result
