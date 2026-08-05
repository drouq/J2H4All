import asyncio
import html as _html
import logging
import re
from datetime import UTC

import httpx
from fastapi import APIRouter, HTTPException, Request

from .config import get_settings

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/telegram", tags=["telegram"])

# The coach writes GitHub-flavoured Markdown (**bold**, *italic*, `code`, - lists).
# Telegram doesn't render that natively, so convert to its HTML parse mode.
_MD_HEADER = re.compile(r"(?m)^#{1,6}\s*(.+?)\s*$")
_MD_BOLD = re.compile(r"\*\*(.+?)\*\*")
_MD_CODE = re.compile(r"`([^`\n]+?)`")
_MD_ITALIC_STAR = re.compile(r"(?<![\*\w])\*(?!\s)([^*\n]+?)(?<!\s)\*(?![\*\w])")
_MD_ITALIC_US = re.compile(r"(?<!\w)_(?!\s)([^_\n]+?)(?<!\s)_(?!\w)")
_MD_BULLET = re.compile(r"(?m)^(\s*)[-*]\s+")


def format_html(text: str) -> str:
    """Markdown -> Telegram-safe HTML (for parse_mode='HTML').

    Escaping &<> FIRST means arbitrary coach text ('run HR <145') is always safe,
    and unbalanced markdown (a lone '**') just stays literal — the only tags in the
    output are the balanced <b>/<i>/<code> pairs we insert, so it can't produce
    invalid HTML that Telegram would 400 on.
    """
    if not text:
        return text
    t = _html.escape(text, quote=False)          # & < >
    t = _MD_HEADER.sub(r"<b>\1</b>", t)          # "# Heading" -> bold line
    t = _MD_BOLD.sub(r"<b>\1</b>", t)            # **x** -> bold
    t = _MD_CODE.sub(r"<code>\1</code>", t)      # `x` -> code
    t = _MD_ITALIC_STAR.sub(r"<i>\1</i>", t)     # *x* -> italic
    t = _MD_ITALIC_US.sub(r"<i>\1</i>", t)       # _x_ -> italic
    t = _MD_BULLET.sub(r"\1• ", t)          # "- x" / "* x" -> "• x"
    return t


def _api_url(method: str) -> str | None:
    settings = get_settings()
    if not settings.telegram_bot_token:
        return None
    return f"https://api.telegram.org/bot{settings.telegram_bot_token}/{method}"


async def send_message(chat_id: str, text: str) -> None:
    url = _api_url("sendMessage")
    if not url:
        logger.warning("TELEGRAM_BOT_TOKEN not set; dropping outbound message")
        return
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                url, json={"chat_id": chat_id, "text": format_html(text), "parse_mode": "HTML"}
            )
            if resp.status_code != 200:
                logger.error("Telegram sendMessage failed: %s", resp.text)
                # A formatting error must never drop the message — resend as plain text.
                await client.post(url, json={"chat_id": chat_id, "text": text})
    except httpx.HTTPError:
        logger.exception("Telegram sendMessage errored")


def send_message_sync(text: str, chat_id: str | None = None) -> None:
    """Blocking send for non-async contexts (sync engine, cron jobs). Splits
    messages over Telegram's 4096-char limit into chunks."""
    chat_id = chat_id or _default_chat()
    url = _api_url("sendMessage")
    if not url or not chat_id:
        logger.warning("Telegram not configured; dropping outbound message")
        return
    for chunk in _chunk(text, 4000):
        try:
            resp = httpx.post(
                url, json={"chat_id": chat_id, "text": format_html(chunk), "parse_mode": "HTML"}, timeout=15
            )
            if resp.status_code != 200:
                logger.error("Telegram sendMessage failed: %s", resp.text)
                # A formatting error must never drop the message — resend as plain text.
                httpx.post(url, json={"chat_id": chat_id, "text": chunk}, timeout=15)
        except httpx.HTTPError:
            logger.exception("Telegram sendMessage errored")


