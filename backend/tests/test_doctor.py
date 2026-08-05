"""The preflight check.

Tested for the same reason `monitor` and `backup` are: it fails invisibly. A
doctor that silently reports "all ok" on a broken install is worse than no doctor
at all, because it converts a visible problem into a confident wrong answer.
"""
import pytest

from app import doctor
from app.config import Settings


def _settings(**over) -> Settings:
    """A fully-configured install, so each test can break exactly one thing."""
    base = dict(
        app_env="production", anthropic_api_key="sk-test", database_url="postgresql://x/y",
        secret_key="a-real-secret", allowed_google_email="athlete@example.com",
        telegram_bot_token="tok", telegram_chat_id="123", telegram_webhook_secret="whs",
        google_client_id="cid", google_client_secret="cs", google_refresh_token="rt",
        garth_token="gt", garmin_sync_enabled=True, garmin_workout_push_enabled=True,
    )
    base.update(over)
    return Settings(**base)


def _run(s) -> doctor.Report:
    r = doctor.Report()
    for check in (doctor._check_core, doctor._check_gates, doctor._check_google,
                  doctor._check_garmin, doctor._check_models):
        check(s, r)
    return r


def _find(report, area):
    return [row for row in report.rows if row[1] == area]


def test_a_fully_configured_install_has_no_failures():
    """The baseline. If this goes red, every other test here is meaningless."""
    r = _run(_settings())
    assert not [row for row in r.rows if row[0] == doctor.FAIL], r.render()


@pytest.mark.parametrize("broken,area", [
    ({"anthropic_api_key": ""}, "Anthropic"),
    ({"allowed_google_email": ""}, "Web gate"),
    ({"telegram_chat_id": ""}, "Telegram gate"),
    ({"telegram_webhook_secret": ""}, "Telegram webhook"),
    ({"garth_token": ""}, "Garmin"),
    ({"secret_key": "dev-only-insecure-key"}, "Session key"),
    ({"database_url": "sqlite:///x.db"}, "Database"),
    ({"dev_auth_bypass_email": "someone@example.com"}, "Auth bypass"),
])
def test_each_broken_credential_is_reported_as_a_failure(broken, area):
    """Every one of these breaks the app in production, and every one of them is
    silent at runtime: a missing key surfaces as a job that just doesn't do
    anything. The doctor exists to make each of them loud, once."""
    r = _run(_settings(**broken))
    rows = _find(r, area)
    assert rows and rows[0][0] == doctor.FAIL, f"{area} not failed: {r.render()}"
    assert rows[0][3], f"{area} reported a problem but no fix"


def test_every_non_ok_row_tells_you_what_to_do():
    """A check that says what's wrong without saying what to do has moved the
    problem, not solved it — the whole point for a non-expert self-hoster."""
    r = _run(_settings(anthropic_api_key="", garth_token="", telegram_bot_token=""))
    for status, area, _msg, fix in r.rows:
        if status != doctor.OK:
            assert fix, f"{area} has no remediation"


def test_the_google_consent_trap_is_always_surfaced():
    """It cannot be detected from config: a consent screen left in "Testing" works
    for 7 days and then the refresh token dies. So it is stated unconditionally,
    even on an otherwise perfect install."""
    r = _run(_settings())
    rows = _find(r, "Google consent")
    assert rows and "In production" in rows[0][2]


def test_dev_install_is_warned_not_failed():
    """Local dev has no secret key and no production gates. That must read as
    warnings, or developers learn to ignore the failures that do matter."""
    r = _run(_settings(app_env="development", secret_key="dev-only-insecure-key",
                       database_url="sqlite:///dev.db"))
    assert _find(r, "Session key")[0][0] == doctor.WARN
    assert _find(r, "Database")[0][0] == doctor.OK


def test_broken_model_map_is_caught_not_raised():
    """MODEL_MAP_JSON is hand-edited env. Malformed JSON must be reported, never
    crash the one command someone runs when things are already going wrong."""
    r = _run(_settings(model_map_json="{not json"))
    assert _find(r, "Models")[0][0] == doctor.FAIL


def test_worst_drives_the_exit_code_ordering():
    """The exit code is the deploy-gate contract: 0 clean, 1 warnings, 2 broken."""
    r = doctor.Report()
    assert r.worst == doctor.OK
    r.add(doctor.OK, "a", "fine")
    assert r.worst == doctor.OK
    r.add(doctor.WARN, "b", "later")
    assert r.worst == doctor.WARN
    r.add(doctor.FAIL, "c", "now")
    assert r.worst == doctor.FAIL


def test_render_output_is_pure_ascii():
    """This is the first command a new install runs, often on a Windows console
    defaulting to cp1252, where an em-dash renders as a replacement character.
    Mojibake in a diagnostic tool undermines the diagnosis."""
    text = _run(_settings(anthropic_api_key="", garth_token="")).render()
    assert text.isascii(), [c for c in text if not c.isascii()]


def test_run_never_raises_even_with_nothing_configured(monkeypatch):
    """The doctor is what you reach for when the app won't start. If it can crash
    on a broken install, it is useless in precisely the situation it exists for."""
    monkeypatch.setattr(doctor, "_check_athlete", lambda r: None)  # no DB in tests
    monkeypatch.setattr("app.config.get_settings", lambda: Settings())
    assert doctor.run().render()
