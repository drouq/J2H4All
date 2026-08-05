"""Race-format doctrine: what THIS kind of race demands, and how it is executed.

The coaching brain is split in two on purpose:

- `coach/doctrine.py` holds the SHARED endurance core — the aerobic engine, the
  ~10%/week ramp, decoupling as the durability KPI, gut training, the medical
  line. That part is the same whether someone is chasing a first marathon or a
  24-hour backyard, and it is the part that has been tuned against real athlete
  data. It does not get rewritten per format.
- The modules here hold the ~30% that genuinely differs: what the race demands,
  how it is executed on the day, and the handful of training additions the format
  requires.

**Why layered rather than one doctrine per format.** Writing a complete standalone
doctrine for each race type is how you end up with several mediocre coaches: the
shared reasoning gets re-derived badly four times and drifts apart. Reading the
original backyard doctrine closely, roughly 70% of the training block was general
endurance principle. Only the race-demands and race-day-execution blocks were
truly format-bound. So those are what a format supplies, and nothing else.

**Formats are a fixed registry, not free text.** A rule that must hold on every
session shouldn't depend on a model inventing the doctrine at onboarding — the
same reasoning that keeps the model tier in config.py and the watch lead-in step
in the payload builder. An unknown format falls back to `generic`, which coaches
sound general endurance rather than guessing at a race it knows nothing about.

⚠️ **Honesty about maturity.** The backyard format is the one that has been
exercised against real athlete data and real evals. The others were written from
established endurance-coaching principle but have NOT been validated against a
real athlete's season. Treat them as a good starting point to tune, not as
finished work, and see ROADMAP.md.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Callable


@dataclass(frozen=True)
class Format:
    """One race format's slice of the coaching brain."""

    key: str
    label: str
    # Goal columns that carry meaning for this format. The rest are left null —
    # `target_laps` means nothing to a marathon, `target_time` little to a backyard.
    goal_fields: tuple[str, ...]
    # Phase vocabulary handed to the macro-plan prompt and its tool schema, so the
    # plan's phase names match the race instead of always saying "backyard-specific".
    phases: str
    # Renders the A-race sentence from the goal row. Must degrade gracefully: a
    # fresh install has a placeholder goal with most fields still null.
    goal_line: Callable[[dict, date, int], str]
    race_demands: str
    # Format-specific ADDITIONS to the shared training doctrine — never a
    # replacement for it.
    training_addenda: str
    execution: str
    # One sentence for the cheap Sonnet surfaces, which can't carry the full text.
    compact: str


def _n(value, suffix: str = "", fmt: str = "g") -> str:
    """Render an optional number, or an empty string. Every goal field can be null
    on a fresh install, and a prompt reading 'a None km race' is worse than one
    that simply doesn't mention the distance."""
    if value is None:
        return ""
    return f"{value:{fmt}}{suffix}"


from . import backyard, generic, road_marathon, road_ultra, trail_ultra  # noqa: E402

FORMATS: dict[str, Format] = {
    f.key: f for f in (
        backyard.SPEC, trail_ultra.SPEC, road_ultra.SPEC,
        road_marathon.SPEC, generic.SPEC,
    )
}

DEFAULT_KEY = generic.SPEC.key

# Tolerated spellings, so a goal written by hand or by the extractor still resolves
# instead of silently dropping the athlete onto the generic coach.
_ALIASES = {
    "backyard": "backyard-ultra", "backyard_ultra": "backyard-ultra", "byu": "backyard-ultra",
    "trail": "trail-ultra", "trail_ultra": "trail-ultra", "ultra-trail": "trail-ultra",
    "mountain-ultra": "trail-ultra", "utmb": "trail-ultra",
    "road_ultra": "road-ultra", "100k": "road-ultra", "50k": "road-ultra",
    "marathon": "road-marathon", "road_marathon": "road-marathon", "42k": "road-marathon",
    "half-marathon": "road-marathon", "half": "road-marathon",
}


def normalize(key: str | None) -> str:
    raw = (key or "").strip().lower().replace(" ", "-").replace("--", "-")
    if raw in FORMATS:
        return raw
    return _ALIASES.get(raw, _ALIASES.get(raw.replace("-", "_"), DEFAULT_KEY))


def get(key: str | None) -> Format:
    """Resolve a Goal.format to its doctrine. Never raises: an unrecognised format
    must degrade to general endurance coaching, not take down every prompt."""
    return FORMATS[normalize(key)]
