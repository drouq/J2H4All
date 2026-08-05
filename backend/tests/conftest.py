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
from sqlalchemy.orm import sessionmaker

# backend/ on the path so `import app...` works when pytest runs from anywhere.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.models import Base  # noqa: E402


@pytest.fixture
def db():
    engine = create_engine("sqlite://", future=True)
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