def _chunk(text: str, size: int) -> list[str]:
    if len(text) <= size:
        return [text]
    parts, cur = [], ""
    for para in text.split("\n"):
        if len(cur) + len(para) + 1 > size:
            if cur:
                parts.append(cur)
            cur = para[:size] if len(para) > size else para
        else:
            cur = f"{cur}\n{para}" if cur else para
    if cur:
        parts.append(cur)
    return parts


def _post_sync(method: str, payload: dict) -> int | None:
    """POST and return the HTTP status (None on transport error) so callers can
    fall back on a definite rejection without duplicating on a network blip."""
    url = _api_url(method)
    if not url:
        logger.warning("Telegram not configured; dropping %s", method)
        return None
    try:
        resp = httpx.post(url, json=payload, timeout=15)
        if resp.status_code != 200:
            logger.error("Telegram %s failed: %s", method, resp.text)
        return resp.status_code
    except httpx.HTTPError:
        logger.exception("Telegram %s errored", method)
        return None


def _send_html_or_plain(method: str, payload: dict, raw_text: str) -> None:
    """Send `payload` (text already HTML) with parse_mode=HTML; on a definite
    rejection (a non-200 response, i.e. a formatting error), resend as plain text
    so the message always lands. A transport error is not retried (no duplicate)."""
    code = _post_sync(method, {**payload, "parse_mode": "HTML"})
    if code is not None and code != 200:
        _post_sync(method, {**payload, "text": raw_text})


def send_card_sync(text: str, keyboard: list[list[dict]], chat_id: str | None = None) -> None:
    """Send a message with an inline keyboard (approval cards, check-in prompt)."""
    chat_id = chat_id or _default_chat()
    if not chat_id:
        return
    _send_html_or_plain(
        "sendMessage",
        {"chat_id": chat_id, "text": format_html(text),
         "reply_markup": {"inline_keyboard": keyboard}},
        text,
    )


def send_proposal_card_sync(proposal_id: int, summary: str, chat_id: str | None = None) -> None:
    """Approval card with inline Approve / Edit / Reject buttons."""
    keyboard = [[
        {"text": "✅ Approve", "callback_data": f"apr:{proposal_id}"},
        {"text": "✏️ Edit", "callback_data": f"edt:{proposal_id}"},
        {"text": "❌ Reject", "callback_data": f"rej:{proposal_id}"},
    ]]
    send_card_sync(summary, keyboard, chat_id)


def answer_callback(callback_id: str, text: str = "") -> None:
    _post_sync("answerCallbackQuery", {"callback_query_id": callback_id, "text": text})


def edit_message_text(chat_id: str, message_id: int, text: str) -> None:
    """Replace a card's text and drop its buttons once it's been acted on."""
    _send_html_or_plain(
        "editMessageText",
        {"chat_id": chat_id, "message_id": message_id, "text": format_html(text)},
        text,
    )


def edit_message_card(chat_id: str, message_id: int, text: str,
                      keyboard: list[list[dict]]) -> None:
    """Replace a card's text but KEEP its buttons — for a card that takes several
    taps (the debrief's feel + life flags) rather than one terminal action."""
    _send_html_or_plain(
        "editMessageText",
        {"chat_id": chat_id, "message_id": message_id, "text": format_html(text),
         "reply_markup": {"inline_keyboard": keyboard}},
        text,
    )


async def _sync_and_report(chat_id: str) -> None:
    from .garmin.sync import run_sync  # local import to avoid a module cycle

    try:
        run = await asyncio.to_thread(run_sync, "incremental")
    except RuntimeError as exc:  # already running
        await send_message(chat_id, str(exc))
        return
    if run.status == "skipped":
        await send_message(
            chat_id,
            "Garmin sync runs from the home machine now (Render can't reach Garmin's "
            "servers). Data refreshes after the home sync — check freshness with /brief.",
        )
        return
    if run.status == "success":
        await send_message(chat_id, f"Sync done. {run.stats}")
    else:
        first_line = (run.detail or "unknown error").splitlines()[0][:200]
        await send_message(chat_id, f"Sync FAILED: {first_line}")


