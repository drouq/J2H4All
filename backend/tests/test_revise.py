"""Pending-Edit flag windowing: a forgotten ✏️ Edit tap must not hijack an unrelated
free-text message (a debrief, a coaching question) hours later — the same 30-min
window the check-in/debrief awaiting flags use."""
from datetime import timedelta

from sqlalchemy import select

from app.coach import checkin, revise
from app.models import Preference
from app.util import utcnow


def _backdate(db, minutes: int):
    pref = db.scalar(select(Preference).where(Preference.key == revise._PENDING_KEY))
    pref.updated_at = utcnow() - timedelta(minutes=minutes)
    db.commit()


def test_pop_none_when_nothing_pending(db):
    assert revise.pop_pending_edit(db) is None


def test_pop_returns_id_within_window(db):
    revise.set_pending_edit(db, 42)
    assert revise.pop_pending_edit(db) == 42
    # consumed → cleared
    assert db.scalar(select(Preference).where(Preference.key == revise._PENDING_KEY)) is None


def test_pop_expires_stale_edit(db):
    revise.set_pending_edit(db, 42)
    _backdate(db, int(checkin.REPLY_WINDOW.total_seconds() // 60) + 5)
    assert revise.pop_pending_edit(db) is None   # stale → not returned
    # and the stale flag is cleared so it can't linger
    assert db.scalar(select(Preference).where(Preference.key == revise._PENDING_KEY)) is None


def test_pop_active_at_window_edge(db):
    revise.set_pending_edit(db, 7)
    _backdate(db, int(checkin.REPLY_WINDOW.total_seconds() // 60) - 1)
    assert revise.pop_pending_edit(db) == 7


# --- carry-through: a redraft that returns only what it touched must not delete the rest


def _s(d, title, type_="easy"):
    return {"date": d, "type": type_, "title": title, "duration_min": 45, "distance_km": None,
            "target_zone": "Z2", "target_pace": None, "purpose": "p", "fueling_note": None,
            "structure": None}


def test_carry_through_restores_omitted_dates():
    original = [_s("2026-10-20", "Easy"), _s("2026-10-21", "Gym", "strength"),
                _s("2026-11-12", "Strides"), _s("2026-11-15", "Long", "long_run")]
    revised = [_s("2026-11-12", "Tempo", "tempo"), _s("2026-11-15", "Long w/ MP", "long_run")]
    merged, carried = revise.carry_through(original, revised)
    assert [s["title"] for s in merged] == ["Easy", "Gym", "Tempo", "Long w/ MP"]
    assert carried == ["2026-10-20", "2026-10-21"]


def test_carry_through_redraft_wins_on_a_covered_date():
    # Two originals on one date (run + gym), redraft covers that date with one rest
    # day: the date is the redraft's — removing a session is a rest day, not omission.
    original = [_s("2026-10-24", "Run"), _s("2026-10-24", "Gym", "strength")]
    revised = [_s("2026-10-24", "Rest", "rest")]
    merged, carried = revise.carry_through(original, revised)
    assert [s["title"] for s in merged] == ["Rest"]
    assert carried == []


def test_revise_proposal_keeps_sessions_the_model_dropped(db, monkeypatch):
    """From the reference deployment: a 31-session card edited down to its 2
    changed sessions wiped the other 29 on approval."""
    from app.plan import proposals as plan_proposals

    original = [_s(f"2026-10-{d}", f"Run {d}") for d in range(20, 31)]
    p = plan_proposals.create(db, kind="sessions", summary="weekly",
                              payload={"sessions": original, "change_note": None},
                              origin="weekly_review")
    monkeypatch.setattr(revise, "call_tool", lambda **kw: {
        "summary": "Reframed", "change_note": "purpose text only",
        "sessions": [_s("2026-10-25", "Run 25 reframed")]})
    new_p, _ = revise.revise_proposal(db, p.id, "make it a prep race")
    titles = [s["title"] for s in new_p.payload["sessions"]]
    assert len(titles) == 11
    assert "Run 25 reframed" in titles and "Run 25" not in titles
    assert titles[0] == "Run 20" and titles[-1] == "Run 30"
