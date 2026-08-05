"""Diagnostic: capture the RAW Cloudflare response on the Garmin OAuth exchange.

Classifies a datacenter 429/403 as either:
  * a JS CHALLENGE (Turnstile / managed challenge) — an HTML interstitial that a headless
    browser could in principle solve to obtain a cf_clearance cookie; or
  * a BARE block (IP-reputation / rate-limit) — no challenge in the response, so no
    browser can help.

It performs the exact failing request (the OAuth1→2 exchange) and dumps status, all
response headers, and the body head instead of raising, then flags challenge markers.
No DB, no writes.

    GARTH_TOKEN=... python -m app.garmin.diag
"""

import base64
import json
import logging
import os
from urllib.parse import urlencode

import garth.sso as _sso
from curl_cffi import requests as _creq
from oauthlib.oauth1 import Client as _OAuth1Client

from .impersonate import IMPERSONATE, _consumer

logging.basicConfig(level=logging.INFO)

# Signatures Cloudflare emits when it serves a solvable JS challenge (in headers or body).
CHALLENGE_MARKERS = [
    "cf-mitigated", "challenge-platform", "cf_chl_opt", "turnstile",
    "/cdn-cgi/challenge-platform", "jschl", "__cf_chl", "cf-chl", "checking your browser",
]


def main() -> int:
    token = os.environ.get("GARTH_TOKEN", "")
    if not token:
        print("DIAG: GARTH_TOKEN not set")
        return 2

    oauth1 = json.loads(base64.b64decode(token))[0]  # [oauth1, oauth2]; we need oauth1
    consumer = _consumer()
    domain = oauth1.get("domain") or "garmin.com"
    url = f"https://connectapi.{domain}/oauth-service/oauth/exchange/user/2.0"
    body = urlencode({"mfa_token": oauth1["mfa_token"]} if oauth1.get("mfa_token") else {})
    headers = {**_sso.USER_AGENT, "Content-Type": "application/x-www-form-urlencoded"}
    signed_url, signed_headers, signed_body = _OAuth1Client(
        consumer["consumer_key"], consumer["consumer_secret"],
        resource_owner_key=oauth1["oauth_token"],
        resource_owner_secret=oauth1["oauth_token_secret"],
    ).sign(url, http_method="POST", body=body, headers=headers)

    try:
        resp = _creq.post(
            signed_url, headers=signed_headers, data=signed_body,
            impersonate=IMPERSONATE, timeout=30,
        )
    except Exception as exc:
        print(f"DIAG: request raised (no HTTP response): {type(exc).__name__}: {exc}")
        return 1

    hdrs = dict(resp.headers)
    text = resp.text or ""
    print(f"DIAG status: {resp.status_code}")
    print("DIAG headers:")
    for k, v in hdrs.items():
        print(f"  {k}: {v}")
    print(f"DIAG body length: {len(text)}")
    print("DIAG body head:")
    print(text[:2000])

    blob = (text + " " + " ".join(f"{k}:{v}" for k, v in hdrs.items())).lower()
    found = [m for m in CHALLENGE_MARKERS if m in blob]
    print(f"DIAG challenge markers found: {found or 'NONE'}")

    if resp.status_code == 200:
        print("DIAG VERDICT: exchange SUCCEEDED — this IP is not blocked.")
    elif found:
        print("DIAG VERDICT: JS CHALLENGE present — a headless browser MIGHT solve it (cf_clearance).")
    else:
        print("DIAG VERDICT: BARE block (no challenge markers) — a headless browser will NOT help.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
