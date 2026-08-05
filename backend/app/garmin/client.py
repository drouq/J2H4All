import json
import logging
import time
from datetime import datetime, timezone

import garth

from ..config import get_settings

logger = logging.getLogger(__name__)

# Between-call pause: undocumented API, single user — be a polite citizen.
THROTTLE_S = 0.15
MAX_RETRIES = 3


class GarminAuthError(RuntimeError):
    """GARTH_TOKEN missing or no longer valid — user must re-run app.garmin.login."""


class GarminClient:
    """Thin wrapper over garth: token load, throttle, retry. No parsing here."""

    _TOKEN_KEY = "garmin_oauth2_token"  # rotating OAuth2 token, persisted in Preference
    _BOOTSTRAP_KEY = "garmin_bootstrap_token"  # login blob, pasted via the setup panel

    def __init__(self, db=None) -> None:
        settings = get_settings()
        # The login blob can arrive two ways. The environment variable wins, so an
        # operator-set value is never silently overridden by something pasted into
        # the web app. The database fallback exists because Garmin blocks datacenter
        # IPs on login: a self-hoster has to run `app.garmin.login` at home, and
        # asking them to then edit a host environment variable is the single step
        # most likely to strand them. Pasting it into their own app is not.
        blob = settings.garth_token or self._load_bootstrap(db)
        if not blob:
            raise GarminAuthError(
                "No Garmin token. Run `python -m app.garmin.login` on your home "
                "machine (Garmin blocks datacenter IPs), then paste the token into "
                "the app or set GARTH_TOKEN."
            )
        from . import impersonate
        impersonate.install()  # curl_cffi Chrome-TLS exchange (residential-only fallback)
        self._client = garth.Client(session=impersonate.ImpersonatedSession())
        try:
            self._client.loads(blob)
        except Exception as exc:  # corrupted/expired token blob
            raise GarminAuthError(f"GARTH_TOKEN could not be loaded: {exc}") from exc
        self._db = db
        self._ensure_oauth2()
        self._display_name: str | None = None

    def _ensure_oauth2(self) -> None:
        """Get a valid OAuth2 access token via the diauth refresh grant, which works from
        datacenter IPs (unlike the OAuth1 exchange) and rolls the refresh token forward.

        Adopts a fresher persisted token first, so successive cron runs chain off the latest
        refresh token rather than the static env one (whose refresh token would otherwise
        expire in ~30 days). Best-effort: if diauth refresh fails (e.g. the refresh token
        finally expired), we leave garth to fall back to the OAuth1 exchange — which only
        works from a residential IP and is the intended re-bootstrap path."""
        from . import oauth2

        stored = self._load_token()
        if stored is not None:
            self._client.oauth2_token = stored
        tok = self._client.oauth2_token
        if isinstance(tok, oauth2.OAuth2Token) and not tok.expired:
            return  # current access token still valid — nothing to do
        try:
            new = oauth2.refresh(tok)
            self._client.oauth2_token = new
            self._save_token(new)
            logger.info("OAuth2 refreshed via diauth (rolling refresh token)")
        except Exception as exc:
            logger.warning("diauth refresh failed (%s); will fall back to OAuth1 exchange", exc)

    @classmethod
    def _load_bootstrap(cls, db) -> str | None:
        """The pasted login blob, if one was saved. Never raises: a broken read here
        must surface as the normal 'no token' error, not a 500."""
        if db is None:
            return None
        try:
            from sqlalchemy import select
            from ..models import Preference
            pref = db.scalar(select(Preference).where(Preference.key == cls._BOOTSTRAP_KEY))
            return pref.value if pref else None
        except Exception:  # noqa: BLE001
            return None

    def _load_token(self):
        if self._db is None:
            return None
        from sqlalchemy import select

        from ..models import Preference
        from . import oauth2
        pref = self._db.scalar(select(Preference).where(Preference.key == self._TOKEN_KEY))
        if not pref or not pref.value:
            self._token_row_stamp = None
            return None
        self._token_row_stamp = pref.updated_at  # for the optimistic save check below
        try:
            return oauth2.from_dict(json.loads(pref.value))
        except Exception:
            logger.warning("Stored %s unreadable; ignoring", self._TOKEN_KEY)
            return None

    def _save_token(self, tok) -> None:
        """Persist the rotated token — OPTIMISTICALLY. The refresh token is
        consume-on-use: if another process (web sync vs cron) rotated the row
        since we loaded it, overwriting would persist an already-consumed token
        and orphan the valid one. In that case keep theirs (our in-memory access
        token still works for this run; the next run adopts the stored one)."""
        if self._db is None:
            return
        from sqlalchemy import select

        from ..models import Preference
        from . import oauth2
        pref = self._db.scalar(select(Preference).where(Preference.key == self._TOKEN_KEY))
        loaded_stamp = getattr(self, "_token_row_stamp", None)
        if pref and loaded_stamp is not None and pref.updated_at != loaded_stamp:
            logger.warning(
                "%s row rotated by another process since load; keeping theirs", self._TOKEN_KEY
            )
            return
        val = json.dumps(oauth2.to_dict(tok))
        now = datetime.now(timezone.utc)
        if pref:
            pref.value, pref.updated_at = val, now
        else:
            self._db.add(Preference(key=self._TOKEN_KEY, value=val, updated_at=now))
        self._db.commit()
        self._token_row_stamp = now

    @property
    def display_name(self) -> str:
        """socialProfile displayName (UUID-ish) — used by the race-predictor path."""
        if self._display_name is None:
            profile = self._client.profile  # userprofile-service/socialProfile
            self._display_name = profile["displayName"]
        return self._display_name

    @property
    def username(self) -> str:
        """socialProfile userName — used by the sleep path (matches garth's own usage)."""
        return self._client.username

    def api_write(self, method: str, path: str, payload: dict | None = None):
        """POST/PUT/DELETE a connectapi path (workout push). Throttled, but NO retry —
        a blind retry of a create could duplicate the workout. Returns parsed JSON
        (or None on 204); raises on any HTTP error, including 404."""
        time.sleep(THROTTLE_S)
        return self._client.connectapi(path, method=method, json=payload)

    def api(self, path: str, **params):
        """GET a connectapi path with throttle + retry. Returns parsed JSON (or None on 204)."""
        last_exc: Exception | None = None
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                time.sleep(THROTTLE_S)
                return self._client.connectapi(path, params=params or None)
            except Exception as exc:
                last_exc = exc
                status = getattr(getattr(exc, "error", None), "response", None)
                status_code = getattr(status, "status_code", None)
                # 4xx other than 429 won't heal on retry
                if status_code and 400 <= status_code < 500 and status_code != 429:
                    raise
                wait = 2**attempt
                logger.warning(
                    "Garmin call %s failed (attempt %d/%d): %s — retrying in %ss",
                    path, attempt, MAX_RETRIES, exc, wait,
                )
                time.sleep(wait)
        raise last_exc  # type: ignore[misc]
