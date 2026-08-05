import logging
import secrets
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from .config import get_settings

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["auth"])

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"


def current_user(request: Request) -> str:
    """FastAPI dependency: the signed-in allowlisted email, or 401."""
    email = request.session.get("user_email")
    if email:
        return email
    settings = get_settings()
    # Dev-only bypass so the authed path is testable without live Google creds.
    # validate_production() guarantees this can never be set in production.
    if settings.app_env == "development" and settings.dev_auth_bypass_email:
        logger.warning("AUTH BYPASS active (dev only): %s", settings.dev_auth_bypass_email)
        return settings.dev_auth_bypass_email
    raise HTTPException(status_code=401, detail="Not signed in")


def _redirect_uri() -> str:
    return f"{get_settings().app_base_url.rstrip('/')}/auth/callback"


@router.get("/login")
def login(request: Request):
    settings = get_settings()
    if not settings.google_client_id:
        raise HTTPException(status_code=503, detail="Google OAuth is not configured")
    state = secrets.token_urlsafe(32)
    request.session["oauth_state"] = state
    params = {
        "client_id": settings.google_client_id,
        "redirect_uri": _redirect_uri(),
        "response_type": "code",
        "scope": "openid email",
        "state": state,
        "prompt": "select_account",
    }
    return RedirectResponse(f"{GOOGLE_AUTH_URL}?{urlencode(params)}")


@router.get("/callback")
async def callback(request: Request, code: str | None = None, state: str | None = None,
                   error: str | None = None):
    settings = get_settings()
    expected_state = request.session.pop("oauth_state", None)
    if error or not code:
        raise HTTPException(status_code=400, detail=f"OAuth failed: {error or 'no code'}")
    if not expected_state or state != expected_state:
        raise HTTPException(status_code=400, detail="OAuth state mismatch")

    async with httpx.AsyncClient(timeout=15) as client:
        token_resp = await client.post(GOOGLE_TOKEN_URL, data={
            "client_id": settings.google_client_id,
            "client_secret": settings.google_client_secret,
            "code": code,
            "grant_type": "authorization_code",
            "redirect_uri": _redirect_uri(),
        })
        if token_resp.status_code != 200:
            logger.error("Google token exchange failed: %s", token_resp.text)
            raise HTTPException(status_code=502, detail="Google token exchange failed")
        access_token = token_resp.json().get("access_token")

        userinfo_resp = await client.get(
            GOOGLE_USERINFO_URL, headers={"Authorization": f"Bearer {access_token}"}
        )
        if userinfo_resp.status_code != 200:
            raise HTTPException(status_code=502, detail="Google userinfo failed")
        info = userinfo_resp.json()

    email = (info.get("email") or "").lower()
    verified = info.get("email_verified", False)

    # PRD §3 — the single-user hard gate. Exactly one email gets a session.
    if not verified or email != settings.allowed_google_email.lower():
        request.session.clear()
        logger.warning("Rejected sign-in attempt from %s", email or "<no email>")
        return HTMLResponse(
            "<h1>Not authorized</h1><p>J2H4All is a single-user app. This account has no access.</p>",
            status_code=403,
        )

    request.session["user_email"] = email
    return RedirectResponse("/")


@router.get("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/")
