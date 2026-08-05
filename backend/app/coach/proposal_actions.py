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


def handle(db: DbSession, action: str, proposal_id: int, chat_id: str, message_id: int, cb_id: str) -> None:
    from ..telegram import answer_callback, edit_message_text, send_message_sync
    from ..plan import proposals as plan_proposals
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
        try:
            plan_proposals.reject(db, proposal_id)
            answer_callback(cb_id, "Rejected")
            edit_message_text(chat_id, message_id, "❌ Rejected — plan unchanged.")
        except plan_proposals.ProposalConflict:
            answer_callback(cb_id, "Already resolved")
            edit_message_text(chat_id, message_id, "Already resolved.")
        except KeyError:
            answer_callback(cb_id, "Not found")
        return

    if action == "apr":
        try:
            result = plan_proposals.approve(db, proposal_id)
            answer_callback(cb_id, "Approved ✓")
            edit_message_text(chat_id, message_id,
                              "✅ Approved — plan updated." + _calendar_line(result.get("applied", {}))
                              + _garmin_line(result.get("applied", {})))
        except plan_proposals.ProposalConflict:
            answer_callback(cb_id, "Already resolved")
            edit_message_text(chat_id, message_id, "Already resolved — no change made.")
        except KeyError:
            answer_callback(cb_id, "Not found")
        return
