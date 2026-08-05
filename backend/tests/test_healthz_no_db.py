"""/healthz must NEVER open a database connection.

Render polls the health check continuously. When it ran `SELECT 1` per poll, every
poll reset Neon's 5-minute scale-to-zero timer, so the compute never suspended:
~4.5 CU-hrs/day against a 100 CU-hrs/month free budget (99.57/100 burned by Jul 22
2026) for a workload that needs minutes a day. This test locks the rule in — the
regression is silent and only shows up weeks later on the usage bill.
"""
import pytest
from fastapi.testclient import TestClient


class _ConnectSpy:
    """Stands in for engine.connect: records the call and fails loudly if used."""

    def __init__(self):
        self.calls = 0

    def __call__(self, *args, **kwargs):
        self.calls += 1
        raise RuntimeError("healthz opened a DB connection")


@pytest.fixture
def client_and_spy(monkeypatch):
    from app import main
    spy = _ConnectSpy()
    monkeypatch.setattr(main.engine, "connect", spy)
    # raise_server_exceptions=False so a regression surfaces as a 500 we can assert
    # on, rather than blowing up the test with a traceback.
    return TestClient(main.app, raise_server_exceptions=False), spy


def test_healthz_opens_no_db_connection(client_and_spy):
    client, spy = client_and_spy
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"
    assert spy.calls == 0, "healthz must not touch the DB — it defeats Neon scale-to-zero"


def test_healthz_db_requires_a_signed_in_user(client_and_spy):
    """The DB probe wakes Neon for >=5 min a hit and the app is on a public URL, so
    it must not be reachable by a crawler that walks to /healthz/db. Unauthed, it
    must 401 WITHOUT opening a connection — a 401 after the query would still burn
    the compute."""
    client, spy = client_and_spy
    r = client.get("/healthz/db")
    assert r.status_code == 401
    assert spy.calls == 0


def test_healthz_db_still_checks_the_database_when_authed(client_and_spy, monkeypatch):
    """The explicit diagnostic keeps its DB check (it just isn't polled, or public)."""
    from app import main
    from app.auth import current_user
    main.app.dependency_overrides[current_user] = lambda: "athlete@example.com"
    try:
        client, spy = client_and_spy
        r = client.get("/healthz/db")
        assert spy.calls == 1
        assert r.status_code == 503  # the spy raises, so it reports degraded
    finally:
        main.app.dependency_overrides.clear()
