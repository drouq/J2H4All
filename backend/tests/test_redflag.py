"""Red-flag hooks. (1) The lifestyle hook: a self-reported illness log triggers a
proactive reason ONLY when a physiological signal corroborates it (avoids over-firing
on a bare 'bit run down' note). (2) Reason-keyed dedupe: a lingering signal must not
re-propose a card every sync. Recovery/deep signals are stubbed so the DB setup stays
light."""
from datetime import date, timedelta

from app.coach import redflag, signals
from app.models import LifestyleLog, Proposal, Session, SessionResult
from app.util import as_dt, utcnow

_TODAY = date(2026, 7, 19)


def _log_illness(db, day=_TODAY):
    db.add(LifestyleLog(date=day, raw_text="feel awful",
                        data={"illness": "sore throat, feverish"},
                        created_at=utcnow(), updated_at=utcnow()))
    db.commit()


def _stub(monkeypatch, *, rhr_recent, rhr_base, deep=None):
    monkeypatch.setattr(signals, "recovery_baseline", lambda db, t: {
        "hrv_recent_3d": None, "hrv_baseline_28d": None,
        "rhr_recent_3d": rhr_recent, "rhr_baseline_28d": rhr_base})
    monkeypatch.setattr(signals, "deep_recovery", lambda db, t: deep or {"latest": {}})


def test_illness_with_corroborating_marker_triggers(db, monkeypatch):
    _log_illness(db)
    _stub(monkeypatch, rhr_recent=53, rhr_base=46)  # +7 bpm → elevated
    reasons = redflag.detect(db, _TODAY)
    assert any("Logged feeling ill" in r for r in reasons)


def test_illness_alone_does_not_trigger(db, monkeypatch):
    _log_illness(db)
    _stub(monkeypatch, rhr_recent=46, rhr_base=46)  # no marker move, no skin/resp
    reasons = redflag.detect(db, _TODAY)
    assert not any("Logged feeling ill" in r for r in reasons)


def test_marker_move_without_illness_flag_has_no_illness_reason(db, monkeypatch):
    # RHR elevated on its own still red-flags (its own reason), but not the illness one.
    _stub(monkeypatch, rhr_recent=53, rhr_base=46)
    reasons = redflag.detect(db, _TODAY)
    assert any("Resting HR elevated" in r for r in reasons)
    assert not any("Logged feeling ill" in r for r in reasons)


# ------------------------------------------------------------------ reason-keyed dedupe

def _flagged_run(db, day=_TODAY):
    # Timestamps anchored to _TODAY, not the wall clock — `detect` and `raised_keys`
    # both window on it.
    r = SessionResult(session_id=None, activity_id=1, completed=True, flagged=True,
                      note="cut short with no HR signal", created_at=as_dt(day))
    db.add(r)
    db.commit()
    return r


def _upcoming_session(db, day=_TODAY):
    db.add(Session(date=day + timedelta(days=1), type="easy", title="Easy 40 min",
                   purpose="aerobic", status="planned",
                   created_at=as_dt(day), updated_at=as_dt(day)))
    db.commit()


def _stub_llm(monkeypatch):
    """The drafting call — irrelevant to dedupe, so return a minimal valid draft."""
    monkeypatch.setattr(redflag, "call_tool", lambda **kw: {
        "summary": "Easing the next few days.", "change_note": "Shortened tomorrow.",
        "sessions": [],
    })


def test_detect_flags_key_a_flagged_run_by_its_result_id(db, monkeypatch):
    _stub(monkeypatch, rhr_recent=46, rhr_base=46)
    r = _flagged_run(db)
    keys = dict(redflag.detect_flags(db, _TODAY))
    assert f"flagged_run:{r.id}" in keys
    # The text view stays exactly what prompts used to receive.
    assert redflag.detect(db, _TODAY) == list(keys.values())


def test_raised_keys_ignores_proposals_older_than_the_window(db):
    fresh = Proposal(kind="sessions", status="approved", origin="red_flag", summary="s",
                     payload={"red_flag_keys": ["rhr_elevated"]},
                     created_at=as_dt(_TODAY - timedelta(days=2)))
    stale = Proposal(kind="sessions", status="approved", origin="red_flag", summary="s",
                     payload={"red_flag_keys": ["hrv_suppressed"]},
                     created_at=as_dt(_TODAY - timedelta(days=redflag.DEDUPE_DAYS + 1)))
    other = Proposal(kind="sessions", status="approved", origin="weekly_review", summary="s",
                     payload={"red_flag_keys": ["soreness"]}, created_at=as_dt(_TODAY))
    db.add_all([fresh, stale, other])
    db.commit()
    assert redflag.raised_keys(db, _TODAY) == {"rhr_elevated"}


def test_lingering_flag_does_not_repropose_the_next_day(db, monkeypatch):
    """The 2026-08-03 storm: one cut-short long run produced five cards over three
    days, the last a no-op redraft of an unchanged week."""
    _stub(monkeypatch, rhr_recent=46, rhr_base=46)
    _stub_llm(monkeypatch)
    _flagged_run(db)
    _upcoming_session(db)

    first = redflag.check_and_propose(db, _TODAY)
    assert first is not None
    proposal, _summary = first
    assert proposal.payload["red_flag_keys"]

    # Same cause, a day later (and with the earlier card resolved, so the
    # one-pending-at-a-time guard is NOT what's doing the work here).
    proposal.status = "approved"
    db.commit()
    assert redflag.check_and_propose(db, _TODAY + timedelta(days=1)) is None


def test_a_new_signal_still_fires_alongside_an_already_raised_one(db, monkeypatch):
    _stub(monkeypatch, rhr_recent=46, rhr_base=46)
    _stub_llm(monkeypatch)
    _flagged_run(db)
    _upcoming_session(db)

    first = redflag.check_and_propose(db, _TODAY)
    assert first is not None
    first[0].status = "approved"
    db.commit()

    # RHR spikes the next day — a genuinely new cause, so the coach pings again,
    # and the card carries BOTH reasons.
    _stub(monkeypatch, rhr_recent=53, rhr_base=46)
    second = redflag.check_and_propose(db, _TODAY + timedelta(days=1))
    assert second is not None
    keys = second[0].payload["red_flag_keys"]
    assert "rhr_elevated" in keys
    assert any(k.startswith("flagged_run:") for k in keys)