def _default_chat() -> str | None:
    """Where an outbound message goes when no chat is named. Env first; falls back
    to the paired chat so a paired install actually receives its briefs."""
    env = get_settings().telegram_chat_id
    if env:
        return str(env)
    from . import telegram_link
    from .db import SessionLocal

    db = SessionLocal()
    try:
        return telegram_link.bound_chat_id(db)
    except Exception:  # noqa: BLE001 — no chat is better than a crashed beat
        return None
    finally:
        db.close()


def _locked(chat_id: str, db=None) -> bool:
    """Everything is locked to the single bound chat ID. See telegram_link.

    The environment variable is checked FIRST and without touching the database,
    so the common configured case costs no query even for a rejected sender. Only
    when it is unset do we consult the paired value.

    An unbound bot returns False for EVERY sender. Unbound means nobody, never
    everybody - this is the fail-closed direction and it is tested directly."""
    tg = get_settings().telegram_chat_id
    if tg:
        return chat_id == str(tg)
    if db is None:
        return False
    from . import telegram_link

    bound = telegram_link.bound_chat_id(db)
    return bool(bound) and chat_id == str(bound)


def _gate(chat_id: str) -> bool:
    """The gate, resolved synchronously. Opens a database session ONLY when the
    environment variable is unset — the configured case costs no query, so a
    stranger spamming the bot can't be used to keep a scale-to-zero database
    awake. (telegram_link also caches the paired value for a minute.)"""
    if get_settings().telegram_chat_id:
        return _locked(chat_id)
    from .db import SessionLocal

    db = SessionLocal()
    try:
        return _locked(chat_id, db)
    except Exception:  # noqa: BLE001 — a database blip must reject, never admit
        logger.exception("Gate lookup failed; rejecting")
        return False
    finally:
        db.close()


def _try_pairing(chat_id: str, text: str) -> bool:
    """Sync helper (opens its own session) so the async handler can offload it."""
    from . import telegram_link
    from .db import SessionLocal

    db = SessionLocal()
    try:
        return telegram_link.try_pair(db, chat_id, text)
    except Exception:  # noqa: BLE001 - a pairing failure must never open the gate
        logger.exception("Pairing attempt failed")
        return False
    finally:
        db.close()


async def handle_update(update: dict) -> None:
    """Shared by the webhook (deployed) and the polling runner (local dev)."""
    if update.get("callback_query"):
        await asyncio.to_thread(_handle_callback, update["callback_query"])
        return

    message = update.get("message") or update.get("edited_message")
    if not message:
        return
    chat_id = str(message.get("chat", {}).get("id", ""))
    if not await asyncio.to_thread(_gate, chat_id):
        # Not the bound chat. Before ignoring, give the message one chance to be
        # a pairing code — that is the ONLY way an unbound bot can ever acquire a
        # chat, and it needs a code armed from the authenticated web app.
        if await asyncio.to_thread(_try_pairing, chat_id, (message.get("text") or "")):
            await send_message(
                chat_id,
                "Paired. This chat is now your coach — nobody else can reach it.\n\n" + COMMANDS,
            )
            return
        # Silence, deliberately: replying would tell a prober that the bot exists,
        # that a pairing is in progress, or that their guess was close.
        logger.info("Ignoring message from non-allowlisted chat %s", chat_id)
        return

    text = (message.get("text") or "").strip()
    if text.startswith("/start"):
        await send_message(chat_id, "J2H4All — Journey to Hundred, for All, your running coach.\n\n" + COMMANDS)
    elif text.startswith("/help"):
        await send_message(chat_id, COMMANDS)
    elif text.startswith("/sync"):
        await send_message(chat_id, "On it — syncing Garmin now...")
        asyncio.create_task(_sync_and_report(chat_id))
    elif text.startswith("/status"):
        await send_message(chat_id, await asyncio.to_thread(_status_text))
    elif text.startswith("/checkin") or text.startswith("/log") or text.startswith("/debrief"):
        await asyncio.to_thread(_send_debrief_prompt)  # /checkin & /log alias the merged debrief
    elif text.startswith("/brief"):
        await asyncio.to_thread(_send_brief)
    elif text.startswith("/push"):
        await send_message(chat_id, "Pushing the current plan to your calendar and watch...")
        await asyncio.to_thread(_push_plan)
    else:
        await asyncio.to_thread(_handle_free_text, text)


