"""Approval-card feedback: after an approve tap, the card must report
what actually reached Google Calendar and the Garmin watch — including a skipped
Garmin push, so a silently-reverted flag can't masquerade as a successful sync."""
from app.coach.proposal_actions import _calendar_line, _garmin_line


def test_calendar_counts_reported():
    line = _calendar_line({"calendar": {"created": 3, "updated": 1, "deleted": 0}})
    assert "3 added" in line and "1 updated" in line and "0 removed" in line


def test_calendar_not_connected_is_surfaced():
    line = _calendar_line({"calendar": {"skipped": "calendar not connected"}})
    assert "not connected" in line.lower()


def test_calendar_error_is_surfaced():
    line = _calendar_line({"calendar": {"error": "token expired"}})
    assert "token expired" in line


def test_calendar_absent_is_silent():
    assert _calendar_line({}) == ""


def test_garmin_counts_reported():
    line = _garmin_line({"garmin_workouts": {"created": 2, "updated": 0, "deleted": 1}})
    assert "2 added" in line and "0 updated" in line and "1 removed" in line


def test_garmin_skip_is_surfaced_not_silent():
    # The regression guard: a disabled push must be visible, never an empty string.
    line = _garmin_line({"garmin_workouts": {"skipped": "workout push disabled"}})
    assert line != ""
    assert "not updated" in line.lower() or "off" in line.lower()


def test_garmin_error_is_surfaced():
    line = _garmin_line({"garmin_workouts": {"error": "garth 429"}})
    assert "garth 429" in line


def test_garmin_absent_is_silent():
    assert _garmin_line({}) == ""
