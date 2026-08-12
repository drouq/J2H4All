"""Goal/plan store: seed known facts, apply approved plans, link results, read
the active plan. Writes here happen only from approved proposals."""

import logging
from collections import defaultdict
from datetime import date, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session as DbSession

from ..models import (
    Activity,
    Goal,
    MacroPlan,
    SecondaryRace,
    Session,
    SessionResult,
)
from ..util import RUN_TYPES
from ..util import as_dt as _as_dt
from ..util import utcnow as _utcnow

logger = logging.getLogger(__name__)
# Session types a run activity may be linked against (planned-vs-actual): a run
# must never attach to a gym/strength or rest session that shares the date.
RUN_SESSION_TYPES = ("long_run", "easy", "recovery", "intervals", "tempo", "race")
# Garmin activity types that complete a planned `strength` (gym) session. Mobility/
# yoga are deliberately excluded: they're a separate routine, not the planned gym
# session, and would falsely mark it done.
STRENGTH_ACTIVITY_TYPES = ("strength_training",)
# How far off its planned date a gym activity may sit and still complete the session.
# A run carries the watch-selected `workoutId`, so a late run finds its own session at
# any distance; gym is never pushed as a structured workout, leaving the date as the
# only evidence — which is why this is a small window rather than a run-style open
# match. Athletes shift a gym day routinely (the case this exists for: a Wednesday
# session done Thursday, which same-day matching left pending forever). Typical gym
# scheduling puts two weekly sessions a few days apart, so ±1 day cannot reach the
# neighbouring slot; widening this past 2 would let one activity claim the other half
# of the week and report both as done.
GYM_LINK_TOLERANCE_DAYS = 1

# The LLM schemas describe `type` as free text; the whole pipeline (rest checks,
# PUSH_TYPES, calendar emoji, trends) does exact matches — so normalize at intake.
CANONICAL_TYPES = {"long_run", "easy", "intervals", "tempo", "recovery", "strength", "rest", "race"}
_TYPE_ALIASES = {
    "easy_run": "easy", "recovery_run": "recovery", "recovery_jog": "recovery",
    "rest_day": "rest", "long": "long_run", "longrun": "long_run",
    "gym": "strength", "strength_training": "strength", "strength_core": "strength",
    "interval": "intervals", "tempo_run": "tempo",
}


def _norm_type(t) -> str:
    raw = str(t or "easy").strip().lower().replace(" ", "_").replace("-", "_")
    norm = _TYPE_ALIASES.get(raw, raw)
    if norm not in CANONICAL_TYPES:
        # Unknown types are kept (they behave as a non-rest, non-push session —
        # calendar event yes, watch push no) but logged so drift is visible.
        logger.warning("apply_sessions: unknown session type %r (kept as-is)", norm)
    return norm


def _pdate(s, default=None):
    if not s:
        return default
    try:
        return date.fromisoformat(str(s)[:10])
    except ValueError:
        return default


# ------------------------------------------------------------------ seed

# A fresh install has no race yet. Rather than crash every caller that assumes a
# Goal row exists, seed an obviously-unconfigured PLACEHOLDER and let the athlete
# replace it. The date is RELATIVE so it can never rot into the past, and the
# floor note says out loud that it isn't real.
#
# Do NOT put a real race here. This module is shared by every install: a hardcoded
# race date is one person's goal silently becoming everyone's. See ROADMAP.md —
# the onboarding flow replaces this seed with a proper wizard.
PLACEHOLDER_HORIZON_DAYS = 182  # ~6 months: a plausible planning horizon
PLACEHOLDER_FLOOR = "PLACEHOLDER GOAL — replace this with your real race"


def ensure_seed(db: DbSession) -> None:
    """Seed a PLACEHOLDER A-goal if absent, so the app is usable before onboarding.
    Idempotent. No secondary races are seeded — the athlete adds their own."""
    if db.scalar(select(func.count(Goal.id))) == 0:
        db.add(Goal(
            format="backyard-ultra", loop_km=6.706, target_laps=24,
            race_date=date.today() + timedelta(days=PLACEHOLDER_HORIZON_DAYS),
            floor_note=PLACEHOLDER_FLOOR,
            stretch_note=None,
            status="active", created_at=_utcnow(),
        ))
    db.commit()



