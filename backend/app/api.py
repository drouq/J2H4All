import threading

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .auth import current_user
from .config import get_settings
from .context import extract as ctx_extract
from .context import pdf as ctx_pdf
from .context import store as ctx_store
from .db import get_db
from .garmin import sync as garmin_sync
from .llm import LLMNotConfigured
from .models import Heartbeat
from .plan import generate as plan_generate
from .plan import proposals as plan_proposals
from .plan import store as plan_store

router = APIRouter(prefix="/api", tags=["api"])


@router.get("/me")
def me(user: str = Depends(current_user)):
    return {"email": user}


@router.post("/heartbeat")
def heartbeat(user: str = Depends(current_user), db: Session = Depends(get_db)):
    """Phase 0 end-to-end proof: write one row, read the aggregate back."""
    row = Heartbeat(note=f"ping by {user}")
    db.add(row)
    db.commit()
    count = db.scalar(select(func.count(Heartbeat.id)))
    latest = db.scalar(
        select(Heartbeat.created_at).order_by(Heartbeat.id.desc()).limit(1)
    )
    return {"count": count, "latest_at": latest.isoformat() if latest else None}


@router.post("/sync")
def start_sync(mode: str = "incremental", user: str = Depends(current_user)):
    """On-demand 'Sync now' (PRD §7). Runs in a background thread; poll /api/sync/status."""
    if mode not in ("incremental", "full"):
        raise HTTPException(status_code=422, detail="mode must be 'incremental' or 'full'")
    if not get_settings().garmin_sync_enabled:
        # Sync is switched off. Report honestly instead of starting a background
        # thread that would do nothing and look like it worked.
        return {"started": False, "home_only": True}
    if garmin_sync._sync_lock.locked():
        raise HTTPException(status_code=409, detail="A sync is already running")
    threading.Thread(target=garmin_sync.run_sync, args=(mode,), daemon=True).start()
    return {"started": True, "mode": mode}


@router.get("/sync/status")
def sync_status(user: str = Depends(current_user), db: Session = Depends(get_db)):
    return garmin_sync.sync_status_summary(db)


# ---------------------------------------------------------------- context (Phase 2)

class ExtractRequest(BaseModel):
    text: str


class ConfirmRequest(BaseModel):
    items: list[dict]


@router.get("/context")
def get_context(user: str = Depends(current_user), db: Session = Depends(get_db)):
    return ctx_store.snapshot(db)


@router.post("/context/extract")
def extract_context(
    body: ExtractRequest, user: str = Depends(current_user), db: Session = Depends(get_db)
):
    """Eponge step 1 (PRD §19): propose typed items from free text. Writes nothing."""
    if not body.text.strip():
        return {"items": []}
    state = ctx_store.get_or_create_state(db)
    from .coach.schedule import local_today
    try:
        # Anchor relative dates ("tomorrow") on HIS local day (PRD §16), like the
        # Telegram arm — a UTC anchor mis-dates windows/injuries in their 00:00-08:00 window.
        items = ctx_extract.extract_items(body.text, local_today(db), state.timezone)
    except LLMNotConfigured as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    return {"items": items}


@router.post("/context/confirm")
def confirm_context(
    body: ConfirmRequest, user: str = Depends(current_user), db: Session = Depends(get_db)
):
    """Eponge step 2 (PRD §19): write the user-confirmed items."""
    applied = ctx_store.apply_items(db, body.items, source="chat")
    return {"applied": applied, "context": ctx_store.snapshot(db)}


@router.post("/context/bloods/parse")
async def parse_bloods(
    file: UploadFile = File(...), user: str = Depends(current_user), db: Session = Depends(get_db)
):
    """PDF blood report → proposed marker items for confirm (PRD §19)."""
    if (file.content_type or "") not in ("application/pdf", "application/octet-stream"):
        raise HTTPException(status_code=415, detail="Expected a PDF")
    data = await file.read()
    try:
        parsed = ctx_pdf.parse_blood_pdf(data)
    except LLMNotConfigured as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    return {"items": ctx_pdf.markers_to_items(parsed)}


# ---------------------------------------------------------------- goal & plan (Phase 3)

class ResolveRequest(BaseModel):
    payload: dict | None = None


class ReviseRequest(BaseModel):
    instruction: str


