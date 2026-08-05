"""Connectivity probe: can THIS host reach Garmin? (No DB, no writes.)

Exercises the exact path that fails on blocked datacenter IPs — GarminClient boot
(curl_cffi impersonation) → OAuth1→2 token exchange (stored access token is expired,
so the exchange always runs) → one authenticated data call. Prints redacted results
and exits 0 on success, 1 on failure.

    GARTH_TOKEN=... python -m app.garmin.probe

Retained as a residential diagnostic (the GitHub Actions probe workflow that once
used it was deleted 2026-07-08 after the diauth fix made Render sync natively —
see docs/garmin-connectivity-report.md). Run it from any host to test whether that
host's IP passes Garmin's Cloudflare on the OAuth1→2 exchange.
"""

import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def main() -> int:
    from .client import GarminAuthError, GarminClient

    try:
        client = GarminClient()
    except GarminAuthError as exc:
        print(f"PROBE FAIL (token load): {exc}")
        return 1
    try:
        # Forces the OAuth exchange (stored OAuth2 token is expired), then proves the
        # refreshed token works with a second, plain data call.
        name = client.display_name
        client.api("/userprofile-service/userprofile/user-settings")
        print(f"PROBE OK: exchange + data call succeeded (profile: {name[:4]}****)")
        return 0
    except Exception as exc:
        status = getattr(getattr(getattr(exc, "error", None), "response", None), "status_code", None)
        print(f"PROBE FAIL: status={status} {type(exc).__name__}: {str(exc)[:300]}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
