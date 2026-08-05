"""Weekly review (PRD §12): the main adaptation beat, Sunday evening local. Opus
(§17) reviews the week's execution + recovery trend and *proposes* next-block
session adjustments through the approval flow (§11) — never applied silently.

Because the 30-day detail window means this can revise sessions he's already seen
(§9), the proposal must carry change-transparency: what changed and why.
"""

import json
import logging
from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession

from ..llm import LLMNotConfigured, call_tool
from ..models import MacroPlan, Session
from ..plan.structure import STRUCTURE_SCHEMA
from . import doctrine, signals

logger = logging.getLogger(__name__)

REVIEW_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "summary": {"type": "string", "description": "3-5 sentence review for a Telegram/web card: how the week went (execution + recovery) and what you propose next. Coach voice."},
        "change_note": {"type": "string", "description": "Explicit 'what changed and why' vs the sessions he's already seen (change-transparency, §9). If nothing material changes, say so."},
        "sessions": {
            "type": "array",
            "description": "The revised detailed sessions for the coming ~4 weeks — the full rolling window you were shown (include rest days). Same date format. Keep aligned with the macro phase.",
            "items": {
                "type": "object", "additionalProperties": False,
                "properties": {
                    "date": {"type": "string"}, "type": {"type": "string"},
                    "title": {"type": "string"},
                    "duration_min": {"type": ["integer", "null"]},
                    "distance_km": {"type": ["number", "null"]},
                    "target_zone": {"type": ["string", "null"]},
                    "target_pace": {"type": ["string", "null"]},
                    "purpose": {"type": "string"},
                    "fueling_note": {"type": ["string", "null"]},
                    "structure": STRUCTURE_SCHEMA,
                },
                "required": ["date", "type", "title", "duration_min", "distance_km",
                             "target_zone", "target_pace", "purpose", "fueling_note", "structure"],
            },
        },
    },
    "required": ["summary", "change_note", "sessions"],
}

def system_prompt(db, today: date | None = None) -> str:
    return (
        "You are the head coach in J2H4All, running the WEEKLY REVIEW — the main adaptation beat.\n\n"
        + doctrine.full_doctrine(db, today)
        + "\n\nReview the week's execution and recovery trend, weigh acute vs chronic load, HRV/resting-HR, "
        "sleep, subjective check-ins, and the calendar reality ahead (treadmill windows, travel, race "
        "proximity). `recent_plan_execution` shows the last two weeks planned-vs-done. It distinguishes "
        "`missed` (the day closed with nothing against it — he may still run it, and it keeps its place on "
        "his calendar) from `abandoned` (still undone after the grace window — gone, and treated as history). "
        "Absorb both into the plan ahead, never compensate for them or schedule a make-up; a `skipped_optional` "
        "run is expected behaviour, not a miss at all. Then propose the next ~4 weeks of detailed sessions (the rolling "
        "30-day window you were shown), staying inside the current macro phase and applying the training "
        "doctrine above (time-on-feet, durability as the KPI, rehearsal of fueling and walk/run inside long "
        "runs when the phase calls for it). Date the proposed sessions "
        "starting TOMORROW at the earliest — never today or past days: today's session (done or not) stands, "
        "and past days are history, not something to re-plan. Be explicit about what changed "
        "and why. Conservative when recovery is down; don't chase a missed session with a bigger one. "
        "`structural_agreements` are standing agreements with the athlete (e.g. his weekly run-frequency cap, "
        "'[Optional]' marking, gym structure, long-run day) — they are BINDING on every week you propose, "
        "including weeks you'd otherwise carry over unchanged: carry-over is never a reason to keep a "
        "non-compliant week. If the inherited plan violates an agreement, restructure it and say so in the "
        "change_note. If you believe an agreement should bend, keep the plan compliant and raise the question "
        "in the summary instead — never silently override. At the END of the change_note, append a COMPLIANCE "
        "TALLY built by re-reading the session list you are about to record — one line per proposed week, "
        "citing actual dates: run-days (a short Z1 recovery jog IS a run) with the '[Optional]' one marked, "
        "and each other agreement's sessions (e.g. gym days) by date. Do not assert compliance without the "
        "dates to show it; if the tally exposes a violation, fix the sessions and redo the tally. "
        "Use the `data_freshness` block to judge how much to trust the recovery read: if HRV/readiness are "
        "several days old (the athlete syncs his watch irregularly), lean on execution + check-ins and don't "
        "over-fit the plan to a stale recovery number — say so in the change_note when it matters."
    )