COMMANDS = (
    "Commands:\n"
    "/sync — pull Garmin data now\n"
    "/status — sync health\n"
    "/debrief — end-of-day: how you feel + life factors (alcohol, sleep, stress)\n"
    "/brief — today's brief\n"
    "/push — re-sync the current plan to Google Calendar + watch\n"
    "/help — this list\n\n"
    "Or just ask me anything — fueling, a recent run, pacing, taper, race strategy — "
    "and I'll answer as your coach. Ask me to change your plan (move/lengthen/swap a "
    "session) and I'll send an Approve/Edit/Reject card; approving pushes it to your "
    "calendar and watch. Durable facts you mention (timezone, bloods, injuries, "
    "treadmill weeks) I'll offer to save to your profile."
)


def _push_plan() -> None:
    """Re-sync the current APPROVED plan to Google Calendar + Garmin (the Telegram
    twin of the web 'Push plan' button). This pushes what's already in the store —
    it does not change the plan, so no approval gate applies."""
    from .calendar.sync import safe_reconcile
    from .coach.proposal_actions import _calendar_line, _garmin_line
    from .db import SessionLocal
    db = SessionLocal()
    try:
        cal = safe_reconcile(db)
        try:
            from .garmin import workouts as garmin_workouts
            gw = garmin_workouts.reconcile(db)
        except Exception as exc:  # noqa: BLE001 — the calendar result still stands
            logger.exception("Garmin workout push (/push) failed")
            gw = {"error": str(exc)}
        applied = {"calendar": cal, "garmin_workouts": gw}
        lines = _calendar_line(applied) or "\n(No calendar changes.)"
        send_message_sync("✅ Plan pushed." + lines + _garmin_line(applied))
    finally:
        db.close()


def _status_text() -> str:
    from .db import SessionLocal
    from .garmin.sync import sync_status_summary
    db = SessionLocal()
    try:
        s = sync_status_summary(db)
    finally:
        db.close()
    if not s["last_run"]:
        return "No syncs yet. Send /sync to pull Garmin data."
    staleness = (f"{s['staleness_hours']}h since last successful sync"
                 if s["staleness_hours"] is not None else "never synced successfully")
    return f"Last sync: {s['last_run']['status']} ({s['last_run']['kind']}). {staleness}."


def send_typing(chat_id: str | None = None) -> None:
    _post_sync("sendChatAction", {"chat_id": chat_id or _default_chat(), "action": "typing"})


def _send_debrief_prompt() -> None:
    from .coach import debrief
    from .coach.checkin import set_awaiting
    from .db import SessionLocal
    db = SessionLocal()
    try:
        text, keyboard = debrief.prompt_card(db)
        send_card_sync(text, keyboard)
        set_awaiting(db, debrief.AWAITING_KEY)  # next free-text reply is the debrief
    finally:
        db.close()


def _send_brief() -> None:
    from .coach.brief import send_brief
    from .db import SessionLocal
    db = SessionLocal()
    try:
        if send_brief(db) is None:
            send_message_sync("No brief available (coaching model not configured).")
    finally:
        db.close()


