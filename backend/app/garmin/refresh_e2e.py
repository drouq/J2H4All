"""End-to-end test: refresh via diauth (datacenter-friendly) THEN make real connectapi
data calls with the resulting access token — from whatever IP this runs on.

The OAuth exchange failed first in earlier tests, so we never learned whether connectapi
*data* endpoints are also IP-blocked. This closes that gap: if both the refresh AND the
data calls 200 from a datacenter, Render can run the full sync natively (no home, no proxy).

    GARTH_TOKEN=... python -m app.garmin.refresh_e2e
"""

import base64
import json
import logging
import os

import garth.sso as _sso
from curl_cffi import requests as _creq

from .impersonate import IMPERSONATE

logging.basicConfig(level=logging.INFO)

TOKEN_URL = "https://diauth.garmin.com/di-oauth2-service/oauth/token"
CLIENT_ID = "GARMIN_CONNECT_MOBILE_ANDROID_DI"
DATA_PATHS = [
    "/userprofile-service/userprofile/user-settings",
    "/activitylist-service/activities/search/activities?limit=1&start=0",
    "/wellness-service/wellness/dailyStress/2026-07-07",
]


def main() -> int:
    token = os.environ.get("GARTH_TOKEN", "")
    if not token:
        print("E2E: GARTH_TOKEN not set")
        return 2
    _, oauth2 = json.loads(base64.b64decode(token))
    ua = {**_sso.USER_AGENT}

    # 1) Refresh (diauth) — the datacenter-friendly grant.
    r = _creq.post(
        TOKEN_URL,
        data={"grant_type": "refresh_token", "refresh_token": oauth2["refresh_token"], "client_id": CLIENT_ID},
        headers={**ua, "Content-Type": "application/x-www-form-urlencoded"},
        impersonate=IMPERSONATE, timeout=30)
    print(f"[refresh @ diauth] status={r.status_code} cf-ray={r.headers.get('cf-ray')}")
    if r.status_code != 200 or not r.json().get("access_token"):
        print(f"  body: {(r.text or '')[:200]}")
        print("\nE2E VERDICT: refresh FAILED from this IP.")
        return 1
    access = r.json()["access_token"]

    # 2) Real data calls (connectapi) with the fresh Bearer token.
    ok = 0
    for path in DATA_PATHS:
        try:
            d = _creq.get(
                f"https://connectapi.garmin.com{path}",
                headers={**ua, "Authorization": f"Bearer {access}"},
                impersonate=IMPERSONATE, timeout=30)
            snippet = (d.text or "")[:80].replace("\n", " ")
            print(f"[data @ connectapi] {d.status_code} cf-ray={d.headers.get('cf-ray')} {path}")
            print(f"    body: {snippet}")
            if d.status_code == 200:
                ok += 1
        except Exception as exc:
            print(f"[data @ connectapi] EXC {type(exc).__name__}: {exc} {path}")

    print(f"\nE2E VERDICT: refresh OK + {ok}/{len(DATA_PATHS)} data calls OK from this IP.")
    return 0 if ok == len(DATA_PATHS) else 1


if __name__ == "__main__":
    raise SystemExit(main())
