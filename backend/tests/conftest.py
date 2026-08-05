"""Test fixtures: an isolated in-memory SQLite DB per test.

The units under test all take a `db` session, so tests never touch the real
Postgres — we build a throwaway SQLite engine from the same models. JsonCol's
Postgres JSONB variant falls back to plain JSON on SQLite, which is enough.
"""
import os
import sys
from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool
from sqlalchemy.orm import sessionmaker

# backend/ on the path so `import app...` works when pytest runs from anywhere.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.models import Base  # noqa: E402
from app.config import Settings, get_settings  # noqa: E402

# Tests must be isolated from whatever is in the developer's backend/.env.
# Without this, following SETUP.md (which tells you to create that file) turns the
# suite red for reasons that have nothing to do with your change: Settings() would
# silently inherit APP_ENV, DEV_AUTH_BYPASS_EMAIL and the rest, so the tests that
# assert on production-gate behaviour see a development config instead. CI has no
# .env and so never caught it. The suite builds every Settings it needs explicitly.
Settings.model_config["env_file"] = None
for _leaked in ("APP_ENV", "DATABASE_URL", "SECRET_KEY", "ALLOWED_GOOGLE_EMAIL",
                "DEV_AUTH_BYPASS_EMAIL", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID",
                "TELEGRAM_WEBHOOK_SECRET", "ANTHROPIC_API_KEY", "GARTH_TOKEN",
                "GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET", "GOOGLE_REFRESH_TOKEN",
                "MODEL_MAP_JSON", "GARMIN_SYNC_ENABLED", "GARMIN_WORKOUT_PUSH_ENABLED"):
    os.environ.pop(_leaked, None)
get_settings.cache_clear()


@pytest.fixture
def db():
    # StaticPool + check_same_thread=False so ONE in-memory database is shared across
    # threads. Needed because fastapi's TestClient runs the app in a worker thread:
    # without this, any test that overrides get_db with this session dies on SQLite's
    # thread affinity rather than on the behaviour it meant to check.
    engine = create_engine(
        "sqlite://", future=True,
        connect_args={"check_same_thread": False}, poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, future=True)
    s = Session()
    try:
        yield s
    finally:
        s.close()
        engine.dispose()


@pytest.fixture
def utcnow():
    return datetime.now(timezone.utc)
