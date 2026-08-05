"""Coaching conversation (PRD §13): the depth surface. Claude reasons natively
about fueling, recovery, race strategy, and taper over the store — context +
prompting, not new modules. Opus tier (§17); this is where coaching quality lives.

Hard rule (§13): the coach stays in the athletic-nutrition/training lane. It may
flag a marker trend and suggest raising it with a doctor, but never diagnoses or
prescribes dosages/supplement regimens as medical instruction.
"""

import json
import logging
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession

from ..models import BloodMarker, Message
from ..util import utcnow as _utcnow
from ..plan.store import plan_view
from . import doctrine, signals

logger = logging.getLogger(__name__)

HISTORY_TURNS = 12  # recent messages carried for continuity

def system_prompt(db, today: date | None = None) -> str:
    return (
        "You are the athlete's endurance running coach in J2H4All (Journey to Hundred, for All). You know their physiology "
        "(Garmin) and their life, and you coach the whole athlete — not just scheduling: fueling and "
        "day-to-day recovery nutrition, recovery & load (sleep, HRV, resting HR, acute:chronic), race "
        "strategy & pacing for both races, and taper.\n\n"
        + doctrine.full_doctrine(db, today, execution=True)
        + "\n\nYou're given recent per-run detail (recent_runs: date, distance, pace, HR, elevation, training "
        "effect, running power, grade-adjusted pace, HR time-in-zone, running dynamics, per-activity training "
        "load, sweat loss, fastest splits, the weather at the run's start, durability stream metrics, and the "
        "athlete's own self-evaluation (self_eval_feel + self_eval_rpe) — so you can answer questions about a "
        "specific recent run, e.g. 'how was yesterday's run'. Weigh their self-eval heavily: a 'Very Weak' feel "
        "or a high RPE for an easy pace is a real signal worth responding to. For anything older than that "
        "window, say you'd need to check Garmin rather than guess.\n\n"
        "CHANGING THE PLAN: when the athlete clearly asks to change their SCHEDULED sessions — move/lengthen/shorten/"
        "swap/add/remove a session, shift the long-run day, insert a rest day — call the `propose_plan_change` "
        "tool. It does NOT apply anything: it sends them an Approve/Edit/Reject card, and only their approval "
        "writes the plan, calendar, and watch. Rules: (1) emit the COMPLETE `plan.upcoming_sessions` list with "
        "your change applied — copy every unchanged session through verbatim (same dates/titles/durations), "
        "because the whole date range you send REPLACES the stored plan for that range; dropping a session "
        "deletes it. (2) Only for concrete requests about their real schedule — never for hypotheticals ('what if "
        "I…'), questions, or changes beyond the ~14-day window you can see (say you'll handle those at the weekly "
        "review). (3) Still reply in words too: briefly say what you're proposing and that it's waiting for their "
        "approval. For everything else, just coach.\n\n"
        "Be specific and practical, grounded in the data you're given. If the data doesn't support a confident "
        "answer, say so. Keep it conversational, not a wall of text."
    )


def _context_block(db: DbSession, today: date) -> str:
    from ..plan.summary import context_for_prompt, garmin_summary

    # Token discipline (PRD §14): goal facts live in the doctrine (system prompt),
    # latest-per-marker bloods live in life_context — so here bloods carry TREND
    # depth only (last 5 readings per marker), and garmin_summary is called
    # directly instead of computing the whole weekly-review bundle to keep 1 key.
    bloods = db.scalars(select(BloodMarker).order_by(BloodMarker.name, BloodMarker.measured_on)).all()
    by_marker: dict[str, list] = {}
    for b in bloods:
        by_marker.setdefault(b.name, []).append(
            {"value": b.value, "unit": b.unit, "measured_on": b.measured_on.isoformat()}
        )
    ctx = {
        "today": today.isoformat(),
        "life_context": context_for_prompt(db),  # timezone, windows, preferences, coaching notes
        "plan": plan_view(db, upcoming_days=14),
        "recovery": signals.recovery_baseline(db, today),
        "recovery_deep": signals.deep_recovery(db, today),
        "fitness_markers": signals.latest_markers(db),
        "garmin": garmin_summary(db, today),
        "recent_runs": signals.recent_runs(db, today),
        "blood_marker_trends": {k: v[-5:] for k, v in by_marker.items()},
        "recent_checkins": signals.recent_checkins(db, today, days=7),
        "recent_lifestyle": signals.recent_lifestyle(db, today, days=10),
    }
    return json.dumps(ctx, default=str)


