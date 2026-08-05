"""Lifestyle store + the recent_lifestyle surfacing the coach reads. (The parse and
the prompt now live in coach/debrief.py — see test_debrief.py; this covers the store
helper and the signal.)"""
from datetime import date, timedelta

from sqlalchemy import select

from app.coach import lifestyle, signals
from app.models import LifestyleLog


def test_upsert_merges_onto_the_same_day_rather_than_replacing(db):
    """Merge is the default so a tap and a typed line can't clobber each other in
    either order (same skip-nulls contract as checkin._upsert)."""
    d = date(2026, 7, 19)
    lifestyle._upsert(db, d, raw_text="2 beers, late night",
                      data={"alcohol": "2 beers", "sleep": "late", "summary": "beers + late"})
    lifestyle._upsert(db, d, raw_text=None,                     # same day → update, not insert
                      data={"alcohol": "3 beers", "illness": None})
    rows = db.scalars(select(LifestyleLog).where(LifestyleLog.date == d)).all()
    assert len(rows) == 1
    assert rows[0].data["alcohol"] == "3 beers"                 # refined
    assert rows[0].data["sleep"] == "late"                      # untouched field survives
    assert "illness" not in rows[0].data or not rows[0].data["illness"]   # nulls skipped
    assert rows[0].raw_text == "2 beers, late night"            # raw_text=None leaves it alone


def test_upsert_replace_wipes_the_days_flags(db):
    """Only the explicit 'nothing to flag' path replaces — it has to be able to
    contradict an earlier tap."""
    d = date(2026, 7, 19)
    lifestyle._upsert(db, d, raw_text="beers", data={"alcohol": "2 beers"})
    lifestyle._upsert(db, d, raw_text=None, data={"summary": "clear"}, replace=True)
    row = db.scalar(select(LifestyleLog).where(LifestyleLog.date == d))
    assert row.data == {"summary": "clear"}


def test_recent_lifestyle_surfaces_only_flagged_fields(db):
    today = date(2026, 7, 19)
    lifestyle._upsert(db, today - timedelta(days=1), raw_text="beers",
                      data={"alcohol": "2 beers", "illness": None, "summary": "beers"})
    lifestyle._upsert(db, today, raw_text="(nothing to report)", data={})  # a clear day
    out = signals.recent_lifestyle(db, today, days=10)
    assert len(out) == 2
    beers = next(o for o in out if o["date"] == (today - timedelta(days=1)).isoformat())
    assert beers["alcohol"] == "2 beers"
    assert "illness" not in beers                # null fields dropped
    clear = next(o for o in out if o["date"] == today.isoformat())
    assert clear["summary"] == "(nothing to report)"  # falls back to raw_text when no summary


def test_upsert_is_conflict_safe(db, monkeypatch):
    from sqlalchemy.exc import IntegrityError
    real_commit = db.commit
    state = {"raised": False}

    def flaky():
        if not state["raised"]:
            state["raised"] = True
            raise IntegrityError("insert", {}, Exception("duplicate date"))
        return real_commit()

    monkeypatch.setattr(db, "commit", flaky)
    row = lifestyle._upsert(db, date(2026, 7, 19), raw_text="x", data={"alcohol": "beer"})
    assert row.data == {"alcohol": "beer"}   # recovered on retry