# Goal columns an athlete may set. `status` and `created_at` are ours; `format` is
# validated against the doctrine registry rather than taken as free text.
GOAL_FIELDS = ("format", "race_date", "loop_km", "target_laps", "distance_km",
               "elevation_gain_m", "target_time", "floor_note", "stretch_note")


def is_placeholder(goal) -> bool:
    """True while the goal is still the one `ensure_seed` invented. The whole plan
    is built backwards from the race date, so drafting against a placeholder
    produces a confident plan for a race that doesn't exist."""
    return goal is not None and (goal.floor_note or "").startswith("PLACEHOLDER")


def set_goal(db: DbSession, **fields) -> Goal:
    """Create or update the active A-goal. Skips nulls (same contract as the profile
    and check-in upserts) so a partial edit can't blank the fields it didn't mention.

    Setting any real field clears the placeholder marker — otherwise an athlete who
    fills in their race would still be told their goal isn't configured."""
    from ..coach import formats

    unknown = set(fields) - set(GOAL_FIELDS)
    if unknown:
        raise ValueError(f"unknown goal field(s): {sorted(unknown)}")
    ensure_seed(db)
    goal = db.scalar(select(Goal).where(Goal.status == "active").limit(1))
    real = {k: v for k, v in fields.items() if v is not None}
    if "format" in real:
        # Normalize through the registry so "marathon" becomes "road-marathon" and a
        # typo lands on `generic` rather than silently coaching the wrong race.
        real["format"] = formats.normalize(real["format"])
    if "race_date" in real:
        parsed = _pdate(real["race_date"])
        if parsed is None:
            raise ValueError(f"race_date is not a date: {real['race_date']!r}")
        real["race_date"] = parsed
    changing_format = "format" in real and real["format"] != goal.format
    for key, value in real.items():
        setattr(goal, key, value)
    # Changing format must CLEAR the fields that belonged to the old one. Skip-nulls
    # is right for editing within a format, but across formats it strands values that
    # are meaningless in the new one — a backyard's target_laps survived a switch to
    # marathon and the plan panel duly announced "your marathon - 24 laps".
    if changing_format:
        from ..coach import formats

        keep = set(formats.get(real["format"]).goal_fields)
        for stale in ("loop_km", "target_laps", "distance_km", "elevation_gain_m", "target_time"):
            if stale not in keep and stale not in real:
                setattr(goal, stale, None)
    if real and is_placeholder(goal) and "floor_note" not in real:
        goal.floor_note = None
    db.commit()
    return goal


def goal_view(db: DbSession, today: date | None = None) -> dict:
    from ..coach.schedule import local_today
    ensure_seed(db)
    goal = db.scalar(select(Goal).where(Goal.status == "active").limit(1))
    races = db.scalars(select(SecondaryRace).order_by(SecondaryRace.date)).all()
    today = today or local_today(db)  # their local day, not server UTC
    return {
        "goal": {
            "format": goal.format, "loop_km": goal.loop_km, "target_laps": goal.target_laps,
            "distance_km": goal.distance_km, "elevation_gain_m": goal.elevation_gain_m,
            "target_time": goal.target_time,
            "race_date": goal.race_date.isoformat(),
            "days_to_race": (goal.race_date - today).days,
            "floor_note": goal.floor_note, "stretch_note": goal.stretch_note,
        } if goal else None,
        "secondary_races": [
            {"name": r.name, "date": r.date.isoformat(), "distance_km": r.distance_km,
             "type": r.type, "priority": r.priority, "days_to_race": (r.date - today).days,
             "note": r.note}
            for r in races
        ],
    }


# ------------------------------------------------------------------ apply approved plan

def apply_macro_plan(db: DbSession, payload: dict) -> int:
    """Supersede any active macro plan and write the new one. Returns new id."""
    ensure_seed(db)
    goal = db.scalar(select(Goal).where(Goal.status == "active").limit(1))
    for old in db.scalars(select(MacroPlan).where(MacroPlan.status == "active")).all():
        old.status = "superseded"
    mp = MacroPlan(
        goal_id=goal.id if goal else 0,
        status="active",
        rationale=payload.get("rationale"),
        b_race_approach=payload.get("b_race_approach"),
        phases=payload.get("phases", []),
        created_at=_utcnow(),
    )
    db.add(mp)
    db.flush()
    return mp.id


