"""Opus-driven plan generation (PRD §9, §14, §17).

Two generators, both returning proposed payloads (nothing is written here — the
approval flow writes on approval, §11):
- generate_macro_plan: dated phases + weekly targets to race day (layer 1)
- generate_sessions: the next ~30 days of detailed sessions (layer 2)

The coach reasons over rolled-up Garmin summaries + context (§14), and must see
the backyard-specific nature (§5) and the B-race interplay.
"""

import json
import logging
from datetime import date

from ..coach import doctrine
from ..llm import call_tool
from .structure import STRUCTURE_SCHEMA
from .summary import context_for_prompt, garmin_summary

logger = logging.getLogger(__name__)

SESSION_WINDOW_DAYS = 30

MACRO_TOOL_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "rationale": {"type": "string", "description": "Why this periodization, in 3-5 sentences, grounded in his current fitness and recovery."},
        "b_race_approach": {"type": "string", "description": "How the B-race (if any) is handled: mini-taper (3-5 day sharpen, not a full taper) then a deliberate rebound before the final backyard-specific block."},
        "phases": {
            "type": "array",
            "description": "Dated phases from now to race day: base -> build -> backyard-specific -> taper, with any B-race slotted in.",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "name": {"type": "string", "description": "e.g. Base, Build, Backyard-specific, B-race sharpen, Rebound, Taper"},
                    "start_date": {"type": "string", "description": "ISO date"},
                    "end_date": {"type": "string", "description": "ISO date"},
                    "focus": {"type": "string", "description": "Primary adaptation this phase targets"},
                    "weekly_km_low": {"type": "number"},
                    "weekly_km_high": {"type": "number"},
                    "intensity_note": {"type": "string", "description": "Intensity distribution / key sessions for the phase"},
                },
                "required": ["name", "start_date", "end_date", "focus", "weekly_km_low", "weekly_km_high", "intensity_note"],
            },
        },
    },
    "required": ["rationale", "b_race_approach", "phases"],
}

SESSIONS_TOOL_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "sessions": {
            "type": "array",
            "description": f"Every training day in the next {SESSION_WINDOW_DAYS} days. Include rest days.",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "date": {"type": "string", "description": "ISO date"},
                    "type": {"type": "string", "description": "long_run | easy | intervals | tempo | recovery | strength | rest | race"},
                    "title": {"type": "string", "description": "Short label, e.g. 'Long Run — 3h30 Z2'"},
                    "duration_min": {"type": ["integer", "null"]},
                    "distance_km": {"type": ["number", "null"]},
                    "target_zone": {"type": ["string", "null"], "description": "e.g. Z2, Z3-Z4"},
                    "target_pace": {"type": ["string", "null"], "description": "e.g. 5:30/km, or null"},
                    "purpose": {"type": "string", "description": "The 'why' — what this session builds toward. Always present."},
                    "fueling_note": {"type": ["string", "null"], "description": "Carb target / in-run fueling if relevant, else null"},
                    "structure": STRUCTURE_SCHEMA,
                },
                "required": ["date", "type", "title", "duration_min", "distance_km", "target_zone", "target_pace", "purpose", "fueling_note", "structure"],
            },
        }
    },
    "required": ["sessions"],
}

def _system_base(db, today: date) -> str:
    return (
        "You are the head coach in J2H4All, a personal running-coach system.\n\n"
        + doctrine.full_doctrine(db, today)
        + "\n\nRespect his stated availability windows (treadmill periods), injuries, and preferences."
    )


def _facts_block(db, today: date) -> str:
    from .store import goal_view

    facts = {
        "today": today.isoformat(),
        **goal_view(db, today),  # goal + secondary_races from the store (store is truth, §2.2)
        "garmin_summary": garmin_summary(db, today),
        "context": context_for_prompt(db),
    }
    return json.dumps(facts, default=str)


def _call_guarded(what: str, key: str, call) -> dict:
    """The malformed-tool-output failure mode (everything crammed into the first
    string field) can survive llm.py's salvage when an embedded array is truncated
    mid-JSON. Persisting such a payload breaks the proposal card — retry once,
    then degrade loudly (§2.4) instead of returning a plan with no {key}."""
    payload = call()
    if not payload.get(key):
        logger.warning("%s returned no %s; retrying once", what, key)
        payload = call()
    if not payload.get(key):
        raise RuntimeError(f"{what} produced no {key} after retry; refusing to draft a broken plan")
    return payload


def generate_macro_plan(db, today: date) -> dict:
    system = (
        _system_base(db, today)
        + "\n\nBuild a periodized MACRO PLAN from now to race day: dated phases (base -> build -> "
        "backyard-specific -> taper) with weekly volume ranges and intensity focus, with the B-race's "
        "mini-taper and rebound slotted in. Ground the ramp in his current weekly volume and acute:chronic "
        "load — do not prescribe a jump his recent training doesn't support. Phases must be contiguous and end on race day."
    )
    return _call_guarded("Macro-plan generation", "phases", lambda: call_tool(
        task="macro_plan",
        system=system,
        content="Here is the athlete's current state as JSON. Generate the macro plan.\n\n" + _facts_block(db, today),
        tool_name="record_macro_plan",
        tool_schema=MACRO_TOOL_SCHEMA,
        max_tokens=6000,
        tool_description="Record the periodized macro training plan.",
        adaptive_thinking=True,  # Opus surface with budget to spare (see llm.call_tool)
    ))


def generate_sessions(db, today: date, macro: dict, days: int = SESSION_WINDOW_DAYS) -> dict:
    system = (
        _system_base(db, today)
        + f"\n\nGenerate detailed daily sessions for the next {days} days, consistent with the macro plan's "
        "current phase. Every session carries a 'purpose' (the why). Include easy/recovery days and rest days. "
        "Add fueling notes where relevant (long runs, backyard-specific work). Respect treadmill windows and injuries. "
        "The athlete's `context.preferences` are standing AGREEMENTS, not hints: before recording, count the "
        "run-days in each week (a short Z1 recovery jog IS a run) against his run-frequency cap, mark any "
        "optional run with '[Optional]' in its title, and structure strength days around his stated gym habits."
    )
    content = (
        "Athlete state and the approved macro plan as JSON. Generate the next "
        f"{days} days of sessions.\n\nMACRO_PLAN:\n" + json.dumps(macro, default=str)
        + "\n\nSTATE:\n" + _facts_block(db, today)
    )
    return _call_guarded("Session generation", "sessions", lambda: call_tool(
        task="macro_plan",  # session generation is part of the high-stakes plan work (Opus)
        system=system,
        content=content,
        tool_name="record_sessions",
        tool_schema=SESSIONS_TOOL_SCHEMA,
        max_tokens=12000,
        tool_description="Record the detailed daily sessions for the rolling window.",
        adaptive_thinking=True,  # Opus surface with budget to spare (see llm.call_tool)
    ))


def generate_onboarding_draft(db, today: date) -> dict:
    """Draft-first onboarding (§14): baseline macro plan + first 30-day block, as one proposal payload."""
    macro = generate_macro_plan(db, today)
    sessions = generate_sessions(db, today, macro)
    return {"macro_plan": macro, "sessions": sessions.get("sessions", [])}
