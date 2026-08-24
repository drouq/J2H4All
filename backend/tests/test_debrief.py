"""Combined 21:00 debrief: one free-text reply fans out to BOTH the check-in scores
(feel, merged onto any earlier tap) and the lifestyle flags. Preserves the check-in
data path (so the soreness>=4 red-flag trigger still fires) while adding lifestyle.
Also covers the one-tap life flags — the half that in production was answerable only
by typing, and so recorded one row in six weeks."""
from datetime import date

from sqlalchemy import select

from app.coach import checkin, debrief, lifestyle, signals
from app.context import store
from app.models import Checkin, LifestyleLog

_D = date(2026, 7, 19)

_FULL = {  # a fully-populated parse
    "energy": 2, "soreness": 4, "motivation": 3, "life_stress": 4,
    "note": "legs wrecked", "alcohol": "2 beers", "illness": None,
    "sleep": "late night", "nutrition": None, "training_extra": None,
    "stress": "work deadline", "summary": "Rough one — beers, late, sore.",
}


def test_reply_writes_both_checkin_and_lifestyle(db, monkeypatch):
    monkeypatch.setattr(debrief, "call_tool", lambda **kw: dict(_FULL))
    debrief.record_reply(db, "wrecked legs, 2 beers, late night, work stress", today=date(2026, 7, 19))

    ci = db.scalar(select(Checkin).where(Checkin.date == date(2026, 7, 19)))
    assert ci.soreness == 4 and ci.energy == 2 and ci.note == "legs wrecked"
    ll = db.scalar(select(LifestyleLog).where(LifestyleLog.date == date(2026, 7, 19)))
    assert ll.data["alcohol"] == "2 beers" and ll.data["stress"] == "work deadline"
    assert ll.raw_text.startswith("wrecked legs")   # raw kept


def test_reply_merges_onto_an_earlier_tap(db, monkeypatch):
    d = date(2026, 7, 19)
    checkin.record_quick(db, "meh", today=d)          # baseline 3/3/3/3
    # A follow-up line mentions only soreness + a note; energy/motivation stay at the tap.
    partial = {**{k: None for k in _FULL}, "soreness": 5, "note": "knee flared", "summary": "knee"}
    monkeypatch.setattr(debrief, "call_tool", lambda **kw: partial)
    debrief.record_reply(db, "knee flared up on the run", today=d)

    ci = db.scalar(select(Checkin).where(Checkin.date == d))
    assert ci.soreness == 5          # refined by the reply
    assert ci.energy == 3            # kept from the tap (reply left it null)
    assert ci.note == "knee flared"


def test_reply_keeps_raw_when_llm_unavailable(db, monkeypatch):
    def _raise(**kw):
        raise debrief.LLMNotConfigured("no key")
    monkeypatch.setattr(debrief, "call_tool", _raise)
    debrief.record_reply(db, "felt ok, had a glass of wine", today=date(2026, 7, 19))
    ci = db.scalar(select(Checkin).where(Checkin.date == date(2026, 7, 19)))
    ll = db.scalar(select(LifestyleLog).where(LifestyleLog.date == date(2026, 7, 19)))
    assert ci.note == "felt ok, had a glass of wine"
    assert ll.raw_text == "felt ok, had a glass of wine" and not ll.data


def test_awaiting_key_is_debrief_specific_and_internal(db):
    checkin.set_awaiting(db, debrief.AWAITING_KEY)
    assert checkin.awaiting_active(db, debrief.AWAITING_KEY) is True
    assert checkin.awaiting_active(db) is False   # not the old check-in channel
    assert store._is_internal_pref(debrief.AWAITING_KEY)
    assert debrief.AWAITING_KEY not in {p["key"] for p in store.snapshot(db)["preferences"]}


def test_prompt_card_offers_feel_and_life_taps(db):
    _, keyboard = debrief.prompt_card(db, _D)
    datas = [b["callback_data"] for row in keyboard for b in row]
    assert set(datas) == {
        "ci:fresh", "ci:good", "ci:meh", "ci:tired",
        "lf:alcohol", "lf:illness", "lf:sleep", "lf:stress", "lf:none",
    }


# ------------------------------------------------- life flags are tappable, not typed-only

def test_life_tap_writes_a_flag_without_any_typing(db):
    assert lifestyle.record_tap(db, "alcohol", _D) == (lifestyle.TAPS["alcohol"]["label"], True)
    ll = db.scalar(select(LifestyleLog).where(LifestyleLog.date == _D))
    assert ll.data["alcohol"]
    # And it reaches the coach the same way a typed flag does.
    assert signals.recent_lifestyle(db, _D, days=3)[-1]["alcohol"]


def test_life_taps_accumulate_and_show_on_the_recard(db):
    lifestyle.record_tap(db, "alcohol", _D)
    lifestyle.record_tap(db, "sleep", _D)
    ll = db.scalar(select(LifestyleLog).where(LifestyleLog.date == _D))
    assert ll.data["alcohol"] and ll.data["sleep"]      # the second didn't clobber the first

    text, keyboard = debrief.render_card(db, _D)
    labels = [b["text"] for row in keyboard for b in row]
    assert sum(lbl.startswith("✓") for lbl in labels) == 2
    assert "Logged:" in text


