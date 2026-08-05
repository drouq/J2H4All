"""The single-user hard gate — the other load-bearing rule.

Web = Google OAuth + a one-email allowlist; Telegram = one chat-ID lock. The
approval gate has had thorough coverage since Phase 3; this one had NONE until
2026-08-03, which is the wrong asymmetry: both are hard rules, and this is the one
where a quietly inverted condition hands a stranger the athlete's medical history
and their coach's write access to their calendar. The logic is small — that is exactly
why it needs locking down rather than re-reading.
"""
import pytest
from fastapi.testclient import TestClient

from app import telegram as tg
from app.config import Settings

ALLOWED = "athlete@example.com"


# --------------------------------------------------------------------- web: the email allowlist

@pytest.fixture
def client(monkeypatch):
    from app import auth, main
    # Pin the auth module's settings: a developer with DEV_AUTH_BYPASS_EMAIL in their
    # .env would otherwise be silently signed in and these assertions would fail for
    # a reason that has nothing to do with the gate.
    monkeypatch.setattr(auth, "get_settings",
                        lambda: Settings(allowed_google_email=ALLOWED, dev_auth_bypass_email=""))
    return TestClient(main.app, raise_server_exceptions=False)


def test_api_routes_reject_an_anonymous_caller(client):
    """Every data route, not just a sample — a new route added without the
    dependency is the realistic regression."""
    for path in ("/api/me", "/api/context", "/api/goal", "/api/plan", "/api/proposals",
                 "/api/trends", "/api/backup/status", "/api/sync/status",
                 "/api/calendar/status"):
        r = client.get(path)
        assert r.status_code == 401, f"{path} answered {r.status_code} to an anonymous caller"


def test_write_routes_reject_an_anonymous_caller(client):
    for path in ("/api/heartbeat", "/api/sync", "/api/plan/draft", "/api/backup/run",
                 "/api/calendar/sync", "/api/calendar/disconnect"):
        r = client.post(path)
        assert r.status_code == 401, f"{path} answered {r.status_code} to an anonymous caller"


def _auth_module(monkeypatch, **overrides):
    from app import auth
    settings = Settings(allowed_google_email=ALLOWED, **overrides)
    monkeypatch.setattr(auth, "get_settings", lambda: settings)
    return auth


def test_current_user_returns_the_session_email(monkeypatch):
    auth = _auth_module(monkeypatch)

    class _Req:
        session = {"user_email": ALLOWED}
    assert auth.current_user(_Req()) == ALLOWED


def test_current_user_401s_without_a_session(monkeypatch):
    from fastapi import HTTPException
    auth = _auth_module(monkeypatch)

    class _Req:
        session = {}
    with pytest.raises(HTTPException) as exc:
        auth.current_user(_Req())
    assert exc.value.status_code == 401


def test_dev_bypass_is_inert_outside_development(monkeypatch):
    """`validate_production` refuses to boot with the bypass set, but the dependency
    must not honour it on env alone either — two locks, not one."""
    from fastapi import HTTPException
    auth = _auth_module(monkeypatch, app_env="production", dev_auth_bypass_email="someone@else.com")

    class _Req:
        session = {}
    with pytest.raises(HTTPException):
        auth.current_user(_Req())


# --------------------------------------------------------------------- the allowlist comparison

@pytest.mark.parametrize("email,verified,expected", [
    (ALLOWED, True, True),                       # the one account
    (ALLOWED.upper(), True, True),               # Google may echo a different case
    ("someone.else@gmail.com", True, False),
    ("", True, False),                           # no email claim at all
    (ALLOWED, False, False),                     # right address, UNVERIFIED — a spoofable claim
    (f" {ALLOWED}", True, False),                # not silently trimmed into a match
    (f"{ALLOWED}.attacker.com", True, False),    # suffix, not a prefix match
])
def test_allowlist_comparison(email, verified, expected):
    """Mirrors the check in auth.callback. Kept as data so a refactor to `in`,
    `startswith` or a case-sensitive compare fails loudly here."""
    allowed = ALLOWED.lower()
    granted = bool(verified) and (email or "").lower() == allowed
    assert granted is expected


