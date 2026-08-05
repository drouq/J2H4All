import json
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict

# Tiered by task, overridable via MODEL_MAP_JSON without a code change.
# (race-strategy reasoning lives in coach_chat; a dedicated race_strategy task
# gets added here the day a race-plan surface is built, likely near race week.)
#
# The Opus tier moved 4.8 → 5 on 2026-08-03 after an A/B on live prod state (see the
# model-tiering note in ARCHITECTURE.md). Changed
# HERE rather than via MODEL_MAP_JSON on Render deliberately: this repo's recurring
# ops failure is per-service env drift — `j2h4all-export` sat broken for a month over a
# `sync:false` var nobody set on that service — and a model override would have needed
# hand-setting on `j2h4all` AND `j2h4all-tick` (the weekly review runs on the TICK), with a
# missed service silently leaving that surface a generation behind. A code default
# applies everywhere at once and is reviewable. MODEL_MAP_JSON remains the escape
# hatch for experiments and for rolling back without a deploy.
DEFAULT_MODELS: dict[str, str] = {
    # Opus tier: heavy, infrequent, high-stakes reasoning
    "macro_plan": "claude-opus-5",
    "weekly_review": "claude-opus-5",
    # Sonnet tier: frequent, lighter work
    "morning_brief": "claude-sonnet-5",
    "checkin_parse": "claude-sonnet-5",
    "context_extraction": "claude-sonnet-5",
    "post_run_read": "claude-sonnet-5",
    "pdf_blood_parse": "claude-sonnet-5",
    # Red-flag = quick between-review adjustment (Sonnet); full re-periodization stays Opus.
    "red_flag": "claude-sonnet-5",
    # Coaching conversation is the depth surface (fueling/strategy/taper) — Opus.
    "coach_chat": "claude-opus-5",
}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    app_env: str = "development"
    app_base_url: str = "http://localhost:8000"
    secret_key: str = "dev-only-insecure-key"

    database_url: str = "sqlite:///./j2h4all_dev.sqlite3"

    allowed_google_email: str = ""
    google_client_id: str = ""
    google_client_secret: str = ""
    google_refresh_token: str = ""

    anthropic_api_key: str = ""
    garth_token: str = ""

    # Kill-switch for all outbound Garmin syncing. Normally true everywhere: Render
    # syncs natively via the diauth OAuth2 refresh (garmin/oauth2.py). Set false to
    # make every sync surface a cheap no-op — the residential-fallback posture
    # (scripts/home_sync.ps1) used during the 2026-07 Cloudflare-429 investigation.
    garmin_sync_enabled: bool = True

    # Workout push (docs/garmin-workout-push.md): approved sessions become
    # scheduled Garmin structured workouts. **LIVE in prod since 2026-07** — the
    # single-session verification passed and the dashboard sets this true on the web
    # service. The default stays False so a fresh/dev environment can't write to their
    # real Garmin account by accident; don't read it as "not shipped yet".
    garmin_workout_push_enabled: bool = False

    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    telegram_webhook_secret: str = ""

    model_map_json: str = ""

    # Proactive-cadence hours, on the user's LOCAL clock. Exact hours
    # configurable without a code change. Weekly review is Sunday evening.
    # The brief runs at 10:00 local: pick an hour AFTER your daily sync cron lands
    # so it briefs on last night's sleep/HRV, not yesterday's. A non-zero minute
    # needs the tick cron denser than hourly (see render*.yaml j2h4all-tick).
    morning_brief_hour: int = 10
    morning_brief_minute: int = 0
    # End-of-day debrief (22:00): one prompt merging the subjective check-in (feel)
    # and the lifestyle log (life factors Garmin can't see) — late enough to capture
    # the evening (alcohol/sleep prep), and an hour before the Sunday weekly review
    # so the two don't collide.
    daily_debrief_hour: int = 22
    # 23:00 Sunday: late enough that the weekend long runs (and their sync) are in,
    # so the review reads the completed weekend before planning the week; sits after
    # the 22:00 debrief.
    weekly_review_hour: int = 23  # Sunday

    # Honored ONLY when app_env == "development"; see auth.current_user.
    dev_auth_bypass_email: str = ""

    @property
    def is_production(self) -> bool:
        return self.app_env == "production"

    @property
    def sqlalchemy_url(self) -> str:
        url = self.database_url
        # Render hands out postgres:// URLs; SQLAlchemy needs the psycopg3 driver name.
        if url.startswith("postgres://"):
            url = "postgresql+psycopg://" + url.removeprefix("postgres://")
        elif url.startswith("postgresql://"):
            url = "postgresql+psycopg://" + url.removeprefix("postgresql://")
        return url

    def model_for(self, task: str) -> str:
        overrides = json.loads(self.model_map_json) if self.model_map_json else {}
        models = {**DEFAULT_MODELS, **overrides}
        return models[task]

    def validate_production(self) -> None:
        """Refuse to boot in production with a broken hard gate or dev leftovers."""
        problems = []
        if not self.allowed_google_email:
            problems.append("ALLOWED_GOOGLE_EMAIL is not set (single-user gate)")
        if self.secret_key == "dev-only-insecure-key" or not self.secret_key:
            problems.append("SECRET_KEY is not set")
        if not self.sqlalchemy_url.startswith("postgresql+psycopg://"):
            problems.append("DATABASE_URL must be Postgres in production")
        if self.dev_auth_bypass_email:
            problems.append("DEV_AUTH_BYPASS_EMAIL must not be set in production")
        # TELEGRAM_CHAT_ID is deliberately NOT required. Since pairing shipped, an
        # unbound bot answers NOBODY (telegram_link.bound_chat_id -> None ->
        # _locked False for every sender), so booting unbound is the safe state
        # rather than an open door. Requiring it would make pairing impossible in
        # production, which is the only place it matters.
        if not self.telegram_webhook_secret:
            # Without it the webhook route skips its HTTP-layer check entirely,
            # leaving forged updates gated only by the guessable numeric chat id.
            problems.append("TELEGRAM_WEBHOOK_SECRET is not set (webhook gate)")
        if problems:
            raise RuntimeError("Refusing to start in production: " + "; ".join(problems))


@lru_cache
def get_settings() -> Settings:
    return Settings()
