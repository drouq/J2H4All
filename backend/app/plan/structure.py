"""Structured-session steps: the coach can prescribe interval structure that is
pushed to the watch as a Garmin structured workout (docs/garmin-workout-push.md).

One shared JSON-schema fragment (`STRUCTURE_SCHEMA`) used by every LLM tool schema
that emits sessions, so all four generators (onboarding, weekly review, edit-redraft,
red-flag) speak the same dialect. Plain runs carry structure=null; only quality
sessions with real step structure use it. One level of repeat, no nesting.

Step format (all keys always present, nullable):
  {"kind": "warmup|work|recover|cooldown", "duration_min", "distance_km",
   "target_zone", "target_pace"}
  {"kind": "repeat", "times": N, "steps": [simple steps, no repeats]}
"""

_SIMPLE_STEP_PROPS = {
    "kind": {"type": "string", "description": "warmup | work | recover | cooldown"},
    "duration_min": {"type": ["number", "null"], "description": "End after N minutes (use this OR distance_km, not both)"},
    "distance_km": {"type": ["number", "null"], "description": "End after N km"},
    "target_zone": {"type": ["string", "null"], "description": "HR zone target, e.g. Z2 (used only when target_pace is null)"},
    "target_pace": {"type": ["string", "null"], "description": "Pace target, e.g. '4:30-4:50/km' (range preferred) or '5:30/km'"},
}

_SIMPLE_STEP = {
    "type": "object",
    "additionalProperties": False,
    "properties": _SIMPLE_STEP_PROPS,
    "required": list(_SIMPLE_STEP_PROPS),
}

STRUCTURE_SCHEMA = {
    "type": ["array", "null"],
    "description": (
        "Structured steps for interval/quality sessions ONLY — null for plain runs. "
        "Executed on the athlete's watch as a Garmin workout, so give each step exactly one "
        "end condition (duration_min or distance_km) and at most one target: target_pace for "
        "controlled quality work (intervals/tempo), target_zone (HR) for easy/long steps or "
        "when heat/humidity makes pace unreliable — consistent with the doctrine's heat rule. "
        "Use kind='repeat' with 'times' and 'steps' for interval blocks; repeats cannot nest."
    ),
    "items": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            **_SIMPLE_STEP_PROPS,
            "kind": {"type": "string", "description": "warmup | work | recover | cooldown | repeat"},
            "times": {"type": ["integer", "null"], "description": "repeat only: iteration count"},
            "steps": {
                "type": ["array", "null"],
                "description": "repeat only: the steps to repeat (work/recover), no nested repeats",
                "items": _SIMPLE_STEP,
            },
        },
        "required": [*_SIMPLE_STEP_PROPS, "times", "steps"],
    },
}


def _step_phrase(step: dict) -> str:
    bits: list[str] = []
    if step.get("distance_km"):
        bits.append(f"{step['distance_km']} km")
    elif step.get("duration_min"):
        bits.append(f"{step['duration_min']:g} min")
    if step.get("target_pace"):
        bits.append(f"@ {step['target_pace']}")
    elif step.get("target_zone"):
        bits.append(step["target_zone"])
    label = {"warmup": "Warmup", "work": "Run", "recover": "Recover",
             "cooldown": "Cooldown"}.get(step.get("kind", ""), step.get("kind", "step"))
    return f"{label} {' '.join(bits)}".strip()


def describe_structure(structure: list | None) -> list[str]:
    """Human lines for a structure — used in the calendar event description."""
    if not structure:
        return []
    lines: list[str] = []
    for step in structure:
        if step.get("kind") == "repeat" and step.get("steps"):
            inner = " + ".join(_step_phrase(s) for s in step["steps"])
            lines.append(f"• {step.get('times', 1)} × ({inner})")
        else:
            lines.append(f"• {_step_phrase(step)}")
    return lines