def _handle_free_text(text: str) -> None:
    """Free text is routed in priority order:
      1. a reply to a pending proposal Edit   -> redraft it
      2. a reply to the off-plan question      -> store it as the session's reason,
         then answer it as a normal coaching turn (the coach now knows WHY)
      3. a note within the check-in window     -> capture as the check-in note
         (unless it's plainly a question — then answer it and keep the flag armed
         so a later note still lands)
      4. otherwise                             -> a coaching question
    """
    from .coach import chat, debrief, postrun, revise
    from .coach.checkin import awaiting_active, clear_awaiting, looks_like_question
    from .db import SessionLocal
    from .llm import LLMNotConfigured
    from .models import Proposal
    db = SessionLocal()
    try:
        pending = revise.pop_pending_edit(db)  # None if no edit pending or the tap is stale
        if pending is not None:
            p = db.get(Proposal, pending)
            if p is not None and p.status == "pending":
                result = revise.revise_proposal(db, pending, text)
                if result is None:
                    send_message_sync("Couldn't revise that one right now — tell me and I'll "
                                      "draft a fresh change.")
                else:
                    proposal, summary = result
                    send_proposal_card_sync(proposal.id, "✏️ Updated proposal:\n\n" + summary)
                return
            # The Edit tap's proposal was resolved after the tap — don't swallow this
            # message; fall through to handle it as a debrief / coaching message.

        # A reply to the off-plan question ("why did Saturday come in short?") is their
        # REASON — store it on the result, then let the coach answer it normally, with
        # the stated cause now in the record. This is the whole point of asking: the
        # adaptation that follows is built on what happened, not on an inference.
        #
        # It OUTRANKS the debrief capture and skips it. The debrief's own pre-beat sync
        # is what triggers this question, so at 22:00 both flags are armed seconds apart
        # — and one message can't be both "why Saturday was short" and tonight's feel
        # log. Answering the specific question wins; the debrief prompt stays armed for
        # their next line, and its card is still tappable either way.
        captured_deviation = False
        if not looks_like_question(text):
            rid = postrun.pending_ask(db)
            if rid is not None:
                postrun.record_reason(db, rid, text)
                captured_deviation = True
                logger.info("Captured off-plan reason for result %s", rid)

        # A reply within the debrief window (22:00 prompt or a feel tap) is captured as
        # the combined debrief — parsed into feel scores AND lifestyle flags. A question
        # in the window still routes to the coach (looks_like_question).
        if (not captured_deviation and awaiting_active(db, debrief.AWAITING_KEY)
                and not looks_like_question(text)):
            clear_awaiting(db, debrief.AWAITING_KEY)
            debrief.record_reply(db, text)
            send_message_sync("Logged — feel and the details. Thanks; it sharpens tomorrow's read.")
            return

        # Anything else is a coaching question. The coach may also raise a plan-change
        # proposal (move/swap/lengthen a session) — surfaced as an approval card, so
        # a change discussed here reaches the plan/calendar/watch only on approval.
        send_typing()
        try:
            answer, proposal = chat.ask_with_proposal(db, text, surface="telegram")
        except LLMNotConfigured:
            send_message_sync("The coaching model isn't configured, so I can't answer that yet.")
            return
        send_message_sync(answer)
        if proposal is not None:
            send_proposal_card_sync(proposal.id, "📝 Proposed plan change:\n\n" + proposal.summary)
        # Chat is also context capture (the Eponge pattern): offer to
        # save any durable facts the message carried — confirm-before-write.
        _offer_context_capture(db, text)
    except Exception:
        logger.exception("Free-text handling failed")
        send_message_sync("Something went wrong answering that — try again in a moment.")
    finally:
        db.close()


_PENDING_CTX_KEY = "pending_context_items"  # filtered from prompts (internal machine state)


def _offer_context_capture(db, text: str) -> None:
    """Telegram arm of the Eponge flow: extract durable facts ('I'm in Tokyo this
    week', 'ferritin came back at 30') and offer a Save/Skip card. Nothing is
    written until the user taps Save — the confirm step, as inline buttons.
    Best-effort: never disturb the coaching answer that was already sent."""
    import json as _json
    from datetime import datetime

    from sqlalchemy import select

    from .coach.schedule import local_today
    from .context.extract import extract_items
    from .context.store import get_or_create_state
    from .models import Preference

    try:
        state = get_or_create_state(db)
        items = extract_items(text, local_today(db), state.timezone or "UTC")
    except Exception:
        logger.debug("Telegram context extraction skipped", exc_info=True)
        return
    if not items:
        return
    try:
        now = datetime.now(UTC)
        pref = db.scalar(select(Preference).where(Preference.key == _PENDING_CTX_KEY))
        if pref is None:
            db.add(Preference(key=_PENDING_CTX_KEY, value=_json.dumps(items), updated_at=now))
        else:
            pref.value = _json.dumps(items)  # one pending capture at a time
            pref.updated_at = now
        db.commit()
        lines = "\n".join("• " + (i.get("summary") or i.get("kind", "item")) for i in items)
        send_card_sync(
            f"📌 That message had {len(items)} thing(s) worth remembering:\n{lines}\n\nSave to your profile?",
            [[{"text": "✅ Save", "callback_data": "ctx:yes"},
              {"text": "✕ Skip", "callback_data": "ctx:no"}]],
        )
    except Exception:
        logger.exception("Context-capture offer failed")


