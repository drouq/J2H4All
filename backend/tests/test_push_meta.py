"""Last-push timestamps: recorded as INTERNAL machine-state prefs so they surface
on the web Calendar panel but never leak into the context snapshot or LLM prompts."""
import pytest

from app.context import store


def test_stamp_and_read_roundtrip(db):
    store.stamp_meta(db, "last_push_calendar_at", "2026-07-19T04:29:42+00:00")
    assert store.get_meta(db, "last_push_calendar_at") == "2026-07-19T04:29:42+00:00"


def test_stamp_defaults_to_now(db):
    store.stamp_meta(db, "last_push_garmin_at")
    assert store.get_meta(db, "last_push_garmin_at")  # some ISO timestamp written


def test_push_stamps_are_internal_and_filtered_from_snapshot(db):
    store.stamp_meta(db, "last_push_calendar_at")
    store.stamp_meta(db, "last_push_garmin_at")
    snap = store.snapshot(db)
    keys = {p["key"] for p in snap["preferences"]}
    assert "last_push_calendar_at" not in keys  # must not reach the web panel / prompts
    assert "last_push_garmin_at" not in keys


def test_stamp_meta_refuses_non_internal_key(db):
    # A public key would leak into prompts — the helper must reject it.
    with pytest.raises(AssertionError):
        store.stamp_meta(db, "run_frequency", "x")
