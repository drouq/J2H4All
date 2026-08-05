"""Orchestration glue for the adaptation loop: runs after each sync and
from the scheduled beats, sending Telegram cards where a proposal results. Every
change is proposed, never applied silently."""

import logging
from datetime import date

from sqlalchemy.orm import Session as DbSession

logger = logging.getLogger(__name__)


def refresh_from_garmin(db: DbSession) -> None:
    """Best-effort incremental Garmin sync before a scheduled beat, so the coach reads
    the freshest cloud data regardless of the daily cron's clock. A sync
    failure must never block the beat — the freshness signals flag any staleness that
    remains, and the beat proceeds on existing data."""
    try:
        from ..garmin.sync import run_sync
        run_sync("incremental")
        db.expire_all()  # let the beat's session see the freshly-committed rows
    except Exception:
        logger.warning("Pre-beat Garmin sync failed; proceeding on existing data", exc_info=True)


def post_sync_coaching(db: DbSession) -> None:
    """After a sync: read completed runs, then raise a red flag if something's acute."""
    from . import postrun, redflag
    from .schedule import local_today
    from ..telegram import send_proposal_card_sync

    today = local_today(db)                     # user-local day, not server-UTC

    # Each stage is guarded on its own: they're independent coaching surfaces, and a
    # failure in one must not silently cost the others. Without this, an error in the
    # read or the question skipped the RED FLAG for that cycle — the one stage whose
    # whole job is not to miss something acute. (The caller's blanket try/except keeps
    # the sync itself safe; it can't tell which stage died.)
    def _stage(name: str, fn):
        try:
            return fn()
        except Exception:
            logger.exception("Post-sync stage %s failed (non-fatal)", name)
            return None

    _stage("post_run_reads", lambda: postrun.run_reads(db, today))   # planned-vs-actual
    # A >20% miss gets a QUESTION before any adaptation — the data can't say why a
    # session ended early, and adapting off a guessed cause is worse than waiting.
    _stage("deviation_question", lambda: postrun.ask_about_deviations(db, today))
    result = _stage("red_flag", lambda: redflag.check_and_propose(db, today))
    if result:
        proposal, summary = result
        send_proposal_card_sync(proposal.id, "🚩 Heads up — " + summary)
        logger.info("Sent red-flag proposal card %s", proposal.id)


def run_weekly_review(db: DbSession, today: date | None = None) -> bool:
    from . import weekly
    from ..telegram import send_proposal_card_sync

    refresh_from_garmin(db)  # review on the freshest data; it also weighs data age
    result = weekly.run_review(db, today)
    if not result:
        return False
    proposal, summary = result
    from .. import monitor  # weekly heartbeat rides the review card (silence = healthy)
    card = "🗓️ Weekly review\n\n" + summary + "\n\n" + monitor.health_summary(db)
    send_proposal_card_sync(proposal.id, card)
    logger.info("Sent weekly-review card %s", proposal.id)
    return True


def send_morning_brief(db: DbSession, today: date | None = None) -> bool:
    from .brief import send_brief
    return send_brief(db, today) is not None


def send_daily_debrief(db: DbSession) -> bool:
    """The 21:00 combined debrief (feel + life factors) — replaces the old 19:00
    check-in and 22:00 lifestyle beats."""
    from ..telegram import send_card_sync
    from . import debrief
    from .checkin import set_awaiting
    refresh_from_garmin(db)  # capture the day's activity before the debrief
    text, keyboard = debrief.prompt_card(db)
    send_card_sync(text, keyboard)
    set_awaiting(db, debrief.AWAITING_KEY)  # next free-text reply is the debrief
    return True