def _handle_context_callback(db, arg: str, chat_id: str, message_id, cb_id) -> None:
    import json as _json

    from sqlalchemy import select

    from .context.store import apply_items
    from .models import Preference

    pref = db.scalar(select(Preference).where(Preference.key == _PENDING_CTX_KEY))
    if pref is None or not pref.value:
        answer_callback(cb_id, "Nothing pending.")
        edit_message_text(chat_id, message_id, "That capture already expired.")
        return
    items = _json.loads(pref.value)
    db.delete(pref)
    db.commit()
    if arg != "yes":
        answer_callback(cb_id, "Skipped")
        edit_message_text(chat_id, message_id, "Skipped — nothing saved.")
        return
    applied = apply_items(db, items, source="telegram")
    answer_callback(cb_id, "Saved ✓")
    edit_message_text(chat_id, message_id, "📌 Saved:\n" + "\n".join("• " + a for a in applied))


def _handle_callback(cb: dict) -> None:
    """Inline-button taps: approve/edit/reject a proposal, or a quick check-in."""
    chat_id = str((cb.get("message") or {}).get("chat", {}).get("id", ""))
    if not _gate(chat_id):
        logger.info("Ignoring callback from non-allowlisted chat %s", chat_id)
        return
    cb_id = cb.get("id")
    data = cb.get("data") or ""
    message_id = (cb.get("message") or {}).get("message_id")

    from .db import SessionLocal
    db = SessionLocal()
    try:
        action, _, arg = data.partition(":")
        if action in ("ci", "lf"):  # feel / life-flag taps on the debrief card
            from .coach import debrief, lifestyle
            from .coach.checkin import QUICK, record_quick, set_awaiting
            from .coach.schedule import local_today
            if action == "ci":
                record_quick(db, arg)
                ack = f"{QUICK.get(arg, {}).get('label', arg)} ✓"
            else:
                tapped = lifestyle.record_tap(db, arg, local_today(db))
                if tapped is None:
                    answer_callback(cb_id, "Unknown option.")
                    return
                label, on = tapped
                ack = f"{label} ✓" if on else f"{label} — removed"
            set_awaiting(db, debrief.AWAITING_KEY)  # refresh window so a follow-up line still folds in
            answer_callback(cb_id, ack)
            # Buttons stay live: the card asks two separate questions and they may
            # want to tap several (or correct one) before typing anything.
            text, keyboard = debrief.render_card(db)
            edit_message_card(chat_id, message_id, text, keyboard)
            return

        if action in ("apr", "rej", "edt"):
            from .coach import proposal_actions as pa
            pa.handle(db, action, int(arg), chat_id, message_id, cb_id)
            return

        if action == "ctx":  # save/skip a context capture (Eponge confirm step)
            _handle_context_callback(db, arg, chat_id, message_id, cb_id)
            return
        answer_callback(cb_id)
    except Exception:
        logger.exception("Callback handling failed for data=%s", data)
        answer_callback(cb_id, "Something went wrong.")
    finally:
        db.close()


@router.post("/webhook")
async def webhook(request: Request):
    import secrets as _secrets

    settings = get_settings()
    # Telegram echoes the secret_token given to setWebhook in this header.
    # Prod refuses to boot without the secret (config.validate_production);
    # the conditional skip below can therefore only apply in development.
    if settings.telegram_webhook_secret:
        provided = request.headers.get("X-Telegram-Bot-Api-Secret-Token") or ""
        if not _secrets.compare_digest(provided, settings.telegram_webhook_secret):
            raise HTTPException(status_code=403, detail="Bad webhook secret")
    await handle_update(await request.json())
    return {"ok": True}
