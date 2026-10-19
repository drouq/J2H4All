"""The 'Edit' arm of the approval flow: the user taps Edit on a card and
replies with an instruction; the coach redrafts the proposal (Sonnet) and sends a
fresh card. Pending-edit state is persisted (a preference row) so it survives a
restart between the tap and the reply."""

import json
import logging
from datetime import UTC

from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession

from ..llm import LLMNotConfigured, call_tool
from ..models import Preference, Proposal
from ..plan.structure import STRUCTURE_SCHEMA
from ..util import utcnow as _utcnow
from . import doctrine

logger = logging.getLogger(__name__)

_PENDING_KEY = "pending_edit_proposal"

REVISE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "summary": {"type": "string", "description": "2-4 sentences describing the revised proposal for a card."},
        "change_note": {"type": "string", "description": "What changed vs the previous version, per their instruction."},
        "sessions": {
            "type": "array",
            "items": {
                "type": "object", "additionalProperties": False,
                "properties": {
                    "date": {"type": "string"}, "type": {"type": "string"}, "title": {"type": "string"},
                    "duration_min": {"type": ["integer", "null"]}, "distance_km": {"type": ["number", "null"]},
                    "target_zone": {"type": ["string", "null"]}, "target_pace": {"type": ["string", "null"]},
                    "purpose": {"type": "string"}, "fueling_note": {"type": ["string", "null"]},
                    "structure": STRUCTURE_SCHEMA,
                },
                "required": ["date", "type", "title", "duration_min", "distance_km",
                             "target_zone", "target_pace", "purpose", "fueling_note", "structure"],
            },
        },
    },
    "required": ["summary", "change_note", "sessions"],
}


def set_pending_edit(db: DbSession, proposal_id: int) -> None:
    pref = db.scalar(select(Preference).where(Preference.key == _PENDING_KEY))
    if pref is None:
        db.add(Preference(key=_PENDING_KEY, value=str(proposal_id), updated_at=_utcnow()))
    else:
        pref.value = str(proposal_id)
        pref.updated_at = _utcnow()
    db.commit()


def pop_pending_edit(db: DbSession) -> int | None:
    """The proposal id awaiting an edit instruction, cleared on read. Returns None if
    no edit is pending OR the Edit tap is stale (older than REPLY_WINDOW) — a forgotten
    Edit tap must NOT hijack an unrelated free-text message (a debrief, a question) hours
    later, the same windowing the check-in/debrief awaiting flags use."""

    from .checkin import REPLY_WINDOW
    pref = db.scalar(select(Preference).where(Preference.key == _PENDING_KEY))
    if pref is None:
        return None
    pid = int(pref.value)
    stamped = pref.updated_at
    if stamped.tzinfo is None:  # SQLite hands back naive; treat as UTC
        stamped = stamped.replace(tzinfo=UTC)
    db.delete(pref)
    db.commit()
    if _utcnow() - stamped > REPLY_WINDOW:
        return None
    return pid


def carry_through(original: list[dict], revised: list[dict]) -> tuple[list[dict], list[str]]:
    """Restore every original session the redraft left out, by date.

    The redraft REPLACES the proposal, and approving it supersedes the stored plan
    across the whole date span it covers, so a redraft that returns only the
    sessions it touched silently deletes everything in between. In the reference
    deployment a 31-session weekly review, edited twice, came back from the second
    edit as just the 2 sessions whose wording changed; approving it wiped 29
    sessions, including the whole block the card's own text said it had put in, and
    nothing alerted because nothing failed.

    The prompt demands the complete set, but a prompt is not a guarantee. This is
    the guarantee: a date present in the original and absent from the redraft keeps
    its original session(s). Removing a session is expressed as a rest day on that
    date (the prompt says so), so carrying an omitted date through never resurrects
    something the athlete asked to drop. Returns (merged sessions sorted by date,
    the dates carried through)."""
    covered = {str(s.get("date"))[:10] for s in revised}
    kept = [s for s in original if str(s.get("date"))[:10] not in covered]
    carried = sorted({str(s.get("date"))[:10] for s in kept})
    merged = sorted([*revised, *kept], key=lambda s: str(s.get("date"))[:10])
    return merged, carried


def revise_proposal(db: DbSession, proposal_id: int, instruction: str):
    """Redraft a pending sessions-style proposal per the instruction. Returns
    (new_proposal, summary) or None if it can't be revised."""
    p = db.get(Proposal, proposal_id)
    if p is None or p.status != "pending":
        return None
    original = p.payload.get("sessions")
    if not original:
        return None  # macro/onboarding edits are done on the web

    from .schedule import local_today
    today = local_today(db)
    facts = {"instruction": instruction, "current_sessions": original,
             "current_change_note": p.payload.get("change_note")}
    try:
        out = call_tool(
            task="red_flag",  # light re-draft, Sonnet tier
            system=("You are the coach in J2H4All. Revise these proposed sessions per the athlete's "
                    "instruction. Keep everything else sensible and consistent with the doctrine below. "
                    "Every session keeps a purpose.\n\n"
                    "Return the COMPLETE set: every session in current_sessions, with the change "
                    "applied and every unchanged session copied through verbatim. Your output replaces "
                    "the whole proposal, so a session you leave out is a session deleted from their "
                    "plan. To remove a session, put a rest day on that date — never just omit it.\n\n"
                    + doctrine.compact_doctrine(db, today)),
            content="Revise per the instruction (JSON):\n" + json.dumps(facts, default=str),
            tool_name="record_revision", tool_schema=REVISE_SCHEMA,
            tool_description="Record the revised sessions.", max_tokens=8000,
        )
    except LLMNotConfigured:
        return None
    except Exception:
        logger.exception("Proposal revision failed")
        return None
    if not out.get("sessions"):
        # Malformed tool output (sessions crammed into a string field and
        # unsalvageable) — keep the original proposal rather than supersede it
        # with an empty one. The caller reports "couldn't revise".
        logger.warning("Proposal revision returned no sessions; keeping the original proposal")
        return None

    from ..plan import proposals as plan_proposals
    # Just create the redraft: create() already supersedes the prior pending proposal of
    # the same kind+origin in one commit. (The old code reject()ed first, which opened a
    # window where a create() failure left the user with NO proposal — and marked the old
    # one "rejected" rather than the semantically-correct "superseded".)
    summary = out.get("summary", "Revised proposal.")
    sessions, carried = carry_through(original, out.get("sessions", []))
    if carried:
        logger.warning("Proposal revision omitted %d date(s); carried the originals through: %s",
                       len(carried), ", ".join(carried))
    payload = {"sessions": sessions, "change_note": out.get("change_note")}
    new_p = plan_proposals.create(db, kind="sessions", summary=summary, payload=payload, origin=p.origin)
    return new_p, summary
