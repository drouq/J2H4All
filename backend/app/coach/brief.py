"""Morning brief: a short, early-local-morning Telegram note — today's
session, how recovery looks, anything worth knowing. Informational, not a proposal;
stays in-app/Telegram (never the calendar). Sonnet tier."""

import json
import logging
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession

from ..llm import LLMNotConfigured, call_text
from ..models import Session
from . import doctrine, signals

logger = logging.getLogger(__name__)


def system_prompt(db, today=None) -> str:
    return (
        "You are the coach in J2H4All. Write the athlete's morning brief: 3-5 short lines, warm but concise, no "
        "fluff. Cover today's planned session (what and why), how their recovery looks (Garmin Training "
        "Readiness score/level if present, plus HRV/resting-HR/sleep trend), and at most one actionable "
        "nudge (fueling, pacing, or 'ease off if X'). If recovery is down, say so plainly.\n\n"
        + doctrine.compact_doctrine(db, today)
        + "\n\nWhen it's natural, frame the session's 'why' in doctrine terms — what today builds toward "
        "THIS athlete's race, using the demands named in the doctrine above rather than generic "
        "fitness language. Weave in `recovery_deep` only when it's telling: elevated waking respiration or a "
        "skin-temp deviation ≥ ~1°C = possible incoming illness (say so plainly); restless_moments contextualize "
        "their sleep score per the doctrine's sleep rule — high restlessness vs THEIR OWN baseline, not vs zero. "
        "Use the `data_freshness` block for honesty about data age: STATE the age of any recovery reading you "
        "cite when it isn't from today (e.g. 'HRV from 2 days ago'), and never present an old number as this "
        "morning's. Use `recent_lifestyle` (their end-of-day logs: alcohol, illness, poor/restless sleep, "
        "stress, travel) to ATTRIBUTE a rough overnight reading to a cause when one fits — 'HRV's down, but "
        "you flagged a late night and a couple of beers, so I'd read that as lifestyle, not lost fitness' — "
        "rather than treating every dip as training fatigue. A logged illness flag with a real marker move "
        "(skin-temp/respiration/RHR) is worth taking seriously. "
        "If `overnight_recovery_is_current` is false, the athlete hasn't synced their watch since last "
        "night — OPEN with a one-line nudge to sync it so you can give a proper recovery read, and treat any "
        "recovery figures as provisional rather than deciding the day off them. When the data is current, brief "
        "normally without harping on freshness. You do NOT know when their watch last uploaded to Garmin — NEVER "
        "state or estimate a watch-sync time. (`last_backend_pull_local` is when J2H4All pulled from Garmin — our "
        "own cron — not a watch upload; don't retell it as one.) Base the nudge on "
        "`overnight_recovery_is_current` alone, without naming a time. Plain text for Telegram (no markdown headers)."
    )


def build_brief(db: DbSession, today: date | None = None) -> str:
    if today is None:
        from .schedule import local_today
        today = local_today(db)
    todays = db.scalars(
        select(Session).where(Session.status == "planned", Session.date == today)
    ).all()
    from ..plan.summary import garmin_summary
    g = garmin_summary(db, today)
    facts = {
        "today": today.isoformat(),
        "todays_sessions": [
            {"type": s.type, "title": s.title, "purpose": s.purpose,
             "target_zone": s.target_zone, "target_pace": s.target_pace,
             "distance_km": s.distance_km, "duration_min": s.duration_min,
             "fueling_note": s.fueling_note, "structure": s.structure}
            for s in todays
        ],
        "recovery": signals.recovery_baseline(db, today),
        "recovery_14d": g["recovery_14d"],  # carries avg_sleep_h — the prompt asks for a sleep trend
        "recovery_deep": signals.deep_recovery(db, today),
        "data_freshness": signals.data_freshness(db, today),
        "fitness_markers": signals.latest_markers(db),
        "recent_4wk_avg_km": g["recent_4wk_avg_km"],
        "vo2max": g["vo2max_latest"],
        "recent_checkins": signals.recent_checkins(db, today, days=3),
        "recent_lifestyle": signals.recent_lifestyle(db, today, days=3),
    }
    return call_text(
        task="morning_brief", system=system_prompt(db, today),
        content="Athlete state (JSON). Write today's brief.\n\n" + json.dumps(facts, default=str),
        max_tokens=700,
    )


def send_brief(db: DbSession, today: date | None = None) -> str | None:
    """Build + send the morning brief. Returns the text sent, or None if unavailable.

    A missed-run notice rides on the end of it (`coach/missed.py`) — the brief is the
    beat the coach chose for it, because by morning the previous day is definitively
    over and any late-evening run has had all night to sync. It's appended as fixed
    text rather than fed to the brief prompt so it can't be reworded into implying a
    cause, and it sends even when the brief itself is unavailable."""
    from ..telegram import send_message_sync
    from . import missed
    from .adapt import refresh_from_garmin
    refresh_from_garmin(db)  # freshest data before briefing (also covers on-demand /brief)
    if today is None:
        from .schedule import local_today
        today = local_today(db)
    try:
        text = build_brief(db, today)
    except LLMNotConfigured:
        logger.info("Morning brief skipped: LLM not configured")
        text = None
    pending = missed.pending_notice(db, today)
    parts = [p for p in (text, pending[0] if pending else None) if p]
    if not parts:
        return None
    out = "\n\n".join(parts)
    send_message_sync(out)
    if pending:
        # Only after a successful send: a notice that never landed must not burn the
        # one chance each session gets to be raised.
        missed.mark_raised(db, pending[1])
    return out
