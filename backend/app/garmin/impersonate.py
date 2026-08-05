"""Make garth's Garmin traffic pass Cloudflare from a datacenter IP (Render).

Garmin's Cloudflare 429s the OAuth token *refresh* from cloud IPs because garth's
default HTTP stack (requests/urllib3) presents a non-browser TLS fingerprint. Two moves:

1. Route garth's data calls through a curl_cffi session that impersonates Chrome's TLS
   fingerprint (garth.Client accepts an injected session).
2. Reimplement the one refresh call — `sso.exchange` (OAuth1 → OAuth2) — over curl_cffi
   with manual OAuth1 signing. garth normally does this via a requests-based
   `OAuth1Session` that bypasses the injected session, so (1) alone wouldn't cover it.

At runtime we load *stored* tokens (no interactive login), so `exchange` is the only
auth call that recurs — impersonating it is what unblocks unattended syncs. Local
(residential IP) never hit the block; this is purely to survive Render's IP.
"""

import logging
from urllib.parse import urlencode

import garth.sso as _sso
from curl_cffi import requests as _creq
from garth.auth_tokens import OAuth2Token
from oauthlib.oauth1 import Client as _OAuth1Client

logger = logging.getLogger(__name__)

IMPERSONATE = "chrome"


class ImpersonatedSession(_creq.Session):
    """curl_cffi session (Chrome TLS fingerprint) with the two requests.Session bits
    garth pokes at — a no-op `mount` and a `hooks` dict — so it drops into garth.Client."""

    def __init__(self, *args, **kwargs):
        kwargs.setdefault("impersonate", IMPERSONATE)
        super().__init__(*args, **kwargs)
        self.hooks = {"response": []}  # garth's telemetry.attach appends here (never fires)

    def mount(self, *args, **kwargs):  # garth mounts a requests Retry adapter — no-op here
        return None


def _consumer() -> dict:
    """Garmin's public OAuth consumer key/secret (hosted on S3). Fetched via curl_cffi
    too, so a datacenter-IP filter there wouldn't block us either."""
    if _sso.OAUTH_CONSUMER:
        return _sso.OAUTH_CONSUMER
    r = _creq.get(_sso.OAUTH_CONSUMER_URL, impersonate=IMPERSONATE, timeout=20)
    r.raise_for_status()
    _sso.OAUTH_CONSUMER = r.json()
    return _sso.OAUTH_CONSUMER


def _exchange(oauth1, client) -> OAuth2Token:
    """Drop-in for garth.sso.exchange: sign the OAuth1 request by hand and POST it via
    curl_cffi (Chrome TLS), instead of garth's requests-based OAuth1Session."""
    consumer = _consumer()
    url = f"https://connectapi.{client.domain}/oauth-service/oauth/exchange/user/2.0"
    body = urlencode({"mfa_token": oauth1.mfa_token} if oauth1.mfa_token else {})
    headers = {**_sso.USER_AGENT, "Content-Type": "application/x-www-form-urlencoded"}
    signed_url, signed_headers, signed_body = _OAuth1Client(
        consumer["consumer_key"],
        consumer["consumer_secret"],
        resource_owner_key=oauth1.oauth_token,
        resource_owner_secret=oauth1.oauth_token_secret,
    ).sign(url, http_method="POST", body=body, headers=headers)
    resp = _creq.post(
        signed_url, headers=signed_headers, data=signed_body,
        impersonate=IMPERSONATE, timeout=client.timeout,
    )
    resp.raise_for_status()
    return OAuth2Token(**_sso.set_expirations(resp.json()))


_installed = False


def install() -> None:
    """Idempotently route garth's token exchange through the curl_cffi implementation."""
    global _installed
    if _installed:
        return
    _sso.exchange = _exchange
    _installed = True
    logger.info("Garmin impersonation installed (curl_cffi '%s' token exchange)", IMPERSONATE)
