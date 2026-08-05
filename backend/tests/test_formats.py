"""Race-format doctrine.

The property that matters: the SHARED endurance core reaches every athlete, and
the format layer reaches only the athlete whose race it is. The failure this
guards against is the one the app started with — coaching everyone as though they
were running the race the original author happened to be training for.
"""
from datetime import date, timedelta

import pytest

from app.coach import doctrine, formats
from app.models import Goal
from app.util import utcnow


def _goal(db, fmt, **extra):
    db.query(Goal).delete()
    db.add(Goal(format=fmt, race_date=date.today() + timedelta(days=120),
                status="active", created_at=utcnow(), **extra))
    db.commit()


# --------------------------------------------------------------- the registry

def test_every_registered_format_is_complete():
    """A half-written format would render a prompt with a blank section, which is
    worse than an obviously-missing one: the coach would just quietly say less."""
    for key, spec in formats.FORMATS.items():
        assert spec.key == key
        for field in ("label", "phases", "race_demands", "training_addenda", "execution", "compact"):
            value = getattr(spec, field)
            assert isinstance(value, str) and value.strip(), f"{key}.{field} is empty"
        assert callable(spec.goal_line)


@pytest.mark.parametrize("written,expected", [
    ("backyard-ultra", "backyard-ultra"), ("backyard", "backyard-ultra"), ("BYU", "backyard-ultra"),
    ("marathon", "road-marathon"), ("road-marathon", "road-marathon"), ("half", "road-marathon"),
    ("trail", "trail-ultra"), ("UTMB", "trail-ultra"), ("mountain-ultra", "trail-ultra"),
    ("road-ultra", "road-ultra"), ("100k", "road-ultra"),
])
def test_aliases_resolve(written, expected):
    """Formats get typed by hand and written by an LLM extractor. A near-miss must
    land on the right doctrine rather than silently dropping to generic."""
    assert formats.normalize(written) == expected


@pytest.mark.parametrize("bad", [None, "", "   ", "ironman", "swimrun", "not a race"])
def test_unknown_format_degrades_to_generic_and_never_raises(bad):
    """An unrecognised format must not take down every prompt in the app, and must
    NOT quietly inherit whichever format happens to be the default."""
    spec = formats.get(bad)
    assert spec.key == "generic"
    assert "not been set" in spec.compact or "not set" in spec.label


# --------------------------------------------------------- composition per format

@pytest.mark.parametrize("fmt", ["backyard-ultra", "trail-ultra", "road-ultra", "road-marathon", "generic"])
def test_shared_core_reaches_every_format(db, fmt):
    """The tuned endurance core is not rewritten per format — it is the same text
    for everyone, and every athlete must get it."""
    _goal(db, fmt)
    text = doctrine.full_doctrine(db)
    assert "HOW WE TRAIN" in text
    for shared in ("10%/week", "decoupling", "80/20", "Gut training"):
        assert shared in text, f"{fmt} lost shared bullet {shared!r}"
    # And the cross-cutting guardrails.
    assert "HARD MEDICAL LINE" in text
    assert "TIME & TIMEZONE" in text


@pytest.mark.parametrize("fmt,marker", [
    ("backyard-ultra", "hourly reset"),
    ("trail-ultra", "DESCENDING"),
    ("road-ultra", "flat course"),
    ("road-marathon", "Goal pace"),
    ("generic", "ASK the athlete what the race actually is"),
])
def test_each_format_contributes_its_own_race_demands(db, fmt, marker):
    _goal(db, fmt)
    assert marker in doctrine.full_doctrine(db)


def test_backyard_doctrine_does_not_leak_into_a_marathon(db):
    """THE regression this whole split exists to prevent. A marathoner must not be
    told about hourly lap resets, night laps or crewing a pit."""
    _goal(db, "road-marathon", distance_km=42.195, target_time="sub-3:15")
    text = doctrine.full_doctrine(db, execution=True) + doctrine.compact_doctrine(db)
    for leaked in ("hourly reset", "lap", "backyard", "Backyard", "crew", "night laps"):
        assert leaked not in text, f"backyard concept {leaked!r} leaked into a marathon"