def test_tapping_a_flag_twice_removes_it(db):
    """Several flags can be on at once, so a tap toggles — a ✓ you can't clear would
    leave a mis-tap only undoable by wiping the day."""
    lifestyle.record_tap(db, "alcohol", _D)
    lifestyle.record_tap(db, "stress", _D)
    assert lifestyle.record_tap(db, "alcohol", _D) == (lifestyle.TAPS["alcohol"]["label"], False)

    ll = db.scalar(select(LifestyleLog).where(LifestyleLog.date == _D))
    assert not ll.data.get("alcohol")     # gone, not left as a null
    assert ll.data["stress"]              # the other flag is untouched
    assert lifestyle.logged_flags(db, _D) == {"stress"}


def test_a_flag_tap_overrides_an_earlier_nothing_to_flag(db):
    lifestyle.record_tap(db, lifestyle.CLEAR_TAP, _D)
    lifestyle.record_tap(db, "illness", _D)
    ll = db.scalar(select(LifestyleLog).where(LifestyleLog.date == _D))
    assert ll.data["illness"] and "summary" not in ll.data
    assert lifestyle.logged_flags(db, _D) == {"illness"}


def test_nothing_to_flag_tap_clears_earlier_flags(db):
    lifestyle.record_tap(db, "alcohol", _D)
    lifestyle.record_tap(db, lifestyle.CLEAR_TAP, _D)
    ll = db.scalar(select(LifestyleLog).where(LifestyleLog.date == _D))
    assert not ll.data.get("alcohol")
    assert ll.data["summary"] == lifestyle.CLEAR_SUMMARY
    assert lifestyle.logged_flags(db, _D) == {lifestyle.CLEAR_TAP}


def test_a_typed_line_refines_a_tapped_flag_and_keeps_the_others(db, monkeypatch):
    lifestyle.record_tap(db, "alcohol", _D)
    lifestyle.record_tap(db, "stress", _D)
    partial = {**{k: None for k in _FULL}, "alcohol": "3 beers actually", "summary": "beers"}
    monkeypatch.setattr(debrief, "call_tool", lambda **kw: partial)
    debrief.record_reply(db, "make that 3 beers", today=_D)

    ll = db.scalar(select(LifestyleLog).where(LifestyleLog.date == _D))
    assert ll.data["alcohol"] == "3 beers actually"   # typed detail wins
    assert ll.data["stress"]                          # untouched tap survives


def test_a_failed_parse_does_not_wipe_tapped_flags(db, monkeypatch):
    lifestyle.record_tap(db, "illness", _D)
    monkeypatch.setattr(debrief, "call_tool", lambda **kw: (_ for _ in ()).throw(RuntimeError("boom")))
    debrief.record_reply(db, "bit of a sore throat", today=_D)

    ll = db.scalar(select(LifestyleLog).where(LifestyleLog.date == _D))
    assert ll.data["illness"]                         # kept
    assert ll.raw_text == "bit of a sore throat"


def test_feel_tap_is_marked_on_the_recard(db):
    checkin.record_quick(db, "tired", today=_D)
    _text, keyboard = debrief.render_card(db, _D)
    tired = next(b for row in keyboard for b in row if b["callback_data"] == "ci:tired")
    good = next(b for row in keyboard for b in row if b["callback_data"] == "ci:good")
    assert tired["text"].startswith("✓") and not good["text"].startswith("✓")


# ---- the card acknowledges what actually happened (2026-08-24) -----------------
# It was FIXED text: it asked "how did today land?" without knowing whether the day held
# a long run, a gym session or nothing - while the coach's own read of the session, written by every
# sync since Phase 5, sat unread in session_result.read_summary.
#
# Keyed off the ACTIVITY date, never the session date: a scheduled workout is selected
# on his watch and runs it early or late, and link_results matches by workoutId with no
# date constraint. Session-keying would go silent on the evening he actually trained.

def _activity(db, aid, when, *, atype="running", minutes=156.0, km=23.6, hr=134):
    from app.models import Activity
    from app.util import utcnow
    db.add(Activity(id=aid, start_time_utc=utcnow(), start_time_local=when,
                    activity_type=atype, name="run", duration_s=minutes * 60,
                    distance_m=(km * 1000) if km else None, avg_hr=hr, raw={},
                    synced_at=utcnow(), detail_synced=False, streams_synced=False))
    db.commit()