def apply_sessions(db: DbSession, sessions: list[dict], macro_plan_id: int | None) -> int:
    """Supersede planned sessions in the covered date range, then write the new set.

    Change-transparency lives in the proposal/approval layer; here we just
    materialize the approved set, superseding overlapping planned sessions."""
    if not sessions:
        return 0
    from ..coach.schedule import local_today
    from ..garmin.workouts import PUSH_TYPES

    today = local_today(db)  # their local day — for a zone ahead of UTC, a UTC
    # 'today' would overnight drop/keep the wrong day and orphan a completed session's result.
    # Never re-issue the past, and never supersede a session already RUN today —
    # approving a Sunday-evening review whose block starts that Sunday must not
    # orphan the morning's completed session (its ✅/result linkage would be
    # unrecoverable). A today-dated session is kept only while today has no
    # linked result yet, so a morning red-flag can still rewrite the day ahead.
    today_done = db.scalar(
        select(SessionResult.id)
        .join(Session, SessionResult.session_id == Session.id)
        .where(Session.date == today).limit(1)
    ) is not None
    incoming: list[tuple[date, dict]] = []
    dropped = 0
    for s in sessions:
        d = _pdate(s.get("date"))
        if d is None or d < today or (d == today and today_done):
            dropped += 1
            continue
        incoming.append((d, {**s, "type": _norm_type(s.get("type"))}))
    if dropped:
        logger.info("apply_sessions: dropped %d past/already-run-today/undated session(s)", dropped)
    if not incoming:
        return 0

    # Stable-ID carry-over: superseded sessions donate their calendar
    # event / Garmin workout ids to the NEW sessions on the same date, strictly
    # one-to-one in order — two sessions on one date (e.g. run + gym) must never
    # share an id. Unclaimed donors KEEP their ids: they are superseded, so the
    # calendar/workout reconciles find them as orphans and delete the remote
    # objects properly (nulling them here would leak the remote event/workout).
    lo = min(d for d, _ in incoming)
    hi = max(d for d, _ in incoming)
    event_donors: dict[date, list[Session]] = defaultdict(list)
    workout_donors: dict[date, list[Session]] = defaultdict(list)
    for old in db.scalars(
        select(Session).where(
            Session.status == "planned", Session.date >= lo, Session.date <= hi
        )
    ).all():
        old.status = "superseded"
        if old.calendar_event_id:
            event_donors[old.date].append(old)
        if old.garmin_workout_id:
            workout_donors[old.date].append(old)

    written = 0
    for d, s in incoming:
        event_id = None
        if s.get("type") != "rest" and event_donors.get(d):
            donor = event_donors[d].pop(0)
            event_id = donor.calendar_event_id
            donor.calendar_event_id = None  # ownership moves to the new session
        wid = sid = None
        if s.get("type") in PUSH_TYPES and workout_donors.get(d):
            donor = workout_donors[d].pop(0)
            wid, sid = donor.garmin_workout_id, donor.garmin_schedule_id
            donor.garmin_workout_id = None
            donor.garmin_schedule_id = None
        db.add(Session(
            macro_plan_id=macro_plan_id,
            date=d,
            type=s.get("type", "easy"),
            title=s.get("title", s.get("type", "session")),
            duration_min=s.get("duration_min"),
            distance_km=s.get("distance_km"),
            target_zone=s.get("target_zone"),
            target_pace=s.get("target_pace"),
            purpose=s.get("purpose", ""),
            fueling_note=s.get("fueling_note"),
            structure=s.get("structure"),
            status="planned",
            calendar_event_id=event_id,
            garmin_workout_id=wid,
            garmin_schedule_id=sid,
            created_at=_utcnow(), updated_at=_utcnow(),
        ))
        written += 1
    return written


def apply_onboarding_draft(db: DbSession, payload: dict) -> dict:
    mp_id = apply_macro_plan(db, payload.get("macro_plan", {}))
    n = apply_sessions(db, payload.get("sessions", []), mp_id)
    db.commit()
    return {"macro_plan_id": mp_id, "sessions_written": n}


# ------------------------------------------------------------------ result linking (layer 3)

def find_planned_session(db: DbSession, on_date: date, session_type: str) -> "Session | None":
    """The planned session of `session_type` on `on_date` that a manual "I did this"
    refers to — the one still lacking a result, since that's the one being reported.
    Rest days are never completable. Used by the Telegram mark-done path."""
    stype = _norm_type(session_type)
    if stype == "rest":
        return None
    candidates = db.scalars(
        select(Session).where(
            Session.date == on_date, Session.status == "planned", Session.type == stype,
        ).order_by(Session.id)
    ).all()
    for session in candidates:
        if not db.scalar(select(SessionResult).where(SessionResult.session_id == session.id)):
            return session
    return None


