"""Combined end-of-day debrief (21:00 local) — one Telegram prompt that merges the
subjective check-in (feel) and the lifestyle log (life factors Garmin can't see).

Why merged: two evening prompts erode response rate, and the coach reasons over feel
+ lifestyle together anyway ("sore AND a late night AND two beers" is one story). One
tap sets a feel baseline (reusing `checkin.QUICK`); a free-text reply is parsed ONCE
into BOTH the check-in scores AND the lifestyle flags, so nothing is lost vs the two
separate beats. Writes to the existing `Checkin` (scores → the soreness≥4 red-flag
trigger still fires) and `LifestyleLog` (flags → recovery attribution) tables.
Reuses the check-in windowed-await machinery with its own key.

The life factors are ALSO tappable (`lifestyle.TAPS`), not typed-only: six weeks of
production showed him tapping the feel emoji nightly and typing a line roughly never,
so the lifestyle half recorded one row while the check-in half recorded every day
(found 2026-08-03). Both halves are now answerable with a tap, the card keeps its
buttons across taps (`render_card`), and a typed line still refines any field.
"""

import logging
from datetime import date

from sqlalchemy.orm import Session as DbSession

from ..llm import LLMNotConfigured, call_tool
from . import checkin, lifestyle

logger = logging.getLogger(__name__)

AWAITING_KEY = "awaiting_debrief_reply"

_LIFESTYLE_FIELDS = ("alcohol", "illness", "sleep", "nutrition", "training_extra", "stress", "summary")

# One parse covering both halves: 1-5 feel scores + the life factors a watch misses.
PARSE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "energy": {"type": ["integer", "null"], "description": "1 spent .. 5 fresh, or null"},
        "soreness": {"type": ["integer", "null"], "description": "1 none .. 5 very sore, or null"},
        "motivation": {"type": ["integer", "null"], "description": "1 .. 5, or null"},
        "life_stress": {"type": ["integer", "null"], "description": "1 calm .. 5 high, or null"},
        "note": {"type": ["string", "null"], "description": "Anything notable about how he/his body feels, in his words, else null"},
        "alcohol": {"type": ["string", "null"], "description": "Alcohol in his words ('2 beers'), else null"},
        "illness": {"type": ["string", "null"], "description": "Feeling ill/run-down or symptoms, else null"},
        "sleep": {"type": ["string", "null"], "description": "Sleep disruptors — late night, restless legs, travel — else null"},
        "nutrition": {"type": ["string", "null"], "description": "Notable nutrition/diet/hydration, else null"},
        "training_extra": {"type": ["string", "null"], "description": "Extra non-run work (home legs) or fueling practiced, else null"},
        "stress": {"type": ["string", "null"], "description": "Work/life stress events, else null"},
        "summary": {"type": "string", "description": "One short line recapping the day in his words"},
    },
    "required": ["energy", "soreness", "motivation", "life_stress", "note",
                 "alcohol", "illness", "sleep", "nutrition", "training_extra", "stress", "summary"],
}


def render_card(db: DbSession, today: date | None = None) -> tuple[str, list[list[dict]]]:
    """(text, inline_keyboard) for the debrief — feel taps, life-flag taps, and a
    free-text invite. Re-rendered after every tap (buttons kept, ✓ on what's landed)
    so several taps stack on the one card: the feel and the life flags are separate
    questions, and dropping the keyboard after the first tap would make the second
    unanswerable without typing — which is exactly what wasn't happening."""
    if today is None:
        from .schedule import local_today
        today = local_today(db)
    text = (
        "🌙 Daily debrief — how did today land, and anything I can't see on your watch?\n\n"
        "Tap your overall feel, then flag anything that was off. Add a line for detail "
        "worth having — a niggle · how much you drank · travel · nutrition · an extra "
        "workout or fueling you practiced. Taps alone are fine on a quiet day."
    )
    ci = checkin.today_row(db, today)
    feel_key = checkin.quick_key_for(ci)
    flags = lifestyle.logged_flags(db, today)
    logged = ([checkin.QUICK[feel_key]["label"]] if feel_key else []) + [
        lifestyle.TAPS[k]["label"] for k in lifestyle.TAPS if k in flags
    ]
    if lifestyle.CLEAR_TAP in flags:
        logged.append("nothing to flag")
    if logged:
        text += "\n\nLogged: " + " · ".join(logged)

    def _mark(label: str, on: bool) -> str:
        return ("✓ " + label) if on else label

    feel = [{"text": _mark(v["label"], k == feel_key), "callback_data": f"ci:{k}"}
            for k, v in checkin.QUICK.items()]
    life = [{"text": _mark(v["label"], k in flags), "callback_data": f"lf:{k}"}
            for k, v in lifestyle.TAPS.items()]
    keyboard = [feel[:2], feel[2:], life[:2], life[2:],  # two per row for phone width
                [{"text": _mark(lifestyle.CLEAR_LABEL, lifestyle.CLEAR_TAP in flags),
                  "callback_data": f"lf:{lifestyle.CLEAR_TAP}"}]]
    return text, keyboard


def prompt_card(db: DbSession, today: date | None = None) -> tuple[str, list[list[dict]]]:
    """The initial (untouched) debrief card."""
    return render_card(db, today)


def record_reply(db: DbSession, text: str, today: date | None = None) -> None:
    """Parse a free-text debrief ONCE and fan out to both stores: the check-in scores
    (merged onto any earlier tap — only fields the message supports overwrite) and the
    lifestyle flags (raw text always kept)."""
    if today is None:
        from .schedule import local_today
        today = local_today(db)
    try:
        out = call_tool(
            task="checkin_parse",  # Sonnet tier
            system=("Extract an end-of-day debrief from the athlete's message: subjective feel "
                    "scores (1-5) AND the life factors a watch can't see (alcohol, illness, sleep "
                    "disruptors, nutrition, extra workouts, stress). Only fill fields the message "
                    "supports; use null otherwise. Keep his own words."),
            content=text,
            tool_name="record_debrief", tool_schema=PARSE_SCHEMA,
            tool_description="Extract the debrief fields.",
        )
    except LLMNotConfigured:
        logger.info("Debrief parse skipped: LLM not configured")
        checkin._upsert(db, today, note=text)
        lifestyle._upsert(db, today, raw_text=text, data=None)
        return
    except Exception:
        logger.exception("Debrief parse failed; keeping raw text")
        checkin._upsert(db, today, note=text)
        lifestyle._upsert(db, today, raw_text=text, data=None)
        return
    # Check-in half — merges onto any quick tap (_upsert skips None fields).
    checkin._upsert(db, today, energy=out.get("energy"), soreness=out.get("soreness"),
                    motivation=out.get("motivation"), life_stress=out.get("life_stress"),
                    note=out.get("note") or text)
    # Lifestyle half — the raw reply plus the parsed flags.
    lifestyle._upsert(db, today, raw_text=text,
                      data={k: out.get(k) for k in _LIFESTYLE_FIELDS})
