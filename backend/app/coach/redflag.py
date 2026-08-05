"""Red-flag proactivity (PRD §12): between weekly reviews the coach stays quiet
UNLESS something acute appears — HRV crash, resting-HR spike, a flagged run, or
logged pain. Then it proactively pings Telegram with a *proposed* conservative
change (never silently applied — §11). Deterministic detection first; Sonnet only
drafts the adjustment when a flag actually trips and nothing is already pending."""

import json
import logging
from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession

from ..llm import LLMNotConfigured, call_tool
from ..models import Checkin, InjuryLog, LifestyleLog, Proposal, Session, SessionResult
from ..plan.structure import STRUCTURE_SCHEMA
from ..util import as_dt as _as_dt
from . import completion, doctrine, signals

logger = logging.getLogger(__name__)

HRV_CRASH_RATIO = 0.85   # recent 3d HRV below 85% of 28d baseline
RHR_SPIKE_BPM = 5        # recent 3d resting HR this many bpm over baseline
SKIN_TEMP_DEV_C = 1.0    # last night's skin temp this far above personal baseline → possible fever
RESP_ELEVATED_BRPM = 3   # 3d waking respiration this far above 28d baseline → illness signal

# How long a red-flag REASON stays "already raised". A flagged run lingers in
# `detect` for 2 days and a suppressed marker for as long as it's suppressed, so
# without this every sync re-proposed the same adjustment for the same cause —
# five cards off one cut-short long run (2026-08-03), two of them already approved
# and the last a no-op re-draft of an unchanged week. Approving a no-op card
# rewrites the whole window and re-pushes calendar+watch for nothing, and cards
# he's expected to read stop being worth reading. Keyed on the reason (not on
# whether one is pending), so a genuinely NEW signal still pings immediately.
DEDUPE_DAYS = 7

ADJUST_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "summary": {"type": "string", "description": "2-3 sentences for a Telegram card: what you saw and what you propose. Coach voice."},
        "change_note": {"type": "string", "description": "Plain 'what changed and why' vs the current plan (change-transparency, §9)."},
        "sessions": {
            "type": "array",
            "description": "The revised sessions for the affected next few days (same dates you were given). Conservative — reduce load, protect recovery.",
            "items": {
                "type": "object", "additionalProperties": False,
                "properties": {
                    "date": {"type": "string"}, "type": {"type": "string"},
                    "title": {"type": "string"},
                    "duration_min": {"type": ["integer", "null"]},
                    "distance_km": {"type": ["number", "null"]},
                    "target_zone": {"type": ["string", "null"]},
                    "target_pace": {"type": ["string", "null"]},
                    "purpose": {"type": "string"},
                    "fueling_note": {"type": ["string", "null"]},
                    "structure": STRUCTURE_SCHEMA,
                },
                "required": ["date", "type", "title", "duration_min", "distance_km",
                             "target_zone", "target_pace", "purpose", "fueling_note", "structure"],
            },
        },
    },
    "required": ["summary", "change_note", "sessions"],
}


