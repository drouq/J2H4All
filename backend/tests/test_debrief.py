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