@router.get("/goal")
def get_goal(user: str = Depends(current_user), db: Session = Depends(get_db)):
    return plan_store.goal_view(db)


@router.get("/plan")
def get_plan(user: str = Depends(current_user), db: Session = Depends(get_db)):
    return plan_store.plan_view(db)


@router.post("/plan/draft")
def draft_plan(user: str = Depends(current_user), db: Session = Depends(get_db)):
    """Draft-first onboarding (PRD §14): Opus generates a macro plan + first 30-day
    block, saved as a PENDING proposal. Nothing is written until approval (§11)."""
    from .coach.schedule import local_today

    plan_store.ensure_seed(db)
    try:
        payload = plan_generate.generate_onboarding_draft(db, local_today(db))
    except LLMNotConfigured as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    except RuntimeError as exc:
        # Malformed model output twice in a row (generate.py's guard) — no
        # proposal was created; the athlete just retries.
        raise HTTPException(status_code=502, detail=f"{exc}. Please try drafting again.")
    n_phases = len(payload.get("macro_plan", {}).get("phases", []))
    n_sessions = len(payload.get("sessions", []))
    summary = f"Draft plan: {n_phases} phases to race day + {n_sessions} sessions for the next 30 days."
    proposal = plan_proposals.create(db, "onboarding_draft", summary, payload)
    return plan_proposals.get(db, proposal.id)


@router.get("/proposals")
def list_proposals(user: str = Depends(current_user), db: Session = Depends(get_db)):
    return {"pending": plan_proposals.list_pending(db)}


@router.post("/proposals/{proposal_id}/approve")
def approve_proposal(
    proposal_id: int, body: ResolveRequest, user: str = Depends(current_user),
    db: Session = Depends(get_db),
):
    try:
        result = plan_proposals.approve(db, proposal_id, body.payload)
    except KeyError:
        raise HTTPException(status_code=404, detail="No such proposal")
    except plan_proposals.ProposalConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return {**result, "plan": plan_store.plan_view(db)}


@router.post("/proposals/{proposal_id}/reject")
def reject_proposal(
    proposal_id: int, user: str = Depends(current_user), db: Session = Depends(get_db)
):
    try:
        plan_proposals.reject(db, proposal_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="No such proposal")
    except plan_proposals.ProposalConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return {"status": "rejected"}


@router.post("/proposals/{proposal_id}/revise")
def revise_proposal(
    proposal_id: int, body: ReviseRequest, user: str = Depends(current_user),
    db: Session = Depends(get_db),
):
    """Correct a proposal in conversation (PRD §11/§14): redraft it per a free-text
    instruction and return the fresh proposal. The old one is superseded."""
    from .coach import revise as coach_revise

    instruction = (body.instruction or "").strip()
    if not instruction:
        raise HTTPException(status_code=400, detail="Empty instruction")
    try:
        result = coach_revise.revise_proposal(db, proposal_id, instruction)
    except LLMNotConfigured as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    if result is None:
        raise HTTPException(
            status_code=409,
            detail="Couldn't revise this proposal (already resolved, or not a session change).",
        )
    new_proposal, _summary = result
    return plan_proposals.get(db, new_proposal.id)


# ---------------------------------------------------------------- coaching chat (Phase 6)

@router.get("/trends")
def trends(user: str = Depends(current_user), db: Session = Depends(get_db)):
    from . import trends as trends_mod
    return trends_mod.build(db)


# ---------------------------------------------------------------- Drive backup (Phase 6, §15)

@router.get("/backup/status")
def backup_status(user: str = Depends(current_user), db: Session = Depends(get_db)):
    from . import backup
    return backup.status(db)


@router.post("/backup/run")
def backup_run(user: str = Depends(current_user), db: Session = Depends(get_db)):
    from . import backup
    from .calendar.oauth import CalendarNotConnected
    try:
        return backup.run_export(db)
    except CalendarNotConnected as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except backup.DriveNotAuthorized as exc:
        raise HTTPException(status_code=409, detail=str(exc))


# NB: the web coaching-chat endpoints (/coach/ask, /coach/history) were removed on
# 2026-08-03. Coaching conversation is a Telegram affordance (`chat.ask_with_proposal`,
# which can also raise a plan-change card); the web panel was dropped long before, so
# these routes had no caller and just left an Opus-spending surface reachable by URL.
# Re-adding a web chat = one route + a panel; the model layer is unchanged.
