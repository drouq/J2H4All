"""Preflight check: what is configured, what is missing, and what to do about it.

Run this FIRST on a new install, and again whenever something has gone quiet:

    python -m app.jobs doctor

Why this exists. Self-hosting means provisioning six independent services, and
almost every one of them fails *silently*: a cron with a missing environment
variable just doesn't do its job, an OAuth consent screen left in "Testing" works
for seven days and then stops, and a Garmin token that lapsed looks exactly like a
watch that hasn't been worn. Silence is this app's normal healthy state (see
`monitor.py`), which is exactly what makes a missing credential so hard to spot.

Design rules for this module:
- **Never fail.** A check that raises is itself a bug; every probe is wrapped.
- **Never write.** This is safe to run against production at any time.
- **No network calls.** It reports on CONFIGURATION, not on liveness, so it is
  fast, free, and works offline. `/healthz/db` and a real job run remain the way
  to test that something actually works end to end.
- **Say what to DO.** A check that reports "GOOGLE_REFRESH_TOKEN missing" without
  saying where to get one has moved the problem, not solved it.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

OK, WARN, FAIL = "ok", "warn", "fail"
_GLYPH = {OK: "[ ok ]", WARN: "[warn]", FAIL: "[FAIL]"}


class Report:
    """Collected results. `worst` drives the process exit code so this is usable
    as a deploy gate, not just something a human reads."""

    def __init__(self) -> None:
        self.rows: list[tuple[str, str, str, str]] = []  # status, area, message, fix

    def add(self, status: str, area: str, message: str, fix: str = "") -> None:
        self.rows.append((status, area, message, fix))

    @property
    def worst(self) -> str:
        for level in (FAIL, WARN, OK):
            if any(r[0] == level for r in self.rows):
                return level
        return OK

    def render(self) -> str:
        width = max((len(r[1]) for r in self.rows), default=0)
        lines = []
        for status, area, message, fix in self.rows:
            lines.append(f"{_GLYPH[status]} {area.ljust(width)}  {message}")
            if fix and status != OK:
                lines.append(f"{' ' * (len(_GLYPH[status]) + width + 3)}-> {fix}")
        counts = {level: sum(1 for r in self.rows if r[0] == level) for level in (OK, WARN, FAIL)}
        lines.append("")
        lines.append(f"{counts[OK]} ok, {counts[WARN]} warning(s), {counts[FAIL]} failure(s)")
        if counts[FAIL]:
            lines.append("Fix the failures above - the app will not work correctly until you do.")
        elif counts[WARN]:
            lines.append("No blockers. The warnings are things that will bite later, not now.")
        else:
            lines.append("Everything this check can see is configured.")
        return "\n".join(lines)


def _check_core(s, r: Report) -> None:
    r.add(OK if s.anthropic_api_key else FAIL, "Anthropic",
          "API key set" if s.anthropic_api_key else "ANTHROPIC_API_KEY is not set - the coach cannot think",
          "Create a key at console.anthropic.com and make sure the account has credit.")

    if not s.database_url:
        r.add(FAIL, "Database", "DATABASE_URL is not set",
              "Point it at your Postgres. A Neon free-tier database is enough.")
    elif s.is_production and not s.sqlalchemy_url.startswith("postgresql+psycopg://"):
        r.add(FAIL, "Database", "production requires Postgres, not SQLite",
              "Set DATABASE_URL to your Postgres connection string.")
    else:
        kind = "Postgres" if "postgresql" in s.sqlalchemy_url else "SQLite (fine for local dev)"
        r.add(OK, "Database", kind)

    if s.secret_key and s.secret_key != "dev-only-insecure-key":
        r.add(OK, "Session key", "set")
    else:
        r.add(FAIL if s.is_production else WARN, "Session key",
              "SECRET_KEY is unset or still the insecure dev default",
              'Generate one: python -c "import secrets; print(secrets.token_urlsafe(48))"')


def _check_gates(s, r: Report) -> None:
    """The two hard rules. Both are single config values, and both fail OPEN in the
    sense that a blank value is easy not to notice - hence explicit checks."""
    if s.allowed_google_email:
        r.add(OK, "Web gate", f"one allowlisted account ({s.allowed_google_email})")
    else:
        r.add(FAIL, "Web gate", "ALLOWED_GOOGLE_EMAIL is not set - nobody can sign in",
              "Set it to the single Google account that owns this install.")

    if s.telegram_bot_token and s.telegram_chat_id:
        r.add(OK, "Telegram gate", f"bot locked to chat {s.telegram_chat_id} (from the environment)")
    elif s.telegram_bot_token:
        # Not a failure since pairing shipped: unbound means the bot answers NOBODY,
        # which is the safe state. The database half of this is reported by the
        # Setup panel, which can see whether a chat is actually paired.
        r.add(WARN, "Telegram gate", "no TELEGRAM_CHAT_ID - the bot answers nobody unless it has been paired",
              "Pair it from the Setup panel (get a code, send it to your bot), or set "
              "TELEGRAM_CHAT_ID, which always wins.")
    else:
        r.add(WARN, "Telegram", "not configured - no briefs, debriefs or approval cards",
              "Create a bot with @BotFather and set TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID.")

    if s.telegram_bot_token and not s.telegram_webhook_secret:
        r.add(FAIL if s.is_production else WARN, "Telegram webhook",
              "TELEGRAM_WEBHOOK_SECRET is not set - forged updates are gated only by a guessable chat id",
              "Set any random string, and pass it as secret_token when you call setWebhook.")

    if s.dev_auth_bypass_email and s.is_production:
        r.add(FAIL, "Auth bypass", "DEV_AUTH_BYPASS_EMAIL is set in production",
              "Unset it. It skips the sign-in gate entirely.")


def _check_google(s, r: Report) -> None:
    if not (s.google_client_id and s.google_client_secret):
        r.add(WARN, "Google OAuth", "not configured - no web sign-in, calendar or backup",
              "Create an OAuth client in a Google Cloud project and set GOOGLE_CLIENT_ID/SECRET.")
        return
    r.add(OK, "Google OAuth", "client configured")
    if s.google_refresh_token:
        r.add(OK, "Google Calendar", "refresh token set")
    else:
        r.add(WARN, "Google Calendar", "GOOGLE_REFRESH_TOKEN not set - calendar/backup run only from the web service",
              'Use "Connect Google Calendar" in the app, then copy the token into the env '
              "so the cron jobs can use it too.")
    # The single most common silent failure on this stack: a consent screen left in
    # Testing issues refresh tokens that expire after 7 days, so everything works
    # for a week and then quietly stops. Config cannot detect it - so always say it.
    r.add(WARN, "Google consent", 'cannot be checked from here - verify it is set to "In production"',
          "In Testing mode your refresh token expires after 7 days and the calendar silently dies.")


def _check_garmin(s, r: Report) -> None:
    if not s.garmin_sync_enabled:
        r.add(WARN, "Garmin", "GARMIN_SYNC_ENABLED=false - no data is being pulled",
              "Set it true unless you are deliberately running without sync.")
        return
    if s.garth_token:
        r.add(OK, "Garmin", "bootstrap token present")
    else:
        r.add(FAIL, "Garmin", "GARTH_TOKEN is not set - no physiology data",
              "Run `python -m app.garmin.login` ON YOUR HOME MACHINE (Garmin blocks "
              "datacenter IPs), then paste the token here.")
    r.add(OK if s.garmin_workout_push_enabled else WARN, "Watch push",
          "enabled" if s.garmin_workout_push_enabled else "GARMIN_WORKOUT_PUSH_ENABLED=false - sessions won't reach the watch",
          "Set it true once you're happy for the app to write to your Garmin account.")


def _check_models(s, r: Report) -> None:
    from .config import DEFAULT_MODELS

    try:
        missing = [t for t in DEFAULT_MODELS if not s.model_for(t)]
        if missing:
            r.add(FAIL, "Models", f"no model resolved for: {', '.join(missing)}",
                  "Check MODEL_MAP_JSON - it merges over the code defaults.")
        else:
            tiers = sorted({s.model_for(t) for t in DEFAULT_MODELS})
            r.add(OK, "Models", f"{len(DEFAULT_MODELS)} tasks -> {', '.join(tiers)}")
    except Exception as exc:  # noqa: BLE001 - malformed MODEL_MAP_JSON must not crash the check
        r.add(FAIL, "Models", f"model map is broken: {exc}",
              "MODEL_MAP_JSON must be a JSON object, e.g. {\"morning_brief\": \"claude-sonnet-5\"}")


def _check_athlete(r: Report) -> None:
    """Configuration can be perfect while the coach still has no idea who it is
    coaching. That is invisible from the environment, so look in the database."""
    try:
        from .context.store import profile_view
        from .db import SessionLocal
        from .models import Goal
        from sqlalchemy import select

        db = SessionLocal()
        try:
            profile = profile_view(db)
            if profile["configured"]:
                r.add(OK, "Athlete", f"{profile['name'] or 'profile set'} ({profile['pronouns']})")
            else:
                r.add(WARN, "Athlete", "no profile yet - the coach doesn't know who you are",
                      "Tell it in chat: your name, pronouns, age, and anything that makes "
                      "your data read wrong (restless legs, night shifts, medication).")

            goal = db.scalar(select(Goal).where(Goal.status == "active").limit(1))
            if goal is None:
                r.add(WARN, "Goal", "no active goal", "Set your race in chat or the web app.")
            elif (goal.floor_note or "").startswith("PLACEHOLDER"):
                r.add(WARN, "Goal", f"still the placeholder goal ({goal.race_date})",
                      "Replace it with your real race - the whole plan is built backwards from it.")
            else:
                r.add(OK, "Goal", f"{goal.format} on {goal.race_date}")
        finally:
            db.close()
    except Exception as exc:  # noqa: BLE001 - an unmigrated DB must not break the report
        r.add(WARN, "Athlete", f"could not read the database ({type(exc).__name__})",
              "Run `alembic upgrade head`, and check DATABASE_URL.")


def run() -> Report:
    from .config import get_settings

    s = get_settings()
    r = Report()
    r.add(OK, "Environment", f"APP_ENV={s.app_env}, base URL {s.app_base_url}")
    for check in (_check_core, _check_gates, _check_google, _check_garmin, _check_models):
        try:
            check(s, r)
        except Exception as exc:  # noqa: BLE001 - one broken probe must not hide the rest
            r.add(WARN, check.__name__, f"check itself failed: {exc}")
    _check_athlete(r)
    return r