def mark_session_done(db: DbSession, session_id: int, note: str | None = None) -> bool:
    """Record a session as completed on the athlete's own say-so. Returns True if this
    call marked it, False if it already carried a result (idempotent — a double tap on
    the confirmation card can't write twice).

    No `activity_id`: there is no Garmin activity behind this, which is the whole point
    — it exists for a session the watch never recorded, or one it recorded too far off
    the planned day for `link_results` to match. The completion classifier reads
    `completed`, and `NO_DELTA_TYPES` exempts gym from the planned-vs-actual delta, so a
    gym session marked this way lands on ✅ exactly like a watch-linked one; a run marked
    this way has no actuals to compare and so is not second-guessed either.

    This is NOT an approval-gate bypass: nothing about the PLAN changes. It records
    what the athlete did — the same class of write as reconcile's ✅-marking of
    completed reality. The plan itself is untouched, and the athlete is the authority
    on whether they trained."""
    session = db.get(Session, session_id)
    if session is None or session.status != "planned" or session.type == "rest":
        return False
    if db.scalar(select(SessionResult).where(SessionResult.session_id == session.id)):
        return False
    db.add(SessionResult(
        session_id=session.id, activity_id=None, completed=True,
        note=note or "Marked done by the athlete.", created_at=_utcnow(),
    ))
    db.commit()
    return True


def _record_result(db: DbSession, session: "Session | None", act: Activity) -> None:
    """Record what Garmin says happened against the session it fulfilled."""
    db.add(SessionResult(
        session_id=session.id if session else None,
        activity_id=act.id,
        completed=True,
        actual_distance_km=round((act.distance_m or 0) / 1000.0, 2) if act.distance_m else None,
        actual_duration_min=round((act.duration_s or 0) / 60.0, 1) if act.duration_s else None,
        actual_avg_hr=act.avg_hr,
        created_at=_utcnow(),
    ))


def _match_gym_session(db: DbSession, act_date: date, *, exact_only: bool) -> "Session | None":
    """The planned gym session a strength activity on `act_date` completes, or None.

    Same day wins; failing that the nearest day within GYM_LINK_TOLERANCE_DAYS, PAST
    before future at equal distance — an activity off its planned day is usually the
    athlete catching up a session they missed, not doing next week's early. Sessions
    that already carry a result are skipped, so the second gym of a day can't re-credit
    the first's session (and a session marked done by hand stays untouched when the
    watch's copy of the same workout syncs later — pending results are visible here
    because queries autoflush).

    `exact_only` is what keeps the window from stealing: `link_results` places every
    same-day match first, so an activity ON the planned day always outranks a
    neighbouring day's activity reaching for the same session."""
    from datetime import timedelta

    offsets = [0]
    if not exact_only:
        for n in range(1, GYM_LINK_TOLERANCE_DAYS + 1):
            offsets += [-n, n]
    for offset in offsets:
        candidates = db.scalars(
            select(Session).where(
                Session.date == act_date + timedelta(days=offset),
                Session.status == "planned", Session.type == "strength",
            ).order_by(Session.id)
        ).all()
        for session in candidates:
            if not db.scalar(select(SessionResult).where(SessionResult.session_id == session.id)):
                return session
    return None


