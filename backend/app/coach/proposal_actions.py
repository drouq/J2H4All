"""Apply an inline Approve / Edit / Reject tap to a proposal, with
idempotency — a resolved proposal can't be re-applied."""

import logging

from sqlalchemy.orm import Session as DbSession

logger = logging.getLogger(__name__)


def _calendar_line(applied: dict) -> str:
    cal = applied.get("calendar") if isinstance(applied, dict) else None
    if not cal:
        return ""
    if "skipped" in cal:
        return "\n(Calendar not connected — plan saved in the app.)"
    if "error" in cal:
        return f"\n(Calendar sync issue: {cal['error']})"
    return f"\nCalendar: {cal.get('created',0)} added · {cal.get('updated',0)} updated · {cal.get('deleted',0)} removed."


def _garmin_line(applied: dict) -> str:
    gw = applied.get("garmin_workouts") if isinstance(applied, dict) else None
    if not gw:
        return ""
    if "skipped" in gw:
        # The push is on in prod; a skip means a flag (GARMIN_WORKOUT_PUSH_ENABLED /
        # GARMIN_SYNC_ENABLED) has drifted off. Surface it so a silent config revert
        # can't masquerade as a successful watch update.
        return "\n(Garmin workout push is off — the watch was NOT updated.)"
    if "error" in gw:
        return f"\n(Garmin workout push issue: {gw['error']})"
    return f"\nGarmin workouts: {gw.get('created',0)} added · {gw.get('updated',0)} updated · {gw.get('deleted',0)} removed."


def _resolve_card(chat_id: str, message_id: int, outcome: str) -> None:
    """Retire a card without destroying it: strip the buttons, then post the outcome as
    a threaded reply to the card itself.

    Replacing the card's text with the outcome — the obvious implementation — means
    approving the Sunday weekly review DELETES the review: the reasoning, the change
    note and the session list the athlete was asked to judge, gone at the moment they
    judge it. What they need afterwards is exactly what they needed before: the content,
    plus a record of the decision. Two messages, both kept."""
    from ..telegram import clear_message_buttons, send_message_sync
    clear_message_buttons(chat_id, message_id)          # text untouched, buttons gone
    send_message_sync(outcome, reply_to_message_id=message_id)


def handle(db: DbSession, action: str, proposal_id: int, chat_id: str, message_id: int, cb_id: str) -> None:
    from ..plan import proposals as plan_proposals
    from ..telegram import answer_callback, send_message_sync
    from . import revise

    if action == "edt":
        p = db.get(plan_proposals.Proposal, proposal_id)
        if p is None or p.status != "pending":
            answer_callback(cb_id, "That proposal is no longer open.")
            return
        revise.set_pending_edit(db, proposal_id)
        answer_callback(cb_id, "What should change?")
        send_message_sync("Tell me what to change (e.g. 'move the long run to Sunday, keep it easier') "
                          "and I'll redraft. Approve/Reject are still on the card above.")
        return

    if action == "rej":
        answer_callback(cb_id, "Rejected")
        try:
            plan_proposals.reject(db, proposal_id)
            _resolve_card(chat_id, message_id, "❌ Rejected — plan unchanged.")
        except plan_proposals.ProposalConflict:
            _resolve_card(chat_id, message_id, "Already resolved — no change made.")
        except KeyError:
            _resolve_card(chat_id, message_id, "That proposal no longer exists.")
        return

    if action == "apr":
        # Acknowledge the tap BEFORE doing the work. Telegram invalidates a callback
        # query after ~15s, and an approval takes far longer: it writes the store, then
        # reconciles the calendar (tens of event operations) and pushes every workout to
        # Garmin (each PUT + unschedule + reschedule, rate-limit throttled) — over a
        # minute end to end on a real plan change. Acking afterwards therefore ALWAYS
        # failed on exactly the approvals that matter: the button spun until it errored
        # while the approval was in fact succeeding, which is the most alarming possible
        # rendering of "it worked". The outcome still arrives, in the card reply below.
        answer_callback(cb_id, "Approving…")
        try:
            result = plan_proposals.approve(db, proposal_id)
            _resolve_card(chat_id, message_id,
                          "✅ Approved — plan updated." + _calendar_line(result.get("applied", {}))
                          + _garmin_line(result.get("applied", {})))
        except plan_proposals.ProposalConflict:
            _resolve_card(chat_id, message_id, "Already resolved — no change made.")
        except KeyError:
            # No second answer_callback: one query gets one answer, and we already used it.
            _resolve_card(chat_id, message_id, "That proposal no longer exists.")
        return
