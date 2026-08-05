"""Post-activity read (PRD §12): after a run syncs, compare planned vs actual and
log a short read. Small deviations are *noted*, not re-planned — the weekly review
(or a red flag) owns adaptation. Sonnet tier (§17).

Also owns the off-plan QUESTION (`ask_about_deviations`): when a session lands more
than 20% off its prescription the coach asks what happened instead of inferring a
cause, and their answer is stored on the result so every later surface reasons from
the real reason. See `coach/completion.py` for why.
"""

import logging
from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession

from ..llm import LLMNotConfigured, call_tool
from ..models import Activity, Preference, Session, SessionResult
from ..util import utcnow as _utcnow
from . import completion, doctrine
from .signals import _feel_label, _rpe_label, recovery_baseline

logger = logging.getLogger(__name__)

# The result we're awaiting an explanation for (internal KV, filtered from prompts).
ASK_KEY = "awaiting_deviation_reason"
# Longer than the debrief's 30 min: this question can land at any hour (it rides a
# sync, not a beat), and they may only see it hours later. `looks_like_question` still
# routes a genuine question past the capture.
ASK_WINDOW = timedelta(hours=6)
# Only ask about a session this recent — otherwise a first deploy, or a backfill,
# would interrogate them about months of history.
ASK_WITHIN_DAYS = 3

READ_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "read": {"type": "string", "description": "1-3 sentences: did they hit the session? over/under-cooked? HR drift vs target? Coach voice, concise."},
        "flagged": {"type": "boolean", "description": "True only if something ACUTE stands out — HR wildly high for the effort (illness?), a big shortfall, or a pattern worth a proactive check between weekly reviews."},
        "flag_reason": {"type": ["string", "null"], "description": "If flagged, the one-line reason; else null."},
    },
    "required": ["read", "flagged", "flag_reason"],
}

def system_prompt(db, today: date | None = None) -> str:
    return (
        "You are the coach in J2H4All reading a single completed run against its plan. Give a brief, honest "
        "read — did they hit it, over/under-cook it, any HR drift vs the target zone. When the `durability` "
        "block is present, ground the drift comment in it: aerobic_decoupling_pct (>~5% on an easy run = "
        "notable cardiac drift / aerobic base still building; low = holding well) and pace_cv_pct (low = "
        "metronomic, the backyard-relevant trait). Don't recite every number — interpret, and when it's "
        "telling, connect the read to what the race demands (see doctrine below). Do NOT propose "
        "plan changes here (that's the weekly review's job). Only set flagged=true for something acute: run "
        "HR far above what the pace/effort warrants (possible illness), a large unexplained shortfall, or a "
        "clear injury signal.\n\n"
        "A shortfall against the planned duration/distance is NOT by itself acute and NOT evidence of "
        "fatigue: you cannot see why a run ended early. State the gap as a fact, say the reason isn't "
        "known yet, and leave it — a separate question asks them directly. Only flag it when a marker or "
        "the run's own HR/effort data actually shows something wrong.\n\n"
        + doctrine.compact_doctrine(db, today)
    )


def run_reads(db: DbSession, today: date | None = None, limit: int = 8) -> list[SessionResult]:
    """Generate reads for planned results that don't have one yet. Returns flagged ones."""
    if today is None:
        from .schedule import local_today
        today = local_today(db)
    pending = db.scalars(
        select(SessionResult).where(
            SessionResult.read_summary.is_(None),
            SessionResult.session_id.isnot(None),  # planned-vs-actual only
        ).order_by(SessionResult.id.desc()).limit(limit)
    ).all()
    if not pending:
        return []

    recovery = recovery_baseline(db, today)
    flagged: list[SessionResult] = []
    for r in pending:
        session = db.get(Session, r.session_id)
        if session is None:
            continue
        act = db.get(Activity, r.activity_id) if r.activity_id else None
        facts = {
            "self_eval": {
                "feel": _feel_label(act.feel) if act else None,
                "rpe": _rpe_label(act.rpe) if act else None,
            },
            "planned": {
                "date": session.date.isoformat(), "type": session.type, "title": session.title,
                "target_zone": session.target_zone, "target_pace": session.target_pace,
                "distance_km": session.distance_km, "duration_min": session.duration_min,
                "purpose": session.purpose,
            },
            "actual": {
                "distance_km": r.actual_distance_km, "duration_min": r.actual_duration_min,
                "avg_hr": r.actual_avg_hr,
            },
            # >20% off the prescription. The gap is stated; the CAUSE is only ever
            # `deviation_reason` (their words) — never inferred from the numbers.
            "off_plan": completion.classify(session, r, today) == completion.PARTIAL,
            "deviation": completion.delta_line(session, r),
            "deviation_reason": r.deviation_reason,
            # Durability from the per-second stream: aerobic decoupling / HR drift /
            # pace consistency — the real numbers behind "HR drift vs target".
            "durability": act.stream_metrics if act else None,
            "recovery_context": recovery,
        }
        try:
            out = call_tool(
                task="post_run_read", system=system_prompt(db, today),
                content="Read this session (JSON):\n" + _json(facts),
                tool_name="record_read", tool_schema=READ_SCHEMA,
                tool_description="Record the planned-vs-actual read.",
            )
        except LLMNotConfigured:
            logger.info("Skipping post-run reads: LLM not configured")
            return []
        except Exception:
            logger.exception("Post-run read failed for result %s", r.id)
            continue
        r.read_summary = out.get("read")
        r.flagged = bool(out.get("flagged"))
        if r.flagged:
            r.note = out.get("flag_reason")
            flagged.append(r)
    db.commit()
    if flagged:
        logger.info("Post-run reads flagged %d result(s)", len(flagged))
    return flagged