def detect_flags(db: DbSession, today: date) -> list[tuple[str, str]]:
    """Deterministic acute signals as (key, reason) pairs. The key is STABLE across
    days for the same underlying cause — plain for a state signal (a marker stays
    suppressed for as long as it's suppressed) and entity-scoped for a discrete
    event (`flagged_run:<result id>`) — so `check_and_propose` can tell a fresh
    signal from one it has already raised a card for. Empty = all clear."""
    reasons: list[tuple[str, str]] = []
    base = signals.recovery_baseline(db, today)
    if base["hrv_recent_3d"] and base["hrv_baseline_28d"]:
        if base["hrv_recent_3d"] < HRV_CRASH_RATIO * base["hrv_baseline_28d"]:
            reasons.append((
                "hrv_suppressed",
                f"HRV suppressed: {base['hrv_recent_3d']} vs {base['hrv_baseline_28d']} baseline (3d avg)."
            ))
    if base["rhr_recent_3d"] and base["rhr_baseline_28d"]:
        if base["rhr_recent_3d"] >= base["rhr_baseline_28d"] + RHR_SPIKE_BPM:
            reasons.append((
                "rhr_elevated",
                f"Resting HR elevated: {base['rhr_recent_3d']} vs {base['rhr_baseline_28d']} baseline."
            ))
    # Illness early-warning from the deep recovery signals (mined from stored raw).
    # Restlessness is deliberately NOT a trigger — his RLS makes it chronically noisy.
    deep = signals.deep_recovery(db, today)
    latest = deep.get("latest") or {}
    std = latest.get("skin_temp_dev_c")
    if std is not None and std >= SKIN_TEMP_DEV_C and latest.get("date", "") >= (today - timedelta(days=1)).isoformat():
        reasons.append((
            "skin_temp",
            f"Skin temperature +{std:.1f}°C above personal baseline last night — possible incoming illness."
        ))
    r3, rb = deep.get("respiration_recent_3d"), deep.get("respiration_baseline_28d")
    resp_elevated = bool(r3 and rb and r3 >= rb + RESP_ELEVATED_BRPM)
    if resp_elevated:
        reasons.append((
            "respiration",
            f"Waking respiration elevated: {r3} vs {rb} brpm baseline (3d avg) — classic illness signal."
        ))
    # Self-reported illness (from the 22:00 lifestyle log) CORROBORATED by a physiological
    # signal — a strong "getting sick" combination worth a conservative ping. A bare "bit
    # run down" note without any marker move is deliberately NOT a trigger (it's captured
    # in the debrief and the brief reads it); we only proactively adjust when body + report
    # agree, keeping precision high per the RLS-noisy-signals doctrine.
    rhr_elevated = bool(base["rhr_recent_3d"] and base["rhr_baseline_28d"]
                        and base["rhr_recent_3d"] >= base["rhr_baseline_28d"] + RHR_SPIKE_BPM)
    skin_elevated = std is not None and std >= SKIN_TEMP_DEV_C
    ll = db.scalar(select(LifestyleLog).order_by(LifestyleLog.date.desc()).limit(1))
    illness = (ll.data or {}).get("illness") if ll else None
    if illness and ll.date >= today - timedelta(days=1) and (skin_elevated or resp_elevated or rhr_elevated):
        reasons.append((
            "illness_corroborated",
            f"Logged feeling ill ('{illness}') and a physiological signal agrees — likely getting sick; "
            "ease off and protect recovery."
        ))
    # Flagged run in the last 2 days
    flagged = db.scalar(
        select(SessionResult).where(
            SessionResult.flagged.is_(True),
            SessionResult.created_at >= _as_dt(today - timedelta(days=2)),
        ).limit(1)
    )
    if flagged:
        # An off-plan session with no stated reason is NOT a red flag — the data can't
        # say why a run ended early, and proposing an easier week off that guess is the
        # 2026-08-01 mistake (logistical cutoff read as fatigue). `postrun` has asked
        # him; once he answers, `deviation_reason` rides along and the coach reasons
        # from the real cause. A flag backed by anything else still fires normally.
        session = db.get(Session, flagged.session_id) if flagged.session_id else None
        off_plan = session is not None and completion.classify(session, flagged, today) == completion.PARTIAL
        if off_plan and not flagged.deviation_reason:
            logger.info("Flagged result %s is an unexplained off-plan session; awaiting his reason, "
                        "not proposing", flagged.id)
        else:
            note = flagged.note or "anomalous read"
            if flagged.deviation_reason:
                note += f" — he says: {flagged.deviation_reason}"
            reasons.append((f"flagged_run:{flagged.id}", f"A recent run was flagged: {note}."))
    # Soreness/pain from the latest checkin
    ci = db.scalar(select(Checkin).order_by(Checkin.date.desc()).limit(1))
    if ci and ci.date >= today - timedelta(days=2):
        if (ci.soreness or 0) >= 4:
            reasons.append(("soreness",
                            f"Check-in soreness high ({ci.soreness}/5)"
                            + (f": '{ci.note}'." if ci.note else ".")))
    # New/active injury logged in the last 3 days
    inj = db.scalar(
        select(InjuryLog).where(
            InjuryLog.status == "active",
            InjuryLog.updated_at >= _as_dt(today - timedelta(days=3)),
        ).limit(1)
    )
    if inj:
        reasons.append((f"injury:{inj.id}", f"Active injury: {inj.body_part} ({inj.notes or 'logged'})."))
    return reasons


def detect(db: DbSession, today: date) -> list[str]:
    """Human-readable acute reasons (empty = all clear). Text-only view of
    `detect_flags` for prompts and callers that don't care about dedupe keys."""
    return [text for _key, text in detect_flags(db, today)]