def test_marathon_doctrine_does_not_leak_into_a_backyard(db):
    """The mirror. A backyard is not organised around goal pace, and telling an
    ultrarunner that threshold work is the main lever would be actively wrong."""
    _goal(db, "backyard-ultra", loop_km=6.706, target_laps=24)
    text = doctrine.full_doctrine(db, execution=True) + doctrine.compact_doctrine(db)
    for leaked in ("goal pace", "Goal pace", "the wall", "tangents"):
        assert leaked not in text, f"marathon concept {leaked!r} leaked into a backyard"


def test_execution_doctrine_is_opt_in_and_format_specific(db):
    """Race-day execution is only carried on the surfaces that discuss strategy —
    it is a lot of tokens to put on every call."""
    _goal(db, "trail-ultra", distance_km=100, elevation_gain_m=5200)
    assert "RACE-DAY EXECUTION" not in doctrine.full_doctrine(db)
    with_exec = doctrine.full_doctrine(db, execution=True)
    assert "RACE-DAY EXECUTION" in with_exec
    assert "Aid stations" in with_exec          # trail-specific
    assert "hourly routine is scripted" not in with_exec   # backyard-specific


@pytest.mark.parametrize("fmt", ["backyard-ultra", "trail-ultra", "road-ultra", "road-marathon", "generic"])
def test_compact_surface_carries_the_format_too(db, fmt):
    """The cheap Sonnet surfaces (morning brief, post-run read) must know the race
    type as well. They were the ones still saying 'backyard' for everyone."""
    _goal(db, fmt)
    compact = doctrine.compact_doctrine(db)
    assert formats.get(fmt).label in compact
    assert formats.get(fmt).compact.split(" - ")[0][:40] in compact


# ------------------------------------------------------------------ goal rendering

def test_goal_line_uses_the_fields_that_matter_for_the_format(db):
    _goal(db, "road-marathon", distance_km=42.195, target_time="sub-3:15")
    assert "sub-3:15" in doctrine.athlete_block(db)
    _goal(db, "trail-ultra", distance_km=100, elevation_gain_m=5200)
    block = doctrine.athlete_block(db)
    assert "100 km" in block and "5200 m" in block


@pytest.mark.parametrize("fmt", ["backyard-ultra", "trail-ultra", "road-ultra", "road-marathon", "generic"])
def test_goal_line_survives_every_field_being_null(db, fmt):
    """A fresh install has a placeholder goal with almost nothing filled in. The
    prompt must still read as a sentence — never 'a None km race'."""
    _goal(db, fmt)
    block = doctrine.athlete_block(db)
    assert "None" not in block
    assert "A-race" in block


def test_a_format_missing_its_numbers_asks_for_them(db):
    """Better to name the gap than to plan around a number nobody supplied."""
    _goal(db, "trail-ultra")
    assert "aren't recorded yet" in doctrine.athlete_block(db)
    _goal(db, "road-marathon", distance_km=42.195)
    assert "No target time recorded" in doctrine.athlete_block(db)


def test_macro_prompt_phases_come_from_the_format(db):
    """The plan's phase names must match the race. A marathon build being asked for
    a 'backyard-specific' block was the most visible symptom of the old coupling."""
    _goal(db, "road-marathon", distance_km=42.195)
    assert "goal-pace" in doctrine.format_for(db).phases
    _goal(db, "backyard-ultra")
    assert "loop simulation" in doctrine.format_for(db).phases


@pytest.mark.parametrize("module,fn", [
    ("app.coach.brief", "system_prompt"),
    ("app.coach.postrun", "system_prompt"),
])
def test_per_surface_prompts_carry_no_hardcoded_format(db, module, fn):
    """The format split moved race-specific reasoning into coach/formats/, but each
    coaching SURFACE builds its own prompt on top of the doctrine — and three of them
    still named one format in their own text, so a marathoner was told about
    walk/run rehearsal and 'the backyard-relevant trait'. Those surfaces must defer
    to the doctrine block they already include."""
    import importlib

    _goal(db, "road-marathon", distance_km=42.195, target_time="sub-3:15")
    text = getattr(importlib.import_module(module), fn)(db)
    for leaked in ("backyard", "walk/run rehearsal", "hourly reset"):
        assert leaked not in text, f"{module} hardcodes {leaked!r}"


def test_context_extraction_assumes_no_race_format(db):
    """It captures bloods, injuries and travel — none of which depend on the race.
    It used to open by asserting the athlete was 'training for a backyard ultra'."""
    from datetime import date as _d

    from app.context import extract

    text = extract._system(_d(2027, 1, 1), "Europe/London")
    assert "backyard" not in text and "ultra-runner" not in text
