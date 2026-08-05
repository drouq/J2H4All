"""Binding the bot to one chat — one of the two HARD RULES.

Pairing moved the bound chat id's SOURCE from an environment variable into the
database. That is the kind of change that quietly turns a gate into a door, so
these tests assert the gate's properties directly rather than testing the happy
path and hoping.

The property that matters most, and the one worth breaking the build over:
**unbound means NOBODY, never everybody.** Every other test here is secondary.
"""
import re
from datetime import timedelta

import pytest

import app.telegram as tg
from app import telegram_link
from app.config import Settings
from app.context.store import _is_internal_pref, snapshot, stamp_meta
from app.util import utcnow


@pytest.fixture(autouse=True)
def _no_env_chat(monkeypatch):
    """Default posture for these tests: nothing configured in the environment, so
    the paired value is what's under test. Also clears the in-process cache, which
    would otherwise leak a binding between tests."""
    s = Settings(telegram_bot_token="bot-token", telegram_chat_id="")
    monkeypatch.setattr("app.config.get_settings", lambda: s)
    monkeypatch.setattr(tg, "get_settings", lambda: s)
    telegram_link._invalidate()
    yield
    telegram_link._invalidate()


def _pair(db, chat="555"):
    code = telegram_link.start_pairing(db)["code"]
    assert telegram_link.try_pair(db, chat, code) is True
    return code


# ------------------------------------------------------------ FAIL CLOSED

def test_unbound_answers_nobody(db):
    """THE property. With no env var and nothing paired, every sender is rejected.
    If this ever inverts, the bot answers strangers."""
    assert telegram_link.bound_chat_id(db) is None
    for someone in ("1", "555", "999999999", "", "None"):
        assert tg._locked(someone, db) is False


def test_a_database_failure_rejects_rather_than_admits(db, monkeypatch):
    """If the binding can't be read, the safe answer is 'no'. An exception that
    fell through to a truthy default would open the gate on a bad day."""
    monkeypatch.setattr(telegram_link, "_read_pref", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("db down")))
    telegram_link._invalidate()
    assert telegram_link.bound_chat_id(db) is None
    assert tg._locked("555", db) is False


def test_unpairing_closes_the_gate_again(db):
    """Unbind must return to answering nobody — not to answering everybody, and
    not to a stale cached binding."""
    _pair(db, "555")
    assert tg._locked("555", db) is True
    telegram_link.unpair(db)
    assert telegram_link.bound_chat_id(db) is None
    assert tg._locked("555", db) is False


# ------------------------------------------------------------ ENV WINS

def test_the_environment_variable_always_wins(db, monkeypatch):
    """An operator-set gate must not be alterable from inside the app."""
    stamp_meta(db, telegram_link.BOUND_KEY, "555")
    db.commit()
    telegram_link._invalidate()
    s = Settings(telegram_bot_token="t", telegram_chat_id="111")
    monkeypatch.setattr("app.config.get_settings", lambda: s)
    monkeypatch.setattr(tg, "get_settings", lambda: s)

    assert telegram_link.bound_chat_id(db) == "111"
    assert tg._locked("111", db) is True
    assert tg._locked("555", db) is False       # the stored value is ignored entirely


def test_pairing_is_refused_when_the_environment_configures_the_gate(db, monkeypatch):
    s = Settings(telegram_bot_token="t", telegram_chat_id="111")
    monkeypatch.setattr("app.config.get_settings", lambda: s)
    with pytest.raises(ValueError, match="always wins"):
        telegram_link.start_pairing(db)
    # And the back door is shut too: a code can't be redeemed against an env gate.
    assert telegram_link.try_pair(db, "555", "12345678") is False


def test_the_configured_case_never_touches_the_database(db, monkeypatch):
    """The gate runs on every inbound message. With the env var set it must resolve
    without a query, so a stranger spamming the bot can't keep a scale-to-zero
    database awake."""
    s = Settings(telegram_bot_token="t", telegram_chat_id="111")
    monkeypatch.setattr("app.config.get_settings", lambda: s)
    monkeypatch.setattr(telegram_link, "_read_pref",
                        lambda *a, **k: pytest.fail("gate hit the database with env set"))
    assert telegram_link.bound_chat_id(db) == "111"


