"""The post-daily-sync calendar reconcile must be PRODUCTION-ONLY.

Root cause (2026-07-23): a forgotten local Scheduled Task ran daily_sync against
the stale dev-mirror DB; the calendar token resolves from the DB's oauth_credential
row, so its reconcile rewrote the PROD Google Calendar from a weeks-old plan every
morning — deleting current events and reviving old ones. The guard makes a
dev-environment sync incapable of touching the live calendar, whatever DB or
scheduled task invokes it."""
import pytest

from app import jobs


class _Settings:
    def __init__(self, production):
        self._p = production

    @property
    def is_production(self):
        return self._p


@pytest.fixture
def spy_reconcile(monkeypatch):
    calls = []
    from app.calendar import sync as cal_sync
    monkeypatch.setattr(cal_sync, "safe_reconcile", lambda db: calls.append(1) or {"updated": 0})
    return calls


def test_dev_environment_never_reconciles_the_calendar(monkeypatch, spy_reconcile):
    from app import config
    monkeypatch.setattr(config, "get_settings", lambda: _Settings(production=False))
    jobs._push_calendar_after_daily_sync()
    assert spy_reconcile == []  # a dev-run sync must never write the live calendar


def test_production_still_reconciles(monkeypatch, spy_reconcile):
    from app import config
    monkeypatch.setattr(config, "get_settings", lambda: _Settings(production=True))
    jobs._push_calendar_after_daily_sync()
    assert spy_reconcile == [1]