def build_facts(db: DbSession, today: date, horizon_days: int = 30) -> dict | None:
    """The review's fact bundle, shared with prompt_eval.py so dry runs exercise
    EXACTLY what prod runs (the harness had drifted: its private copy predated the
    ahead-trim and would have missed structural_agreements). None = no active plan."""
    mp = db.scalar(select(MacroPlan).where(MacroPlan.status == "active").limit(1))
    if mp is None:
        return None
    current = db.scalars(
        select(Session).where(
            Session.status == "planned", Session.date >= today,
            Session.date <= today + timedelta(days=horizon_days),
        ).order_by(Session.date)
    ).all()
    sig = signals.gather(db, today)
    # Token discipline: `current_upcoming_sessions` below carries the full-fidelity
    # planned window, and `context` already has windows/injuries — drop the lower-
    # fidelity duplicates from `ahead`. (`recent_plan_execution` in `sig` already
    # carries the actuals plus the missed/skipped picture.)
    sig["ahead"] = {k: v for k, v in sig["ahead"].items()
                    if k in ("days_to_A_race", "days_to_next_secondary")}
    return {
        # Hoisted out of context.preferences: buried in the bundle, the 07-12
        # live review kept an inherited 5-run week despite the 3-run agreement.
        # Top-level + named in the system prompt = binding, checkable.
        "structural_agreements": sig["context"].pop("preferences", []),
        "signals": sig,
        "macro_phases": mp.phases,
        "current_upcoming_sessions": [
            {"date": s.date.isoformat(), "type": s.type, "title": s.title,
             "duration_min": s.duration_min, "distance_km": s.distance_km,
             "target_zone": s.target_zone, "target_pace": s.target_pace,
             "purpose": s.purpose, "fueling_note": s.fueling_note,
             "structure": s.structure}
            for s in current
        ],
    }


def run_review(db: DbSession, today: date | None = None, horizon_days: int = 30):
    """Generate a weekly-review proposal (pending). Returns (proposal, summary) or None.

    The horizon matches the PRD §9 rolling ~30-day detail window: the review must
    SEE and re-propose the whole remaining window, or the unreviewed tail survives
    supersede and can contradict the new block at the boundary."""
    if today is None:
        from .schedule import local_today
        today = local_today(db)
    facts = build_facts(db, today, horizon_days)
    if facts is None:
        logger.info("Weekly review skipped: no active macro plan yet")
        return None
    def _call() -> dict:
        return call_tool(
            task="weekly_review", system=system_prompt(db, today),
            content="Full signal bundle + current plan (JSON). Run the weekly review.\n\n"
                    + json.dumps(facts, default=str),
            tool_name="record_weekly_review", tool_schema=REVIEW_SCHEMA,
            tool_description="Record the weekly review + proposed next-block sessions.",
            max_tokens=12000,
            adaptive_thinking=True,  # Opus surface with budget to spare (see llm.call_tool)
        )

    try:
        out = _call()
        if not out.get("sessions"):
            # Rare malformed structured output (everything crammed into `summary`,
            # sessions empty) — seen in a dry run. Retry once rather than issue a
            # broken empty proposal.
            logger.warning("Weekly review returned no sessions; retrying once")
            out = _call()
    except LLMNotConfigured:
        logger.info("Weekly review skipped: LLM not configured")
        return None
    if not out.get("sessions"):
        # Degrade loudly (§2.4): raising routes into the scheduler's cron-failure
        # Telegram alert instead of silently sending an empty review card.
        raise RuntimeError("Weekly review produced no sessions after retry; not creating an empty proposal")

    from ..plan import proposals as plan_proposals
    summary = out.get("summary", "Weekly review.")
    payload = {"sessions": out.get("sessions", []), "change_note": out.get("change_note")}
    proposal = plan_proposals.create(db, kind="sessions", summary=summary, payload=payload, origin="weekly_review")
    logger.info("Weekly-review proposal %s created (%d sessions)", proposal.id, len(payload["sessions"]))
    return proposal, summary
