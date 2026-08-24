"""Approval-card feedback: after an approve tap, the card must report
what actually reached Google Calendar and the Garmin watch — including a skipped
Garmin push, so a silently-reverted flag can't masquerade as a successful sync."""
from app.coach.proposal_actions import _calendar_line, _garmin_line, handle


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


# ---- the tap is acked FIRST, and resolving a card must not destroy it ---------
# An approval takes over a minute on a real plan change (calendar writes + throttled
# Garmin pushes) while Telegram kills a callback query after ~15s, so acking afterwards
# failed every time: the button spun, then errored, while the approval succeeded. And
# replacing the card text with the outcome deleted the review it was asking about.
# Order and preservation are the fixes, so order and preservation are what these pin.


class _Recorder:
    """Patches every outbound telegram call the handler can make, so a test can never
    reach the real API - and so a change of delivery mechanism shows up here."""

    def __init__(self, monkeypatch):
        self.calls = []
        import app.telegram as tg
        monkeypatch.setattr(tg, "answer_callback", lambda cb, text="": self.calls.append(("ack", text)))
        monkeypatch.setattr(tg, "clear_message_buttons",
                            lambda c, m: self.calls.append(("clear", m)))
        monkeypatch.setattr(tg, "send_message_sync",
                            lambda t, chat_id=None, reply_to_message_id=None:
                            self.calls.append(("outcome", t, reply_to_message_id)))
        monkeypatch.setattr(tg, "edit_message_text",
                            lambda c, m, t: self.calls.append(("REWROTE_CARD", t)))

    def order(self):
        return [c[0] for c in self.calls]


def test_approve_acks_before_doing_the_work(db, monkeypatch):
    from app.plan import proposals as plan_proposals
    rec = _Recorder(monkeypatch)
    monkeypatch.setattr(plan_proposals, "approve",
                        lambda _db, pid: rec.calls.append(("approve", pid)) or {"applied": {}})
    handle(db, "apr", 32, "chat", 77, "cb")
    assert rec.order() == ["ack", "approve", "clear", "outcome"]
    assert "REWROTE_CARD" not in rec.order()      # the card text was never touched
    assert rec.calls[-1][2] == 77                 # outcome threaded under the card
    assert "Approved" in rec.calls[-1][1]


def test_approve_acks_exactly_once(db, monkeypatch):
    """One callback query gets one answer; a second is an error, not a courtesy."""
    from app.plan import proposals as plan_proposals
    rec = _Recorder(monkeypatch)

    def _conflict(_db, pid):
        raise plan_proposals.ProposalConflict("already approved")

    monkeypatch.setattr(plan_proposals, "approve", _conflict)
    handle(db, "apr", 32, "chat", 77, "cb")
    assert rec.order().count("ack") == 1
    assert "Already resolved" in rec.calls[-1][1]


def test_rejecting_also_keeps_the_card(db, monkeypatch):
    from app.plan import proposals as plan_proposals
    rec = _Recorder(monkeypatch)
    monkeypatch.setattr(plan_proposals, "reject", lambda _db, pid: None)
    handle(db, "rej", 32, "chat", 77, "cb")
    assert "REWROTE_CARD" not in rec.order()
    assert ("clear", 77) in rec.calls
    assert "Rejected" in rec.calls[-1][1]


def test_a_vanished_proposal_still_updates_the_card(db, monkeypatch):
    """Previously this branch only answered the callback - already dead after a slow
    tap - so the card sat unchanged forever with live buttons."""
    from app.plan import proposals as plan_proposals
    rec = _Recorder(monkeypatch)

    def _missing(_db, pid):
        raise KeyError(pid)

    monkeypatch.setattr(plan_proposals, "approve", _missing)
    handle(db, "apr", 999, "chat", 77, "cb")
    assert rec.order() == ["ack", "clear", "outcome"]
