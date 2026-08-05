"""Binding the bot to exactly one Telegram chat, and the pairing flow that does it.

This module owns one of the app's two HARD RULES: the bot answers exactly one
chat and silently ignores every other sender. Read this before changing anything
here.

WHY IT EXISTS. The chat id used to come only from `TELEGRAM_CHAT_ID`, which meant
a new self-hoster had to message their bot, curl `getUpdates`, and dig a numeric
id out of the JSON before the coach could reach them. That is a genuinely bad
first experience and it strands people.

WHAT CHANGED, PRECISELY. Only the SOURCE of the bound chat id. The gate itself is
exactly as strict as before:

  - The environment variable ALWAYS WINS. If `TELEGRAM_CHAT_ID` is set, this
    module's stored value is never consulted. An operator-set gate can't be
    altered from inside the app.
  - UNBOUND MEANS NOBODY. With no env var and nothing paired, `bound_chat_id`
    returns None and the gate rejects every sender including the pairing
    attempt's own chat until a code matches. It fails CLOSED. That is the single
    most important property here and it is tested directly.
  - Binding requires a code that only someone signed into the web app can
    generate. The web app is behind the other hard gate (one allowlisted Google
    account), so arming a pairing is already an authenticated action.
  - One chat, ever. Once bound, a second chat cannot pair over it; rebinding
    requires an explicit unbind from the web app.

THE RESIDUAL RISK, STATED PLAINLY. Between arming a code and it being used, a
stranger who has found the bot could bind themselves by guessing the code. That
is why the code is 8 digits from `secrets` (1 in 100 million), expires in 10
minutes, is single-use, and can only be armed while nothing is bound. A wrong
guess is answered with silence, so the bot cannot be used as an oracle to tell a
prober that a pairing is even in progress.

DATABASE COST. The gate runs on every inbound message, and this app's rule is
that nothing on a timer may open a database connection. A webhook is
event-driven rather than polled, so it is not covered by that rule — but a
stranger spamming the bot would still wake a scale-to-zero database on every
message. Hence: the env var is checked FIRST and needs no query at all, and the
stored value is cached in-process for `_CACHE_TTL_S`, so even sustained spam
costs at most one read a minute.
"""

from __future__ import annotations

import logging
import secrets
import time
from datetime import timedelta

from .util import as_utc, utcnow

logger = logging.getLogger(__name__)

# Both are INTERNAL preference keys (see context.store._INTERNAL_PREF_KEYS), so
# neither the web context panel nor any LLM prompt ever sees them.
BOUND_KEY = "telegram_bound_chat_id"
CODE_KEY = "telegram_pair_code"

CODE_TTL = timedelta(minutes=10)
CODE_DIGITS = 8

# How long a resolved binding is trusted without re-reading the database. Short
# enough that an unbind takes effect promptly, long enough that message spam
# can't be used to keep a scale-to-zero database awake.
_CACHE_TTL_S = 60
_cache: tuple[float, str | None] | None = None


def _read_pref(db, key: str) -> str | None:
    from sqlalchemy import select

    from .models import Preference

    pref = db.scalar(select(Preference).where(Preference.key == key))
    # An empty string means "cleared" (unpair writes "" rather than deleting the
    # row) and must read back as None. Returning "" would make `is_bound` true
    # forever after an unpair, so pairing could never be re-armed.
    return (pref.value or None) if pref else None


def _invalidate() -> None:
    global _cache
    _cache = None


def bound_chat_id(db=None) -> str | None:
    """The one chat this bot answers, or None if it answers nobody.

    Resolution order is deliberate: the environment variable first (and without
    touching the database at all), then the paired value. None is a valid,
    SAFE answer — callers must treat it as 'reject everyone', never as
    'allow anyone'."""
    from .config import get_settings

    env = get_settings().telegram_chat_id
    if env:
        return str(env)
    if db is None:
        return None

    global _cache
    now = time.monotonic()
    if _cache is not None and now - _cache[0] < _CACHE_TTL_S:
        return _cache[1]
    try:
        value = _read_pref(db, BOUND_KEY)
    except Exception:  # noqa: BLE001 — a database blip must not open the gate
        return _cache[1] if _cache else None
    _cache = (now, value)
    return value


def start_pairing(db) -> dict:
    """Arm a single-use pairing code. Called only from the authenticated web app.

    Refuses while a binding already exists: rebinding is an explicit unbind
    followed by a new pairing, so a forgotten armed code can never quietly move
    the coach to a different chat."""
    from .config import get_settings
    from .context.store import stamp_meta

    if get_settings().telegram_chat_id:
        raise ValueError(
            "TELEGRAM_CHAT_ID is set in the environment, which always wins. "
            "Unset it to pair from here, or leave it as is."
        )
    if bound_chat_id(db) is not None:
        raise ValueError("Already paired to a chat. Unpair first if you want to move it.")

    code = "".join(secrets.choice("0123456789") for _ in range(CODE_DIGITS))
    expires = utcnow() + CODE_TTL
    stamp_meta(db, CODE_KEY, f"{code}:{expires.isoformat()}")
    db.commit()
    return {"code": code, "expires_at": expires.isoformat(),
            "ttl_minutes": int(CODE_TTL.total_seconds() // 60)}


def _consume_code(db, offered: str) -> bool:
    """Check and burn the armed code. Single-use whether it matched or not, so a
    wrong guess costs the attacker the whole window rather than giving them
    unlimited attempts against one code."""
    from .context.store import stamp_meta

    raw = _read_pref(db, CODE_KEY)
    if not raw or ":" not in raw:
        return False
    code, _, expiry = raw.partition(":")
    stamp_meta(db, CODE_KEY, "")  # burn on ANY attempt
    db.commit()
    try:
        from datetime import datetime

        if as_utc(datetime.fromisoformat(expiry)) < utcnow():
            return False
    except (ValueError, TypeError):
        return False
    # compare_digest so a wrong guess can't be narrowed down by timing.
    return secrets.compare_digest(code, offered)


def try_pair(db, chat_id: str, text: str) -> bool:
    """Attempt to bind `chat_id` using `text` as the code. Returns True on bind.

    Called ONLY for senders the gate has already rejected. Returns False for
    everything that isn't an exact, unexpired, single-use match — and the caller
    stays silent on False, so this can't be probed."""
    from .config import get_settings
    from .context.store import stamp_meta

    if get_settings().telegram_chat_id:
        return False                      # env-configured gates are not pairable
    if bound_chat_id(db) is not None:
        return False                      # one chat, ever
    offered = (text or "").strip()
    if not offered.isdigit() or len(offered) != CODE_DIGITS:
        return False
    if not _consume_code(db, offered):
        return False
    stamp_meta(db, BOUND_KEY, str(chat_id))
    db.commit()
    _invalidate()
    logger.info("Telegram bot paired to chat %s via pairing code", chat_id)
    return True


def unpair(db) -> None:
    """Drop the binding. The bot then answers NOBODY until paired again — which
    is the safe direction to fail."""
    from .context.store import stamp_meta

    stamp_meta(db, BOUND_KEY, "")
    stamp_meta(db, CODE_KEY, "")
    db.commit()
    _invalidate()
