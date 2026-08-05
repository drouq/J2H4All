"""Datacenter-friendly OAuth2 refresh (diauth.garmin.com), with rotating refresh tokens.

garth refreshes the OAuth2 access token by re-running the OAuth1->2 exchange on
connectapi.garmin.com, which Garmin's Cloudflare 429s from datacenter IPs (Render, CI).
The OAuth2 *refresh grant* lives on a different host, diauth.garmin.com, which is NOT
blocked from datacenters — and it returns a fresh ~23h access token plus a NEW ~30d
refresh token each call. Adopting + persisting that new refresh token keeps it rolling
indefinitely, so the blocked exchange is only ever needed to bootstrap (once, from a
residential IP, at the ~yearly interactive login). Verified from Render/GitHub datacenter
IPs on 2026-07-08 (see docs/garmin-connectivity-report.md).
"""

import logging

import garth.sso as _sso
from curl_cffi import requests as _creq
from garth.auth_tokens import OAuth2Token

from .impersonate import IMPERSONATE

logger = logging.getLogger(__name__)

TOKEN_URL = "https://diauth.garmin.com/di-oauth2-service/oauth/token"
CLIENT_ID = "GARMIN_CONNECT_MOBILE_ANDROID_DI"  # public mobile client (from the token's JWT)

_FIELDS = (
    "scope", "jti", "token_type", "access_token", "refresh_token",
    "expires_in", "expires_at", "refresh_token_expires_in", "refresh_token_expires_at",
)


def refresh(current: OAuth2Token) -> OAuth2Token:
    """Exchange the current refresh token for a fresh access + refresh token via diauth.
    Raises on any non-200 (e.g. an expired refresh token → needs residential re-bootstrap)."""
    r = _creq.post(
        TOKEN_URL,
        data={"grant_type": "refresh_token", "refresh_token": current.refresh_token, "client_id": CLIENT_ID},
        headers={**_sso.USER_AGENT, "Content-Type": "application/x-www-form-urlencoded"},
        impersonate=IMPERSONATE, timeout=30,
    )
    r.raise_for_status()
    d = _sso.set_expirations(r.json())
    return OAuth2Token(
        scope=d.get("scope", current.scope),
        jti=d.get("jti", current.jti),
        token_type=d.get("token_type", current.token_type),
        access_token=d["access_token"],
        refresh_token=d["refresh_token"],
        expires_in=d["expires_in"], expires_at=d["expires_at"],
        refresh_token_expires_in=d["refresh_token_expires_in"],
        refresh_token_expires_at=d["refresh_token_expires_at"],
    )


def to_dict(t: OAuth2Token) -> dict:
    return {f: getattr(t, f) for f in _FIELDS}


def from_dict(d: dict) -> OAuth2Token:
    return OAuth2Token(**{f: d[f] for f in _FIELDS})
