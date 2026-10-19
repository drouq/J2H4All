"""A race date that isn't decided yet, and secondary-race notes reaching the coach.

Athletes often know roughly when their race is before they know the day — a ballot,
a choice between two events, a race they are organising themselves. The goal row
carries the earliest candidate (`date_window_start`) while `race_date` stays the
planning anchor, and the doctrine names the window and the point where the two
plans fork."""
from datetime import date

import pytest

from app.coach import doctrine
from app.models import SecondaryRace
from app.plan import store as plan_store
from app.util import utcnow

TODAY = date(2026, 10, 19)


def _goal(db, **kw):
    fields = {"format": "road-ultra", "race_date": "2026-12-26",
              "date_window_start": "2026-11-28", "distance_km": 100.0, **kw}
    plan_store.set_goal(db, **fields)


def test_window_is_stored_and_shown(db):
    _goal(db)
    g = plan_store.goal_view(db, TODAY)["goal"]
    assert g["date_window_start"] == "2026-11-28"
    assert g["race_date"] == "2026-12-26"


def test_empty_string_clears_the_window_once_the_date_is_decided(db):
    _goal(db)
    plan_store.set_goal(db, race_date="2026-11-28", date_window_start="")
    g = plan_store.goal_view(db, TODAY)["goal"]
    assert g["date_window_start"] is None
    assert g["race_date"] == "2026-11-28"


def test_editing_another_field_keeps_the_window(db):
    _goal(db)
    plan_store.set_goal(db, distance_km=101.0)
    assert plan_store.goal_view(db, TODAY)["goal"]["date_window_start"] == "2026-11-28"


def test_window_must_start_before_the_anchor(db):
    with pytest.raises(ValueError):
        _goal(db, date_window_start="2026-12-30")
    with pytest.raises(ValueError):
        _goal(db, date_window_start="late november")


def test_undecided_date_names_the_window_and_the_fork(db):
    _goal(db)
    block = doctrine.athlete_block(db, TODAY)
    assert "DATE NOT YET FIXED" in block
    assert "between 2026-11-28 and 2026-12-26" in block
    # The fork is a taper's length before the earliest candidate.
    assert "around 2026-11-14 (26 days from today)" in block
    assert "DATE NOT FIXED" in doctrine.compact_doctrine(db, TODAY)


def test_fork_past_says_the_early_date_is_off(db):
    _goal(db)
    assert "has PASSED" in doctrine.athlete_block(db, date(2026, 11, 20))


def test_a_fixed_date_says_nothing_about_windows(db):
    _goal(db, date_window_start=None)
    assert "DATE NOT" not in doctrine.full_doctrine(db, TODAY)
    assert "DATE NOT" not in doctrine.compact_doctrine(db, TODAY)


def test_secondary_race_inside_the_window_is_planned_both_ways(db):
    _goal(db)
    db.add(SecondaryRace(name="City Marathon", date=date(2026, 12, 6), distance_km=42.195,
                         type="road", priority="B", note="Would like sub-4, not a target.",
                         created_at=utcnow()))
    db.commit()
    block = doctrine.athlete_block(db, TODAY)
    assert "City Marathon, 42.195 km road" in block
    assert "falls INSIDE the A-race date window" in block
    # The athlete's own note on the race reaches the coach — it never used to.
    assert "Would like sub-4, not a target." in block
