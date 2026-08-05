"""Beat scheduling: a beat fires on the first tick at/after its local slot, within
a bounded catch-up window. Exact-hour matching used to lose the whole day's beat if
a single tick was skipped or ran late (Render crons can slip). The window must not
cross midnight — a long outage must never deliver a "morning" brief in the evening,
and the once-per-local-day claim has to stay keyed to the right local date."""
from datetime import datetime

from app.coach.schedule import CATCHUP_HOURS, _due_now


def _at(hour, minute=0):
    return datetime(2026, 7, 22, hour, minute)  # a Wednesday


def test_fires_exactly_on_the_slot():
    assert _due_now(_at(10), 10) is True


def test_does_not_fire_before_the_slot():
    assert _due_now(_at(9, 59), 10) is False


def test_respects_the_scheduled_minute():
    assert _due_now(_at(10, 29), 10, 30) is False
    assert _due_now(_at(10, 30), 10, 30) is True


def test_catches_up_after_a_skipped_tick():
    """The 10:00 tick was skipped — 11:00 and 12:00 must still deliver the brief."""
    assert _due_now(_at(11), 10) is True
    assert _due_now(_at(12), 10) is True


def test_gives_up_outside_the_catch_up_window():
    """A long outage must not fire a 'morning' brief in the evening."""
    assert _due_now(_at(10 + CATCHUP_HOURS), 10) is False
    assert _due_now(_at(20), 10) is False


def test_late_evening_beat_never_spills_past_midnight():
    """22:00 debrief may catch up to 23:59 but must not leak into the next day —
    the window is capped by the end of the day, not wrapped."""
    assert _due_now(_at(22), 22) is True
    assert _due_now(_at(23, 59), 22) is True
    assert _due_now(_at(0), 22) is False   # next local day: not due
    assert _due_now(_at(1), 22) is False


def test_sunday_2300_review_window_is_the_last_hour_of_the_day():
    assert _due_now(_at(23), 23) is True
    assert _due_now(_at(22, 59), 23) is False
