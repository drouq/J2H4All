"""Approval-flow invariants (PRD §11/§21): create supersedes the same kind+origin,
approve is idempotent (no double-apply), and approving supersedes every other
pending card so a stale one can't roll back a newer plan."""
from datetime import date, timedelta

import pytest

from app.models import Proposal
from app.plan import proposals


def _sessions_payload():
    d = (date.today() + timedelta(days=2)).isoformat()
    return {"sessions": [{"date": d, "type": "easy", "title": "e", "purpose": "p"}]}


@pytest.fixture(autouse=True)
def _no_side_effects(monkeypatch):
    # Approval fires best-effort calendar + Garmin pushes; keep tests hermetic/offline.
    monkeypatch.setattr(proposals, "_reconcile_calendar", lambda db: {"skipped": "test"})
    monkeypatch.setattr(proposals, "_push_garmin_workouts", lambda db: {"skipped": "test"})


def test_create_supersedes_same_kind_and_origin(db):
    proposals.create(db, "sessions", "first", _sessions_payload(), origin="weekly_review")
    proposals.create(db, "sessions", "second", _sessions_payload(), origin="weekly_review")
    pending = proposals.list_pending(db)
    assert len(pending) == 1
    assert pending[0]["summary"] == "second"


def test_approve_is_idempotent(db):
    p = proposals.create(db, "sessions", "s", _sessions_payload(), origin="weekly_review")
    res = proposals.approve(db, p.id)
    assert res["applied"]["sessions_written"] == 1
    with pytest.raises(proposals.ProposalConflict):
        proposals.approve(db, p.id)


def test_approve_supersedes_other_pending(db):
    keep = proposals.create(db, "sessions", "weekly", _sessions_payload(), origin="weekly_review")
    other = proposals.create(db, "onboarding_draft", "draft", {"macro_plan": {}, "sessions": []}, origin="web")
    proposals.approve(db, keep.id)
    assert db.get(Proposal, other.id).status == "superseded"


def test_reject_is_idempotent(db):
    p = proposals.create(db, "sessions", "s", _sessions_payload(), origin="weekly_review")
    proposals.reject(db, p.id)
    with pytest.raises(proposals.ProposalConflict):
        proposals.reject(db, p.id)


def test_reject_after_approve_does_not_clobber(db):
    # The CAS: a late Reject must NOT overwrite an already-applied approval.
    p = proposals.create(db, "sessions", "s", _sessions_payload(), origin="weekly_review")
    proposals.approve(db, p.id)
    with pytest.raises(proposals.ProposalConflict):
        proposals.reject(db, p.id)
    assert proposals.get(db, p.id)["status"] == "approved"