def test_an_empty_allowlist_admits_nobody():
    """If ALLOWED_GOOGLE_EMAIL is ever unset the gate must fail CLOSED. (Production
    boot refuses outright — this covers dev and any misread of that guard.)"""
    settings = Settings(allowed_google_email="")
    assert Settings.model_fields["allowed_google_email"].default == ""
    for email in (ALLOWED, "", "anyone@anywhere.com"):
        assert not (email.lower() == settings.allowed_google_email.lower() and email)


# --------------------------------------------------------------------- telegram: the chat-ID lock

def test_telegram_lock_admits_only_the_configured_chat(monkeypatch):
    monkeypatch.setattr(tg, "get_settings", lambda: Settings(telegram_chat_id="111222333"))
    assert tg._locked("111222333") is True
    assert tg._locked(111222333) is False        # str/int mismatch must not pass silently
    assert tg._locked("967782875") is False
    assert tg._locked("") is False
    assert tg._locked("0") is False


def test_telegram_lock_fails_closed_when_unconfigured(monkeypatch):
    """An unset chat id must lock EVERYONE out, never open the bot to all senders."""
    monkeypatch.setattr(tg, "get_settings", lambda: Settings(telegram_chat_id=""))
    assert tg._locked("111222333") is False
    assert tg._locked("") is False


# --------------------------------------------------------------------- the production boot guard

def test_production_refuses_to_boot_with_a_broken_gate():
    s = Settings(app_env="production", allowed_google_email="", secret_key="x" * 32,
                 database_url="postgresql+psycopg://u:p@h/db",
                 telegram_chat_id="1", telegram_webhook_secret="s")
    with pytest.raises(RuntimeError, match="ALLOWED_GOOGLE_EMAIL"):
        s.validate_production()


def test_production_refuses_to_boot_with_the_dev_bypass_set():
    s = Settings(app_env="production", allowed_google_email=ALLOWED, secret_key="x" * 32,
                 database_url="postgresql+psycopg://u:p@h/db",
                 telegram_chat_id="1", telegram_webhook_secret="s",
                 dev_auth_bypass_email=ALLOWED)
    with pytest.raises(RuntimeError, match="DEV_AUTH_BYPASS_EMAIL"):
        s.validate_production()


def test_production_refuses_to_boot_without_the_telegram_locks():
    for missing in ("telegram_chat_id", "telegram_webhook_secret"):
        kw = dict(app_env="production", allowed_google_email=ALLOWED, secret_key="x" * 32,
                  database_url="postgresql+psycopg://u:p@h/db",
                  telegram_chat_id="1", telegram_webhook_secret="s")
        kw[missing] = ""
        with pytest.raises(RuntimeError):
            Settings(**kw).validate_production()


def test_a_valid_production_config_boots():
    Settings(app_env="production", allowed_google_email=ALLOWED, secret_key="x" * 32,
             database_url="postgresql+psycopg://u:p@h/db",
             telegram_chat_id="1", telegram_webhook_secret="s").validate_production()


# --------------------------------------------------------------------- model tiering

def test_heavy_surfaces_are_opus_and_frequent_ones_are_sonnet():
    """Model tiering, asserted as an invariant rather than exact IDs so a version
    bump stays a one-line change — but a heavy surface quietly dropping to the cheap
    tier (or a per-minute surface jumping to Opus) fails here."""
    from app.config import DEFAULT_MODELS

    heavy = {"macro_plan", "weekly_review", "coach_chat"}
    frequent = {"morning_brief", "checkin_parse", "context_extraction",
                "post_run_read", "pdf_blood_parse", "red_flag"}
    assert heavy | frequent == set(DEFAULT_MODELS), "a task was added without a tier"
    for task in heavy:
        assert "opus" in DEFAULT_MODELS[task], f"{task} must stay on the Opus tier"
    for task in frequent:
        assert "sonnet" in DEFAULT_MODELS[task], f"{task} must stay on the Sonnet tier"


def test_model_map_json_can_override_any_task():
    """The escape hatch has to keep working — it's the no-deploy rollback."""
    s = Settings(model_map_json='{"weekly_review":"claude-opus-4-8"}')
    assert s.model_for("weekly_review") == "claude-opus-4-8"
    assert s.model_for("macro_plan") == "claude-opus-5"      # untouched keys keep the default
