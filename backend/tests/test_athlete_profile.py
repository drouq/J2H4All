"""The athlete profile: who the coach is coaching, as data rather than as prompt text.

This is the table that makes the app reusable. Before it existed, one person's
name, age, pronouns and physiology were hardcoded in `coach/doctrine.py`, so every
install coached a stranger's biography. These tests lock the properties that
matter: the profile reaches the prompts, a partial update can't blank the rest,
and an install with no profile still renders a working (if humbler) coach.
"""
from datetime import date

import pytest

from app.coach import doctrine
from app.context import store


def test_fresh_install_has_no_profile_but_still_coaches(db):
    """An empty profile is a valid state, not an error: someone has just deployed
    and hasn't talked to the coach yet. The prompt must still render, must not
    invent a person, and must say the profile is missing so the coach knows to
    ask rather than to assume."""
    view = store.profile_view(db)
    assert view["configured"] is False
    assert view["pronouns"] == "they/them"      # neutral default, never a guess
    assert view["name"] is None and view["age"] is None

    line = doctrine.identity_line(db)
    assert "the athlete" in line
    assert "they/them" in line
    assert "has not filled in their profile" in line


def test_profile_reaches_both_doctrine_surfaces(db):
    """A name/pronoun set in the store must appear on the expensive AND the cheap
    surface — a coach that uses the right pronouns in the weekly review and the
    wrong ones in the morning brief is worse than one that never knew."""
    store.set_profile(db, name="Alex", pronouns="she/her", birthdate=date(1990, 6, 1))
    for text in (doctrine.full_doctrine(db), doctrine.compact_doctrine(db)):
        assert "Alex" in text
        assert "she/her" in text


def test_age_is_derived_not_stored(db):
    """Age is computed from birthdate against the athlete's LOCAL day, so it can't
    go stale in the database and can't be a day off in their morning window."""
    store.set_profile(db, birthdate=date(1990, 6, 1))
    today = doctrine._default_today(db)
    expected = today.year - 1990 - ((today.month, today.day) < (6, 1))
    assert store.profile_view(db)["age"] == expected


def test_partial_update_does_not_blank_other_fields(db):
    """Skip-nulls, the same contract as the check-in and lifestyle upserts. A chat
    message that only mentions a name must not wipe the pronouns — otherwise every
    conversational correction silently destroys the rest of the profile."""
    store.set_profile(db, name="Alex", pronouns="she/her", data_caveats="Shift worker.")
    store.set_profile(db, name="Alexandra")
    view = store.profile_view(db)
    assert view["name"] == "Alexandra"
    assert view["pronouns"] == "she/her"
    assert view["data_caveats"] == "Shift worker."


def test_empty_pronouns_cannot_clear_the_default(db):
    """`pronouns` is NOT NULL. An empty string from a sloppy caller would either
    break the insert or leave the coach with no pronoun to use at all."""
    store.set_profile(db, pronouns="   ")
    assert store.profile_view(db)["pronouns"] == "they/them"


def test_unknown_field_is_rejected(db):
    """The profile is written from chat extraction. An unrecognised key must raise
    rather than being silently dropped — a typo'd field that quietly does nothing
    is how 'I told the coach and it ignored me' bugs happen."""
    with pytest.raises(ValueError):
        store.set_profile(db, favourite_gel="citrus")


def test_data_caveats_reach_the_prompt_verbatim(db):
    """The single highest-value personalisation: what makes THIS athlete's numbers
    read wrong. It must arrive in the athlete's own words, next to the generic
    sleep-composite rule it qualifies — not buried in a notes dump."""
    store.set_profile(db, data_caveats="Restless legs: sleep score reads low, recovery is fine.")
    text = doctrine.athlete_block(db)
    assert "Restless legs: sleep score reads low, recovery is fine." in text
    assert "DATA CAVEATS" in text


def test_snapshot_exposes_the_profile(db):
    """The web panel and the prompts must read the same source, or they drift."""
    store.set_profile(db, name="Alex", pronouns="he/him")
    snap = store.snapshot(db)
    assert snap["athlete"]["name"] == "Alex"
    assert snap["athlete"]["pronouns"] == "he/him"


def test_diet_is_unspecified_until_told(db):
    """It used to default to one athlete's diet, so every install inherited it and
    the coach confidently reasoned about someone else's fueling."""
    assert store.snapshot(db)["diet"]["diet"] == "unspecified"
    store.apply_items(db, [{"kind": "dietary_note", "diet": "vegan", "text": "No dairy."}])
    assert store.snapshot(db)["diet"]["diet"] == "vegan"
