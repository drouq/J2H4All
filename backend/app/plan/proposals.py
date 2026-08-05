"""Approval flow: a proposal is created (pending), surfaced for review,
and only on explicit approval does it write to the store. Idempotent — a resolved
proposal can't be applied again.

Only ONE plan proposal is actionable at a time: creating a fresh proposal of the
same kind+origin supersedes the previous pending one (a re-draft or this week's
review replaces last week's unapproved card), and approving ANY proposal
supersedes every other pending one — a stale ignored card can never silently
roll back a newer approved plan. Superseded cards answer "Already resolved"."""

import logging

from sqlalchemy import select, update
from sqlalchemy.orm import Session as DbSession

from ..models import Proposal
from ..util import utcnow as _utcnow
from . import store

logger = logging.getLogger(__name__)


class ProposalConflict(RuntimeError):
    """Proposal already resolved — refuse to double-apply."""


def create(db: DbSession, kind: str, summary: str, payload: dict, origin: str = "web") -> Proposal:
    # A fresh proposal replaces the previous pending one of the same kind+origin
    # (e.g. onboarding re-draft, or this Sunday's review vs last Sunday's ignored one).
    stale = db.execute(
        update(Proposal)
        .where(Proposal.status == "pending", Proposal.kind == kind, Proposal.origin == origin)
        .values(status="superseded", resolved_at=_utcnow())
    ).rowcount
    if stale:
        logger.info("Superseded %d stale pending %s/%s proposal(s)", stale, kind, origin)
    p = Proposal(kind=kind, status="pending", origin=origin, summary=summary,
                 payload=payload, created_at=_utcnow())
    db.add(p)
    db.commit()
    return p


def _view(p: Proposal) -> dict:
    return {
        "id": p.id, "kind": p.kind, "status": p.status, "origin": p.origin,
        "summary": p.summary, "payload": p.payload,
        "created_at": p.created_at.isoformat(),
        "resolved_at": p.resolved_at.isoformat() if p.resolved_at else None,
    }


def list_pending(db: DbSession) -> list[dict]:
    rows = db.scalars(
        select(Proposal).where(Proposal.status == "pending").order_by(Proposal.id.desc())
    ).all()
    return [_view(p) for p in rows]


def get(db: DbSession, proposal_id: int) -> dict | None:
    p = db.get(Proposal, proposal_id)
    return _view(p) if p else None


def approve(db: DbSession, proposal_id: int, edited_payload: dict | None = None) -> dict:
    """Apply the proposal's payload (or the user's edited version) and mark approved.
    Raises ProposalConflict if already resolved (idempotency)."""
    p = db.get(Proposal, proposal_id)
    if p is None:
        raise KeyError(proposal_id)
    # Atomic claim (compare-and-set): two concurrent approvals — a Telegram tap
    # and a web click — must not both pass a read-then-check and double-apply.
    claimed = db.execute(
        update(Proposal)
        .where(Proposal.id == proposal_id, Proposal.status == "pending")
        .values(status="approved", resolved_at=_utcnow())
    ).rowcount
    db.commit()
    if not claimed:
        db.refresh(p)
        raise ProposalConflict(f"proposal {proposal_id} is already {p.status}")

    payload = edited_payload if edited_payload is not None else p.payload
    applied: dict = {}
    try:
        if p.kind == "onboarding_draft":
            applied = store.apply_onboarding_draft(db, payload)
        elif p.kind == "macro_plan":
            mp_id = store.apply_macro_plan(db, payload)
            db.commit()
            applied = {"macro_plan_id": mp_id}
        elif p.kind == "sessions":
            incoming = payload.get("sessions", [])
            n = store.apply_sessions(db, incoming, payload.get("macro_plan_id"))
            db.commit()
            # dropped = past/today-already-run/undated sessions filtered at apply —
            # surfaced so a silently-shrunk block is visible to the caller.
            applied = {"sessions_written": n, "sessions_dropped": len(incoming) - n}
        else:
            raise ProposalConflict(f"unknown proposal kind {p.kind!r}")
    except Exception:
        # Apply failed — release the claim so the proposal can be retried.
        db.rollback()
        p.status = "pending"
        p.resolved_at = None
        db.commit()
        raise

    if edited_payload is not None:
        p.payload = payload  # keep the version that was actually applied
    # An approved plan invalidates every other pending proposal — a stale ignored
    # card (old red-flag, old draft) must not be able to roll this plan back later.
    others = db.execute(
        update(Proposal)
        .where(Proposal.status == "pending", Proposal.id != proposal_id)
        .values(status="superseded", resolved_at=_utcnow())
    ).rowcount
    if others:
        logger.info("Approval of proposal %d superseded %d other pending proposal(s)", proposal_id, others)
    db.commit()

    # Approval writes the session store AND the calendar (and, when
    # enabled, Garmin workouts). The store is already committed (source of truth);
    # both pushes are best-effort so an outage never rolls back an approved plan —
    # it degrades loudly by returning the error instead.
    applied["calendar"] = _reconcile_calendar(db)
    applied["garmin_workouts"] = _push_garmin_workouts(db)
    return {"applied": applied}


def _reconcile_calendar(db: DbSession) -> dict:
    # Shared best-effort wrapper (also used by the daily-sync cron) — guarded by
    # connection state, never raises, so an approved plan is never rolled back.
    from ..calendar.sync import safe_reconcile
    return safe_reconcile(db)


def _push_garmin_workouts(db: DbSession) -> dict:
    from ..garmin import workouts as garmin_workouts

    try:
        return garmin_workouts.reconcile(db)  # returns {"skipped": ...} when the flag is off
    except Exception as exc:  # noqa: BLE001 — never fail an approved plan on a Garmin error
        logger = __import__("logging").getLogger(__name__)
        logger.exception("Garmin workout push after approval failed")
        return {"error": f"garmin workout push failed: {exc}"}


def reject(db: DbSession, proposal_id: int) -> None:
    p = db.get(Proposal, proposal_id)
    if p is None:
        raise KeyError(proposal_id)
    # Atomic claim (compare-and-set), like approve() — a read-then-set could clobber a
    # concurrent web-Approve with a "rejected" write, defeating the idempotency invariant.
    claimed = db.execute(
        update(Proposal)
        .where(Proposal.id == proposal_id, Proposal.status == "pending")
        .values(status="rejected", resolved_at=_utcnow())
    ).rowcount
    db.commit()
    if not claimed:
        db.refresh(p)
        raise ProposalConflict(f"proposal {proposal_id} is already {p.status}")