def _did(db, *, ran_on, planned_for=None, stype="long_run", title="Long Run - 2h30 Z2",
         read=None, minutes=156.0, km=23.6, hr=134, aid=9001, atype="running"):
    """An activity on `ran_on`, optionally fulfilling a session planned for another day."""
    from datetime import datetime, time

    from app.models import Session as PlannedSession
    from app.models import SessionResult
    from app.util import utcnow
    _activity(db, aid, datetime.combine(ran_on, time(18, 0)), atype=atype,
              minutes=minutes, km=km, hr=hr)
    sid = None
    if planned_for is not None:
        s = PlannedSession(date=planned_for, type=stype, title=title, purpose="",
                           status="planned", created_at=utcnow(), updated_at=utcnow())
        db.add(s)
        db.flush()
        sid = s.id
    db.add(SessionResult(session_id=sid, activity_id=aid, completed=True,
                         actual_duration_min=minutes, actual_distance_km=km,
                         actual_avg_hr=hr, read_summary=read, created_at=utcnow()))
    db.commit()


def test_card_names_the_session_and_carries_the_read(db):
    _did(db, ran_on=_D, planned_for=_D, read="HR held in Z2 all the way, but pace faded late.")
    text, _kb = debrief.render_card(db, _D)
    assert "Long Run - 2h30 Z2" in text
    assert "156 min" in text and "23.6 km" in text and "avg HR 134" in text
    assert "pace faded late" in text          # the read finally reaches him
    assert "How did that land" in text        # the question now refers to something
    assert "planned" not in text              # ran it on its own day - nothing to explain


def test_a_run_done_AHEAD_of_schedule_is_acknowledged_tonight(db):
    """Friday's workout is selected on the watch and run on Wednesday. link_results
    matches by workoutId regardless of date, so the session is already complete - and
    tonight's debrief has to be about the run he just did."""
    from datetime import timedelta
    _did(db, ran_on=_D, planned_for=_D + timedelta(days=2), read="Solid Z2 throughout.")
    text, _kb = debrief.render_card(db, _D)
    assert "Long Run - 2h30 Z2" in text and "Solid Z2 throughout." in text
    assert "planned" in text                  # says which day it was for


def test_a_run_done_LATE_is_acknowledged_on_the_day_he_ran_it(db):
    from datetime import timedelta
    _did(db, ran_on=_D, planned_for=_D - timedelta(days=1))
    text, _kb = debrief.render_card(db, _D)
    assert "Long Run - 2h30 Z2" in text and "planned" in text


def test_the_session_date_alone_never_triggers_it(db):
    """The mirror of the two above: a session planned for today that has not been done
    must not be acknowledged just because the date matches."""
    from datetime import timedelta
    _did(db, ran_on=_D - timedelta(days=1), planned_for=_D)   # run yesterday, planned today
    text, _kb = debrief.render_card(db, _D)
    assert "Long Run" not in text
    assert "how did today land" in text


def test_gym_is_acknowledged_too(db):
    """run_reads writes a read for any linked result, gym included."""
    _did(db, ran_on=_D, planned_for=_D, stype="strength", title="Gym - Upper Body (Push)",
         read="Avg HR 93 is where a strength supplement should sit.",
         minutes=45.0, km=None, hr=93, atype="strength_training")
    text, _kb = debrief.render_card(db, _D)
    assert "Gym - Upper Body (Push)" in text and "strength supplement" in text
    assert "23.6 km" not in text              # no distance on a gym session


def test_an_unplanned_run_still_counts_as_training(db):
    """No session, so no read - but it happened, and a debrief that ignored it would be
    asking about a day it had not looked at."""
    _did(db, ran_on=_D, planned_for=None)
    text, _kb = debrief.render_card(db, _D)
    assert "Unplanned run" in text and "23.6 km" in text


def test_a_session_without_a_read_still_gets_acknowledged(db):
    """The read is best-effort (a failed sync, the LLM unavailable). The
    acknowledgement must not depend on it."""
    _did(db, ran_on=_D, planned_for=_D, read=None)
    text, _kb = debrief.render_card(db, _D)
    assert "Long Run - 2h30 Z2" in text and "156 min" in text


def test_a_hand_marked_session_is_acknowledged(db):
    """mark_session_done writes a result with no activity behind it."""
    from app.models import Session as PlannedSession
    from app.models import SessionResult
    from app.util import utcnow
    s = PlannedSession(date=_D, type="strength", title="Gym - Pull", purpose="",
                       status="planned", created_at=utcnow(), updated_at=utcnow())
    db.add(s)
    db.flush()
    db.add(SessionResult(session_id=s.id, activity_id=None, completed=True,
                         created_at=utcnow()))
    db.commit()
    text, _kb = debrief.render_card(db, _D)
    assert "Gym - Pull" in text and "marked done" in text


def test_a_day_with_no_training_keeps_the_original_question(db):
    text, _kb = debrief.render_card(db, _D)
    assert "how did today land" in text
    assert "✅" not in text


def test_an_unrun_planned_session_is_not_claimed_as_done(db):
    """A session on the calendar was not done must never be acknowledged - that would
    be the system telling him training happened when it did not."""
    from app.models import Session as PlannedSession
    from app.util import utcnow
    db.add(PlannedSession(date=_D, type="easy", title="Easy Aerobic - 60 min", purpose="",
                          status="planned", created_at=utcnow(), updated_at=utcnow()))
    db.commit()
    text, _kb = debrief.render_card(db, _D)
    assert "Easy Aerobic" not in text
    assert "how did today land" in text