def _history(db: DbSession) -> list[dict]:
    rows = db.scalars(select(Message).order_by(Message.id.desc()).limit(HISTORY_TURNS)).all()
    return [{"role": m.role, "content": m.content} for m in reversed(rows)]


def _propose_tool() -> dict:
    """Tool the coach calls to turn a requested plan change into a pending proposal
    (never applied here — approval writes the plan/calendar/watch)."""
    from .revise import REVISE_SCHEMA  # {summary, change_note, sessions[]}
    return {
        "name": "propose_plan_change",
        "description": (
            "Propose a change to the athlete's scheduled upcoming sessions. Emit the COMPLETE "
            "current upcoming_sessions with the change applied (unchanged ones copied through "
            "verbatim) — the date range you send replaces the stored plan for that range. This "
            "only sends an approval card; nothing changes until they approve."
        ),
        "input_schema": REVISE_SCHEMA,
    }


def ask_with_proposal(db: DbSession, question: str, surface: str = "telegram",
                      today: date | None = None):
    """A coaching turn, grounded in the current store, which may also raise a
    plan-change proposal. Returns (answer_text, Proposal | None) — the caller sends
    the approval card, so the §11 gate holds. (A text-only `ask()` existed for the
    web endpoint; both were removed with it on 2026-08-03.)"""
    if today is None:
        from .schedule import local_today
        today = local_today(db)
    db.add(Message(role="user", content=question, surface=surface, created_at=_utcnow()))
    db.commit()

    # history ends with the user turn we just stored; replace it with a richer
    # version that prepends the current store snapshot for grounding.
    history = _history(db)
    final_turn = {"role": "user", "content": [
        {"type": "text", "text": "Current athlete state (JSON, for your reasoning):\n" + _context_block(db, today)},
        {"type": "text", "text": question},
    ]}
    messages = history[:-1] + [final_turn]

    answer, tool_input = call_text_conversation(
        messages, system_prompt(db, today), tools=[_propose_tool()])
    if not answer:
        # A tool-only turn (no prose) still needs a reply so the card has context.
        answer = "Here's a proposed change — review the card below." if tool_input else \
                 "I didn't catch that — could you rephrase?"
    db.add(Message(role="assistant", content=answer, surface=surface, created_at=_utcnow()))
    db.commit()

    proposal = _proposal_from_tool(db, tool_input) if tool_input else None
    return answer, proposal


def _proposal_from_tool(db: DbSession, tool_input: dict):
    """Create a pending sessions proposal from the coach's tool call. Returns the
    Proposal, or None when the tool output carried no usable sessions."""
    sessions = tool_input.get("sessions") if isinstance(tool_input, dict) else None
    if not sessions:
        logger.warning("propose_plan_change returned no sessions; no proposal created")
        return None
    from ..plan import proposals as plan_proposals
    summary = tool_input.get("summary") or "Proposed plan change."
    payload = {"sessions": sessions, "change_note": tool_input.get("change_note")}
    return plan_proposals.create(db, kind="sessions", summary=summary, payload=payload, origin="coach_chat")


def call_text_conversation(messages: list[dict], system: str, tools: list[dict] | None = None):
    """Multi-turn coaching completion (Opus). Separate from llm.call_text so we can
    pass a full message list with a persistent system prompt. Returns
    (text, tool_input | None) — tool_input is the first propose_plan_change call, if any."""
    from ..config import get_settings
    from ..llm import _salvage_xmlish_tool_input, get_client
    client = get_client()
    model = get_settings().model_for("coach_chat")
    kwargs = dict(model=model, max_tokens=8000 if tools else 1500,
                  thinking={"type": "disabled"}, system=system, messages=messages)
    if tools:
        kwargs["tools"] = tools
    # Stream (not .create): a plan-sized proposal is an 8000-token Opus call, and a
    # non-streaming one hits the idle-middlebox disconnect llm.call_tool documents.
    with client.messages.stream(**kwargs) as stream:
        resp = stream.get_final_message()
    text = "".join(b.text for b in resp.content if b.type == "text").strip()
    tool_input = None
    if tools:
        block = next((b for b in resp.content
                      if b.type == "tool_use" and b.name == "propose_plan_change"), None)
        if block is not None:
            schema = next((t["input_schema"] for t in tools if t["name"] == "propose_plan_change"), None)
            fixed = _salvage_xmlish_tool_input(block.input, schema) if schema else None
            tool_input = fixed if fixed is not None else block.input  # salvage or raw (call_tool parity)
    return text, tool_input


