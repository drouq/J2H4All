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
        "fluff. Cover today's session (what and why), how their recovery looks (Garmin Training "
        "Readiness score/level if present, plus HRV/resting-HR/sleep trend), and at most one actionable "
        "nudge (fueling, pacing, or 'ease off if X'). If recovery is down, say so plainly.\n\n"
        "EACH of `todays_sessions` carries a `status`. When it is `done` or `partial` the athlete has "
        "ALREADY completed it — a scheduled workout is often run a day early off the watch — so "
        "ACKNOWLEDGE it (naming `actually_done_on` when it says they did it on another day) and NEVER "
        "announce it as today's plan or tell them to go and do it. If everything today is already "
        "done, say the day's work is in the bank and brief on recovery instead. Only `planned` is "
        "still ahead of them.\n\n"
        + doctrine.compact_doctrine(db, today)
        + "\n\nWhen it's natural, frame the session's 'why' in doctrine terms — what today builds toward "
        "THIS athlete's race, using the demands named in the doctrine above rather than generic "
        "fitness language. Weave in `recovery_deep` only when it's telling: elevated waking respiration or a "
        "skin-temp deviation ≥ ~1°C = possible incoming illness (say so plainly); restless_moments contextualize "
        "their sleep score per the doctrine's sleep rule — high restlessness vs THEIR OWN baseline, not vs zero. "
        "`sleep_nights` is their last week of bed→wake windows, per night: when last night's in_bed_h is well "
        "short of their own pattern, SAY SO and weigh it in the day's read — a genuinely short night is a "
        "recovery fact even when HRV/RHR look fine, and it is exactly what the sleep-score rule tells you the "
        "score can't show you. One short night shapes today; only a pattern reshapes the plan. "
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


def _session_facts(db: DbSession, s: Session, today: date) -> dict:
    """One of today's sessions, WITH whether it has already been done.

    The completion half matters because a scheduled workout selected on the watch can be
    run whenever it suits, and `link_results` matches by `workoutId` regardless of date —
    so today's session may already be complete before the brief fires. Without this the
    brief reads straight off the calendar square and announces a session as "today's plan"
    the morning after it was run, while the previous evening's debrief acknowledged it
    correctly: the debrief follows the run, and the brief followed the square. Running a
    session a day early is habitual for some athletes, so this bites routinely, not rarely.

    Uses the shared `completion.classify` rather than a local "has a result?" test, so the
    brief, the calendar glyph and the web Plan panel cannot drift apart."""
    from ..models import SessionResult
    from . import completion

    result = db.scalars(
        select(SessionResult).where(SessionResult.session_id == s.id).order_by(SessionResult.id)
    ).first()
    facts = {
        "type": s.type, "title": s.title, "purpose": s.purpose,
        "target_zone": s.target_zone, "target_pace": s.target_pace,
        "distance_km": s.distance_km, "duration_min": s.duration_min,
        "fueling_note": s.fueling_note, "structure": s.structure,
        "status": completion.classify(s, result, today),
    }
    if result is not None and result.completed:
        facts["actual"] = {
            "distance_km": result.actual_distance_km,
            "duration_min": result.actual_duration_min,
            "avg_hr": result.actual_avg_hr,
            "read": result.read_summary,
        }
        # When it was run early the DAY is the point — "you did this yesterday" rather than
        # a bare "done" that leaves them wondering which run it means.
        act_date = _activity_date(db, result)
        if act_date is not None and act_date != today:
            facts["actually_done_on"] = act_date.isoformat()
    return facts


def _activity_date(db: DbSession, result) -> date | None:
    """The day the linked activity happened, or None — a session marked done by hand has
    no activity behind it."""
    from ..models import Activity

    if result.activity_id is None:
        return None
    act = db.get(Activity, result.activity_id)
    if act is None:
        return None
    start = act.start_time_local or act.start_time_utc
    return start.date() if start is not None else None


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
        "todays_sessions": [_session_facts(db, s, today) for s in todays],
        "recovery": signals.recovery_baseline(db, today),
        "recovery_14d": g["recovery_14d"],  # carries avg_sleep_h — the prompt asks for a sleep trend
        # Per-night bed→wake windows: a single short night is INVISIBLE in the 14-day
        # average above, and stage-classified asleep-hours can't tell a short night from
        # a restless full one. See recent_sleep_nights and the doctrine's sleep rule.
        "sleep_nights": signals.recent_sleep_nights(db, today),
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
