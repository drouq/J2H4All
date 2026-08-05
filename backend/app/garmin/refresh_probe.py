"""Probe: can we refresh the OAuth2 access token via the standard refresh_token grant,
instead of garth's OAuth1->2 exchange?

garth deliberately re-runs the OAuth1 exchange (http.py refresh_oauth2) rather than the
OAuth2 refresh grant. The exchange lives on connectapi.garmin.com, which Cloudflare
blocks from datacenter IPs. The refresh grant lives on a DIFFERENT host,
diauth.garmin.com/di-oauth2-service/oauth/token — potentially different Cloudflare rules.

This tries a few param variants of the refresh grant and dumps the raw response for each.
Run residentially first to find a working format; then from a datacenter (GitHub Actions)
to test whether diauth is reachable where connectapi is not. No DB, no writes.

    GARTH_TOKEN=... python -m app.garmin.refresh_probe
"""

import base64
import json
import logging
import os

from curl_cffi import requests as _creq

import garth.sso as _sso
from .impersonate import IMPERSONATE, _consumer

logging.basicConfig(level=logging.INFO)

TOKEN_URL = "https://diauth.garmin.com/di-oauth2-service/oauth/token"


def _b64json(seg: str) -> dict:
    seg += "=" * (-len(seg) % 4)
    return json.loads(base64.urlsafe_b64decode(seg))


def main() -> int:
    token = os.environ.get("GARTH_TOKEN", "")
    if not token:
        print("REFRESH: GARTH_TOKEN not set")
        return 2
    oauth1, oauth2 = json.loads(base64.b64decode(token))
    refresh_token = oauth2["refresh_token"]
    consumer = _consumer()
    ckey, csecret = consumer["consumer_key"], consumer["consumer_secret"]

    # Decode the (expired) access-token JWT for the real client_id.
    cid = "GARMIN_CONNECT_MOBILE_ANDROID_DI"
    try:
        payload = _b64json(oauth2["access_token"].split(".")[1])
        cid = payload.get("client_id") or cid
        print("JWT hints:", {k: payload.get(k) for k in
              ("iss", "client_id", "client_type", "garmin_guid") if k in payload})
    except Exception as exc:
        print(f"JWT decode skipped: {exc}")

    ua = {**_sso.USER_AGENT, "Content-Type": "application/x-www-form-urlencoded"}
    basic = base64.b64encode(f"{cid}:".encode()).decode()
    variants = [
        ("D: public client_id in body", ua,
         {"grant_type": "refresh_token", "refresh_token": refresh_token, "client_id": cid}),
        ("E: HTTP Basic (client_id:'')", {**ua, "Authorization": f"Basic {basic}"},
         {"grant_type": "refresh_token", "refresh_token": refresh_token}),
        ("F: client_id + consumer_secret", ua,
         {"grant_type": "refresh_token", "refresh_token": refresh_token,
          "client_id": cid, "client_secret": csecret}),
    ]

    for label, hdrs, data in variants:
        try:
            r = _creq.post(TOKEN_URL, data=data, headers=hdrs,
                           impersonate=IMPERSONATE, timeout=30)
            body = (r.text or "")[:300]
            has_access = '"access_token"' in (r.text or "")
            print(f"\n[{label}] status={r.status_code} cf-ray={r.headers.get('cf-ray')} "
                  f"got_access_token={has_access}")
            print(f"  body: {body}")
            if r.status_code == 200 and has_access:
                tok = r.json()
                rt_ttl = tok.get("refresh_token_expires_in") or 0
                print(f"\n  new tokens: access expires_in={tok.get('expires_in')}s, "
                      f"refresh expires_in={rt_ttl}s (~{rt_ttl // 86400}d)")
                # Chain test: immediately refresh AGAIN using the NEW refresh token. If this
                # 200s with a fresh ~equal TTL, rolling refresh sustains indefinitely.
                new_rt = tok.get("refresh_token")
                r2 = _creq.post(
                    TOKEN_URL,
                    data={"grant_type": "refresh_token", "refresh_token": new_rt, "client_id": cid},
                    headers=ua, impersonate=IMPERSONATE, timeout=30)
                tok2 = r2.json() if r2.status_code == 200 else {}
                chained = bool(tok2.get("access_token"))
                rt_ttl2 = tok2.get("refresh_token_expires_in") or 0
                print(f"  chain refresh with NEW refresh_token: status={r2.status_code} ok={chained} "
                      f"(new refresh TTL ~{rt_ttl2 // 86400}d)")
                print(f"\nREFRESH VERDICT: SUCCESS via [{label}]. "
                      f"Rolling-refresh {'CONFIRMED' if chained else 'NOT confirmed'}.")
                return 0
        except Exception as exc:
            print(f"\n[{label}] request raised: {type(exc).__name__}: {exc}")

    print("\nREFRESH VERDICT: no variant succeeded from this IP.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
