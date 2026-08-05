"""Google Calendar OAuth: obtain and store a refresh token via a one-time
offline-access consent, then mint short-lived access tokens from it on demand.

Least-privilege scope: calendar.app.created — the app can create secondary
calendars and fully manage events on calendars it created, and nothing else. This
is exactly the isolation we want ("we only ever touch our own events").

Token resolution order: GOOGLE_REFRESH_TOKEN env (production) → the
oauth_credential row written by the connect flow (runtime-obtained fallback).
"""

import logging
from datetime import UTC, datetime
from urllib.parse import urlencode

import httpx
from sqlalchemy.orm import Session

from ..config import get_settings
from ..models import OAuthCredential

logger = logging.getLogger(__name__)

PROVIDER = "google_calendar"
# Least-privilege scopes: calendar.app.created (own our training calendar) +
# drive.file (create/manage only the backup files we create).
SCOPE = (
    "https://www.googleapis.com/auth/calendar.app.created "
    "https://www.googleapis.com/auth/drive.file"
)
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"


class CalendarNotConnected(RuntimeError):
    """No refresh token available — the calendar hasn't been connected yet."""


def redirect_uri() -> str:
    return f"{get_settings().app_base_url.rstrip('/')}/auth/calendar/callback"


def build_consent_url(state: str) -> str:
    settings = get_settings()
    params = {
        "client_id": settings.google_client_id,
        "redirect_uri": redirect_uri(),
        "response_type": "code",
        "scope": SCOPE,
        "state": state,
        "access_type": "offline",   # ask for a refresh token
        "prompt": "consent",         # force a refresh token even on re-consent
        "include_granted_scopes": "true",
    }
    return f"{AUTH_URL}?{urlencode(params)}"


async def exchange_code(code: str) -> dict:
    settings = get_settings()
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(TOKEN_URL, data={
            "client_id": settings.google_client_id,
            "client_secret": settings.google_client_secret,
            "code": code,
            "grant_type": "authorization_code",
            "redirect_uri": redirect_uri(),
        })
    if resp.status_code != 200:
        logger.error("Calendar token exchange failed: %s", resp.text)
        raise RuntimeError("Google token exchange failed")
    return resp.json()


def store_refresh_token(db: Session, refresh_token: str, scopes: str = SCOPE) -> None:
    cred = db.get(OAuthCredential, PROVIDER)
    now = datetime.now(UTC)
    if cred is None:
        db.add(OAuthCredential(provider=PROVIDER, refresh_token=refresh_token, scopes=scopes, updated_at=now))
    else:
        cred.refresh_token = refresh_token
        cred.scopes = scopes
        cred.updated_at = now
    db.commit()


def get_refresh_token(db: Session) -> str | None:
    env = get_settings().google_refresh_token
    if env:
        return env
    cred = db.get(OAuthCredential, PROVIDER)
    return cred.refresh_token if cred else None


def is_connected(db: Session) -> bool:
    return get_refresh_token(db) is not None


def granted_scopes(db: Session) -> str | None:
    """The scopes actually granted for the stored token. The env-provisioned
    production token is assumed to carry the full SCOPE; the runtime-connected
    token records exactly what Google returned (routes.py stores tokens['scope'])."""
    if get_settings().google_refresh_token:
        return SCOPE
    cred = db.get(OAuthCredential, PROVIDER)
    return cred.scopes if cred else None


def drive_authorized(db: Session) -> bool:
    """Whether the stored token can write Drive — a calendar-only token (granted
    before the drive.file scope was requested) is 'connected' but cannot back up."""
    return "drive.file" in (granted_scopes(db) or "")


def disconnect(db: Session) -> None:
    cred = db.get(OAuthCredential, PROVIDER)
    if cred is not None:
        db.delete(cred)
        db.commit()


def access_token(db: Session) -> str:
    """Mint a short-lived access token from the stored refresh token."""
    refresh = get_refresh_token(db)
    if not refresh:
        raise CalendarNotConnected("Google Calendar is not connected")
    settings = get_settings()
    resp = httpx.post(TOKEN_URL, data={
        "client_id": settings.google_client_id,
        "client_secret": settings.google_client_secret,
        "refresh_token": refresh,
        "grant_type": "refresh_token",
    }, timeout=15)
    if resp.status_code != 200:
        logger.error("Calendar token refresh failed: %s", resp.text)
        # An invalid_grant here means the refresh token was revoked or expired
        # (e.g. the OAuth consent screen is still in 'Testing' → 7-day expiry).
        raise CalendarNotConnected("Refresh token rejected — reconnect Google Calendar")
    return resp.json()["access_token"]