def link_results(db: DbSession, window_days: int = 45) -> int:
    """Match recent Garmin activities to the planned session they fulfilled and record
    a session_result (planned-vs-actual). Runs link to run sessions (by the workout the
    athlete selected on the watch, else same-day); strength activities link to a planned
    gym session on the same day, or — in a second pass, once every same-day match is
    placed — one within GYM_LINK_TOLERANCE_DAYS. The coaching 'read' is Phase 5; this is
    the linking + raw actuals. Idempotent per activity."""
    from datetime import timedelta

    today = date.today()
    since = today - timedelta(days=window_days)
    acts = db.scalars(
        select(Activity).where(Activity.start_time_utc >= _as_dt(since))
        .order_by(Activity.start_time_utc)  # deterministic: earlier activity claims first
    ).all()
    linked = 0
    late_gym: list[Activity] = []
    for act in acts:
        atype = act.activity_type or ""
        is_run = any(atype.startswith(p) for p in RUN_TYPES)
        is_strength = atype in STRENGTH_ACTIVITY_TYPES
        if not (is_run or is_strength):
            continue
        if db.scalar(select(SessionResult).where(SessionResult.activity_id == act.id)):
            continue  # already recorded
        act_date = (act.start_time_local or act.start_time_utc).date()
        if is_run:
            # Prefer the workout the athlete actually SELECTED on the watch: Garmin stamps
            # the activity with the scheduled workout's id (`raw.workoutId`), so a run done
            # a day (or more) late still links to the session it was meant to fulfil — not
            # to whatever happens to sit on the day they ran it. Falls back to the same-day
            # match for free/unstructured runs that carry no workoutId. (reflect the
            # plan as done, keyed off their watch selection.)
            session = None
            wid = (act.raw or {}).get("workoutId")
            if wid is not None:
                session = db.scalar(
                    select(Session).where(
                        Session.garmin_workout_id == str(wid), Session.status == "planned",
                        Session.type.in_(RUN_SESSION_TYPES),
                    ).order_by(Session.id).limit(1)
                )
            if session is None:
                session = db.scalar(
                    select(Session).where(
                        Session.date == act_date, Session.status == "planned",
                        Session.type.in_(RUN_SESSION_TYPES),  # a run never links to a gym/rest session
                    ).order_by(Session.id).limit(1)
                )
        else:
            # Gym: a strength activity completes a planned `strength` session on the same
            # day, else one within GYM_LINK_TOLERANCE_DAYS — athletes shift a gym day and
            # there is no workoutId to key off (gym isn't a pushed structured workout), so
            # the date is the only evidence available. Only ever links to a session that
            # has no result yet, so a day's activity can't double-count one already
            # credited. Still records nothing when no gym session sits nearby: a stray
            # strength log marks nothing, and leaves no orphan result to lock the
            # activity out.
            session = _match_gym_session(db, act_date, exact_only=True)
            if session is None:
                late_gym.append(act)  # retry below, after every same-day match is placed
                continue
        _record_result(db, session, act)
        linked += 1

    for act in late_gym:
        act_date = (act.start_time_local or act.start_time_utc).date()
        session = _match_gym_session(db, act_date, exact_only=False)
        if session is None:
            continue
        _record_result(db, session, act)
        linked += 1
    db.commit()
    return linked


# ------------------------------------------------------------------ read active plan

def plan_view(db: DbSession, upcoming_days: int = 30) -> dict:
    from datetime import timedelta

    from ..coach import completion
    from ..coach.schedule import local_today
    today = local_today(db)  # their local day, not server UTC
    mp = db.scalar(select(MacroPlan).where(MacroPlan.status == "active").limit(1))
    sessions = db.scalars(
        select(Session).where(
            Session.status == "planned", Session.date >= today,
            Session.date <= today + timedelta(days=upcoming_days),
        ).order_by(Session.date)
    ).all()
    # Attach results for the sessions in view only — this used to load EVERY
    # session_result in the database on each /api/plan call, which grows without
    # bound over a two-year history for a window of ~30 rows.
    session_ids = [s.id for s in sessions]
    result_by_session = {
        r.session_id: r for r in db.scalars(
            select(SessionResult).where(
                SessionResult.session_id.in_(session_ids), SessionResult.completed.is_(True)
            ).order_by(SessionResult.id)
        ).all()
    } if session_ids else {}
    return {
        "macro_plan": {
            "rationale": mp.rationale,
            "b_race_approach": mp.b_race_approach,
            "phases": mp.phases,
        } if mp else None,
        "upcoming_sessions": [
            {
                "id": s.id, "date": s.date.isoformat(), "type": s.type, "title": s.title,
                "duration_min": s.duration_min, "distance_km": s.distance_km,
                "target_zone": s.target_zone, "target_pace": s.target_pace,
                "purpose": s.purpose, "fueling_note": s.fueling_note,
                "structure": s.structure,
                # The same classifier the calendar uses (coach/completion.py), so the
                # web and the calendar can't disagree about the same session — `done`
                # alone called a run that came in a third short "✓ done".
                "done": s.id in result_by_session,
                "status": completion.classify(s, result_by_session.get(s.id), today),
                "deviation": completion.delta_line(s, result_by_session.get(s.id)),
                "deviation_reason": getattr(result_by_session.get(s.id), "deviation_reason", None),
            }
            for s in sessions
        ],
    }