# ------------------------------------------------------------ THE CODE

def test_only_the_right_code_binds(db):
    telegram_link.start_pairing(db)
    assert telegram_link.try_pair(db, "555", "00000000") is False
    assert telegram_link.bound_chat_id(db) is None


def test_a_code_is_single_use_even_when_wrong(db):
    """A wrong guess burns the window. Otherwise one armed code gives an attacker
    unlimited attempts at 8 digits."""
    code = telegram_link.start_pairing(db)["code"]
    assert telegram_link.try_pair(db, "555", "00000000") is False
    assert telegram_link.try_pair(db, "555", code) is False   # correct, but burned
    assert telegram_link.bound_chat_id(db) is None


def test_a_used_code_cannot_bind_a_second_chat(db):
    code = _pair(db, "555")
    assert telegram_link.try_pair(db, "666", code) is False
    assert telegram_link.bound_chat_id(db) == "555"


def test_an_expired_code_does_not_bind(db):
    code = telegram_link.start_pairing(db)["code"]
    stamp_meta(db, telegram_link.CODE_KEY,
               f"{code}:{(utcnow() - timedelta(minutes=1)).isoformat()}")
    db.commit()
    assert telegram_link.try_pair(db, "555", code) is False
    assert telegram_link.bound_chat_id(db) is None


def test_a_second_chat_cannot_pair_over_an_existing_binding(db):
    """One chat, ever. Rebinding is an explicit unpair, so a forgotten armed code
    can never quietly move the coach to someone else's phone."""
    _pair(db, "555")
    with pytest.raises(ValueError, match="Already paired"):
        telegram_link.start_pairing(db)
    assert telegram_link.try_pair(db, "666", "12345678") is False
    assert telegram_link.bound_chat_id(db) == "555"


def test_the_code_is_not_trivially_guessable(db):
    """8 digits from `secrets`, so a single armed window is 1 in 100 million.
    Also checks it isn't accidentally sequential or constant across arms."""
    seen = set()
    for _ in range(5):
        telegram_link.unpair(db)
        code = telegram_link.start_pairing(db)["code"]
        assert re.fullmatch(r"\d{8}", code)
        seen.add(code)
    assert len(seen) == 5


def test_garbage_never_reaches_the_code_check(db):
    """Non-numeric or wrong-length input is rejected before the comparison, so an
    armed code isn't burned by ordinary chatter from a stranger."""
    code = telegram_link.start_pairing(db)["code"]
    for junk in ("hello", "", "   ", "1234567", "123456789", "1234-5678"):
        assert telegram_link.try_pair(db, "555", junk) is False
    assert telegram_link.try_pair(db, "555", code) is True   # not burned by the junk


# ------------------------------------------------------------ AFTER PAIRING

def test_pairing_binds_the_gate_for_that_chat_only(db):
    _pair(db, "555")
    assert tg._locked("555", db) is True
    for other in ("556", "5550", "55", "666"):
        assert tg._locked(other, db) is False


def test_outbound_messages_go_to_the_paired_chat(db, monkeypatch):
    """Binding is useless if the briefs still have nowhere to go."""
    _pair(db, "555")
    monkeypatch.setattr(tg, "SessionLocal", None, raising=False)
    import app.db as appdb

    monkeypatch.setattr(appdb, "SessionLocal", lambda: db)
    assert tg._default_chat() == "555"


# ------------------------------------------------------------ SECRECY

def test_the_binding_and_code_are_internal_preferences(db):
    """Both are machine state; the code is a live credential. Neither may reach the
    web context panel or an LLM prompt."""
    assert _is_internal_pref(telegram_link.BOUND_KEY)
    assert _is_internal_pref(telegram_link.CODE_KEY)
    _pair(db, "555")
    keys = {p["key"] for p in snapshot(db)["preferences"]}
    assert telegram_link.BOUND_KEY not in keys
    assert telegram_link.CODE_KEY not in keys