def _json(obj) -> str:
    import json
    return json.dumps(obj, default=str)


# --------------------------------------------------------------- the off-plan question

def pending_ask(db: DbSession) -> int | None:
    """The SessionResult id we're awaiting an explanation for, if the window is still
    open. Consumes the flag — the caller has the reply."""
    from .checkin import awaiting_active, clear_awaiting
    if not awaiting_active(db, ASK_KEY, window=ASK_WINDOW):
        return None
    pref = db.scalar(select(Preference).where(Preference.key == ASK_KEY))
    rid = int(pref.value) if pref and (pref.value or "").isdigit() else None
    clear_awaiting(db, ASK_KEY)
    return rid


def _arm_ask(db: DbSession, result_id: int) -> None:
    pref = db.scalar(select(Preference).where(Preference.key == ASK_KEY))
    if pref is None:
        db.add(Preference(key=ASK_KEY, value=str(result_id), updated_at=_utcnow()))
    else:
        pref.value = str(result_id)
        pref.updated_at = _utcnow()
    db.commit()


def record_reason(db: DbSession, result_id: int, text: str) -> SessionResult | None:
    """Store their answer on the result, so the calendar, the weekly review and the
    red-flag path all reason from the stated reason instead of re-guessing."""
    r = db.get(SessionResult, result_id)
    if r is None:
        return None
    r.deviation_reason = text.strip()
    db.commit()
    return r


def ask_about_deviations(db: DbSession, today: date | None = None) -> SessionResult | None:
    """If a recent session came in >20% off plan and we haven't asked yet, ask —
    once, plainly, without proposing a cause. Returns the result asked about.

    Deliberately at most ONE question per sync: several syncs run per day and a burst
    of interrogation about a training week is worse than no question at all."""
    if today is None:
        from .schedule import local_today
        today = local_today(db)
    from ..telegram import send_message_sync
    from .checkin import awaiting_active

    if awaiting_active(db, ASK_KEY, window=ASK_WINDOW):
        return None  # a question is already out; don't stack
    rows = db.execute(
        select(Session, SessionResult)
        .join(SessionResult, SessionResult.session_id == Session.id)
        .where(
            SessionResult.completed.is_(True),
            SessionResult.deviation_asked_at.is_(None),
            SessionResult.deviation_reason.is_(None),
            Session.date >= today - timedelta(days=ASK_WITHIN_DAYS),
        )
        .order_by(Session.date.desc())
    ).all()
    for session, r in rows:
        if completion.classify(session, r, today) != completion.PARTIAL:
            continue
        gap = completion.delta_line(session, r)
        when = "Today's" if session.date == today else f"{session.date.strftime('%A')}'s"
        send_message_sync(
            f"📋 {when} {session.title.lstrip('🏃🚶🏋️⚡🏁 ')} came in at {gap}.\n\n"
            "What happened? Could be anything — time, logistics, heat, gut, legs, or you just "
            "called it. I'd rather ask than guess, and I'll shape the next few days around the "
            "real reason."
        )
        r.deviation_asked_at = _utcnow()
        db.commit()
        _arm_ask(db, r.id)
        logger.info("Asked about off-plan session %s (result %s): %s", session.id, r.id, gap)
        return r
    return None
