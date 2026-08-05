"""Calendar connect + management routes.

- /auth/calendar/connect  → start the one-time offline consent (authed)
- /auth/calendar/callback → capture + store the refresh token
- /api/calendar/status    → connection + calendar state
- /api/calendar/sync      → explicit push of the current plan to the calendar
- /api/calendar/disconnect

The manual /api/calendar/sync is the explicit-approval path for pushing an
already-approved plan (the button click is the authorization). Proposal approval
covers the in-flow case. Nothing else writes to the calendar.
"""

import logging
import secrets

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..auth import current_user
from ..context.store import get_or_create_state
from ..db import get_db
from ..models import Session as PlanSession
from . import oauth
from . import sync as cal_sync

logger = logging.getLogger(__name__)

auth_router = APIRouter(prefix="/auth/calendar", tags=["calendar"])
api_router = APIRouter(prefix="/api/calendar", tags=["calendar"])


@auth_router.get("/connect")
def connect(request: Request, user: str = Depends(current_user)):
    if not oauth.get_settings().google_client_id:
        raise HTTPException(status_code=503, detail="Google OAuth is not configured")
    state = secrets.token_urlsafe(32)
    request.session["cal_oauth_state"] = state
    return RedirectResponse(oauth.build_consent_url(state))


@auth_router.get("/callback")
async def callback(request: Request, code: str | None = None, state: str | None = None,
                   error: str | None = None, db: Session = Depends(get_db)):
    # This route runs in the user's browser right after they consent, so it's the
    # signed-in user; still verify state to prevent CSRF.
    expected = request.session.pop("cal_oauth_state", None)
    if error or not code:
        return HTMLResponse(f"<h1>Calendar not connected</h1><p>{error or 'no code'}</p>", status_code=400)
    if not expected or state != expected:
        raise HTTPException(status_code=400, detail="OAuth state mismatch")

    tokens = await oauth.exchange_code(code)
    refresh = tokens.get("refresh_token")
    if not refresh:
        # Google only returns a refresh token on first consent; force re-consent.
        return HTMLResponse(
            "<h1>No refresh token returned</h1><p>Revoke J2H4All's access in your Google "
            "account and reconnect, or ensure the consent prompt appeared.</p>",
            status_code=400,
        )
    oauth.store_refresh_token(db, refresh, tokens.get("scope", oauth.SCOPE))
    # Create the dedicated calendar now so the connected state is complete.
    try:
        token = oauth.access_token(db)
        cal_sync.ensure_calendar(db, cal_sync.CalendarClient(token))
    except Exception:
        logger.exception("Calendar creation after connect failed (will retry on first sync)")
    return RedirectResponse("/?calendar=connected")


@api_router.get("/status")
def status(user: str = Depends(current_user), db: Session = Depends(get_db)):
    connected = oauth.is_connected(db)
    state = get_or_create_state(db)
    unsynced = db.scalar(
        select(PlanSession).where(
            PlanSession.status == "planned", PlanSession.type != "rest",
            PlanSession.calendar_event_id.is_(None),
        ).limit(1)
    )
    return {
        "connected": connected,
        "calendar_id": state.training_calendar_id,
        "has_unsynced_sessions": unsynced is not None,
        # Last successful push, rendered on their local clock, for each target.
        "last_calendar_push": _local_stamp(db, "last_push_calendar_at"),
        "last_garmin_push": _local_stamp(db, "last_push_garmin_at"),
    }


def _local_stamp(db: Session, key: str) -> str | None:
    from datetime import datetime

    from ..coach.schedule import fmt_local
    from ..context.store import get_meta
    raw = get_meta(db, key)
    if not raw:
        return None
    try:
        return fmt_local(db, datetime.fromisoformat(raw))
    except ValueError:
        return None


@api_router.post("/sync")
def sync(user: str = Depends(current_user), db: Session = Depends(get_db)):
    """Explicit push of the current plan — one button, two surfaces (the click is
    the approval): Google Calendar events + Garmin scheduled workouts (when the
    push flag is on). Each result is reported on its own line in the UI."""
    try:
        calendar = cal_sync.reconcile(db)
    except oauth.CalendarNotConnected as exc:
        raise HTTPException(status_code=409, detail=str(exc))

    from ..garmin import workouts as garmin_workouts
    try:
        garmin = garmin_workouts.reconcile(db)
    except Exception as exc:  # noqa: BLE001 — calendar result still stands
        logger.exception("Garmin workout push (manual sync) failed")
        garmin = {"error": str(exc)}
    return {"calendar": calendar, "garmin": garmin}


@api_router.post("/disconnect")
def disconnect(user: str = Depends(current_user), db: Session = Depends(get_db)):
    oauth.disconnect(db)
    return {"connected": False}