def raised_keys(db: DbSession, today: date) -> set[str]:
    """Reason keys a red-flag card has already been raised for in the last
    DEDUPE_DAYS — whatever became of that card. Approved (the plan was already
    adjusted for it), rejected (he said no), and superseded (a later weekly review
    re-planned the same window) all mean the same thing here: he has seen it."""
    rows = db.scalars(
        select(Proposal).where(
            Proposal.origin == "red_flag",
            Proposal.created_at >= _as_dt(today - timedelta(days=DEDUPE_DAYS)),
        )
    ).all()
    return {k for p in rows for k in ((p.payload or {}).get("red_flag_keys") or [])}


def check_and_propose(db: DbSession, today: date | None = None):
    """If an acute signal we haven't already raised is present and nothing is
    pending, draft a conservative adjustment and return (proposal, summary).
    Otherwise return None."""
    if today is None:
        from .schedule import local_today
        today = local_today(db)
    flags = detect_flags(db, today)
    if not flags:
        return None
    # Only ping for a cause we haven't already raised a card for. A lingering signal
    # (a flagged run stays visible for 2 days, a suppressed marker for as long as it
    # lasts) must not re-propose daily; a NEW key alongside it still fires, and the
    # card then carries the full picture, so acuity is preserved.
    already = raised_keys(db, today)
    fresh = [(k, text) for k, text in flags if k not in already]
    if not fresh:
        logger.info("Red flag(s) present but all already raised in the last %dd: %s",
                    DEDUPE_DAYS, [k for k, _ in flags])
        return None
    keys = [k for k, _ in flags]
    reasons = [text for _k, text in flags]
    # Don't stack red-flag pings — one pending at a time.
    existing = db.scalar(
        select(Proposal).where(Proposal.status == "pending", Proposal.origin == "red_flag").limit(1)
    )
    if existing:
        logger.info("Red flag(s) present but a red-flag proposal is already pending; not stacking")
        return None

    upcoming = db.scalars(
        select(Session).where(
            Session.status == "planned", Session.type != "rest",
            Session.date >= today, Session.date <= today + timedelta(days=7),
        ).order_by(Session.date)
    ).all()
    if not upcoming:
        logger.info("Red flag(s) present but no upcoming sessions to adjust")
        return None

    facts = {
        "today": today.isoformat(),
        "red_flags": reasons,
        "recovery": signals.recovery_baseline(db, today),
        "recovery_deep": signals.deep_recovery(db, today),
        "recent_checkins": signals.recent_checkins(db, today, days=5),
        "recent_lifestyle": signals.recent_lifestyle(db, today, days=5),
        "upcoming_sessions": [
            {"date": s.date.isoformat(), "type": s.type, "title": s.title,
             "duration_min": s.duration_min, "distance_km": s.distance_km,
             "target_zone": s.target_zone, "target_pace": s.target_pace, "purpose": s.purpose,
             "structure": s.structure}
            for s in upcoming
        ],
    }
    system = (
        "You are the coach in J2H4All. Acute red flags have appeared between weekly reviews. Propose a "
        "CONSERVATIVE adjustment to the next few days only — dial back intensity/volume, insert recovery "
        "or rest, protect the athlete. Keep the same dates you were given (you may change a day to rest). "
        "Be specific and state what changed and why.\n\n"
        + doctrine.compact_doctrine(db, today)
        + "\n\nRemember: chronic consistency toward race day beats any single session — a few easy days now "
        "protects the block; digging the hole deeper does not."
    )
    try:
        out = call_tool(
            task="red_flag", system=system,
            content="Signals and the current upcoming plan (JSON):\n" + json.dumps(facts, default=str),
            tool_name="propose_adjustment", tool_schema=ADJUST_SCHEMA,
            tool_description="Propose a conservative between-review adjustment.",
            max_tokens=4000,
        )
    except LLMNotConfigured:
        logger.info("Red flags present but LLM not configured; skipping proposal")
        return None
    except Exception:
        logger.exception("Red-flag proposal generation failed")
        return None

    from ..plan import proposals as plan_proposals
    summary = out.get("summary", "Proposed a conservative adjustment.")
    payload = {"sessions": out.get("sessions", []), "change_note": out.get("change_note"),
               "red_flag_keys": keys}
    proposal = plan_proposals.create(db, kind="sessions", summary=summary, payload=payload, origin="red_flag")
    logger.info("Red-flag proposal %s created (%d reasons, fresh: %s)",
                proposal.id, len(reasons), [k for k, _ in fresh])
    return proposal, summary
