"""Chat-driven context extraction — the Eponge pattern (PRD §19).

The user talks; Sonnet extracts to typed fields; the API returns the proposed
items; the user confirms/edits; only then does `store.apply_items` write. This
module does the extract half. Timezone-via-chat and treadmill windows (§16) are
just two of the item kinds handled here.
"""

from datetime import date

from ..llm import call_tool

ITEM_KINDS = [
    "dietary_note",
    "blood_marker",
    "availability_window",
    "injury",
    "preference",
    "note",
    "timezone",
]

# Hand-written schema (full control over required/additionalProperties). Every
# item carries `kind` + `summary` (the confirmation line shown to the user) plus
# the fields relevant to its kind; irrelevant fields are left null.
_ITEM_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "kind": {"type": "string", "enum": ITEM_KINDS},
        "summary": {"type": "string", "description": "One-line plain-English confirmation of this item."},
        # blood_marker
        "marker_name": {"type": ["string", "null"], "description": "e.g. ferritin, hemoglobin, vitamin D, B12"},
        "value": {"type": ["number", "null"]},
        "unit": {"type": ["string", "null"]},
        "measured_on": {"type": ["string", "null"], "description": "ISO date YYYY-MM-DD"},
        # availability_window
        "window_type": {"type": ["string", "null"], "description": "e.g. treadmill"},
        "start_date": {"type": ["string", "null"], "description": "ISO date"},
        "end_date": {"type": ["string", "null"], "description": "ISO date, null if open-ended"},
        # injury
        "body_part": {"type": ["string", "null"]},
        "status": {"type": ["string", "null"], "enum": ["active", "resolved", None]},
        # preference / dietary / note / timezone
        "key": {"type": ["string", "null"], "description": "preference key, e.g. long_run_day"},
        "text": {"type": ["string", "null"], "description": "free text for dietary_note / preference value / note"},
        "timezone": {"type": ["string", "null"], "description": "IANA zone, e.g. Europe/London"},
    },
    "required": ["kind", "summary", "marker_name", "value", "unit", "measured_on",
                 "window_type", "start_date", "end_date", "body_part", "status",
                 "key", "text", "timezone"],
}

EXTRACT_TOOL_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {"items": {"type": "array", "items": _ITEM_SCHEMA}},
    "required": ["items"],
}


def _system(today: date, current_tz: str) -> str:
    return (
        "You are the context-extraction step of J2H4All, a single-user running-coach app for one athlete, "
        "an ultra-runner training for a backyard ultra. Read the user's message and extract any "
        "durable facts about their body, life, or constraints into typed items. Only extract things "
        "worth persisting; ignore small talk and transient chatter.\n\n"
        f"Today is {today.isoformat()}. The user's current timezone is {current_tz}.\n\n"
        "Item kinds:\n"
        "- blood_marker: a lab result. Set marker_name, value, unit, measured_on (default today if no date given).\n"
        "- availability_window: a dated training constraint. window_type='treadmill' for treadmill-only periods; "
        "set start_date and end_date (null if open-ended). Resolve relative dates ('next 10 days') against today.\n"
        "- injury: a niggle or injury. Set body_part, status (active/resolved), and text for detail.\n"
        "- dietary_note: a fueling/diet note (they is vegetarian, fixed). Put the note in text.\n"
        "- preference: a structured constraint like long-run day or no-sessions-before time. Set key and text (the value).\n"
        "- timezone: they say where they is ('I'm in London this week'). Set timezone to the IANA zone.\n"
        "- note: anything coaching-relevant that fits no field. Put it in text.\n\n"
        "A single message may yield several items (e.g. 'I'm in Tokyo and on the treadmill for a week' = "
        "a timezone item AND a treadmill availability_window). If nothing is worth persisting, return an empty items list. "
        "Leave every field not relevant to an item's kind as null. Write each summary as a short confirmation the athlete can verify."
    )


def extract_items(text: str, today: date, current_tz: str) -> list[dict]:
    result = call_tool(
        task="context_extraction",
        system=_system(today, current_tz),
        content=text,
        tool_name="record_context",
        tool_schema=EXTRACT_TOOL_SCHEMA,
    )
    return result.get("items", [])
