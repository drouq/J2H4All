"""Notice a planned run that never happened, and ask — once, without assuming.

The twin of `postrun.ask_about_deviations`. That one fires when they ran a session more
than 20% off its prescription; this one fires when nothing came in at all. The gap it
fills was exact: **run 40% of a session and the coach asks, run 0% of it and the coach
said nothing.** (2026-08-05: a Monday easy run went undone, the watch synced that
evening and the next morning, and neither the 22:00 debrief nor the 10:00 brief
mentioned it — because `ask_about_deviations` JOINs an existing `SessionResult`, so a
session with no activity can never reach it, and `build_brief` is only handed TODAY's
sessions and has no view of yesterday at all.)

Every rule below is the coach's own, asked with full doctrine + live prod state:

- **Fires in the NEXT MORNING'S brief**, once the day is closed — not the 22:00 debrief
  and not on sync. They sometimes run at 21:00, and a watch that hasn't synced looks
  identical to a run that didn't happen; asking "did you not run?" half an hour after they
  ran is the single worst failure mode for this feature, because it proves the system
  isn't watching properly, which is the exact complaint it exists to fix. Sync-time firing
  is worse still: sync events land at arbitrary hours with no relation to the day being
  over. The cost is ~12 hours of lag, and that is fine — a Tuesday-morning question about
  Monday's run is still perfectly timed to be useful.
- **`[Optional]` runs NEVER trigger it.** The optional 4th run exists so they can decline it
  without a conversation; asking converts an explicit permission into a soft obligation
  and they would stop skipping it freely. A hard exclusion, not a judgement.
- **Gym/strength NEVER triggers it.** Two upper-body days a week are a habit structure they
  set, not a stimulus being periodized — they move them around and nothing they'd prescribe
  changes if one lands on Thursday. Pinging about gym would also dilute the run signal.
  A months-long disappearance is a pattern for the weekly review, not a daily ping.
- **Raised at most ONCE per session** (`Session.missed_asked_at`), never re-raised. This is
  the rule that makes the feature safe: the maximum cost of a false positive is one
  sentence, one time.
- **One message per morning**, naming up to `NAME_LIMIT` runs. Beyond that it stops being
  a roll-call and asks one open question about the week instead — several missed runs is a
  life problem, not a session problem, and a list of grievances is the wrong response.
- **`LOOKBACK_DAYS` = 7.** Older than a week it isn't rescheduleable, it's archaeology.

**"Don't chase" is not "don't ask"** — the coach drew this line explicitly, because the
doctrine tells the weekly review not to chase missed sessions and that is what left nobody
asking. *Don't chase* = never reschedule it, compensate for it, shrink a later session to
repay it, or raise it twice; a missed week is absorbed. *Ask once* = notice out loud, from
which nothing follows automatically. The asking exists to SERVE the no-chasing rule: a gap
has no diagnostic meaning on its own (logistics, weather, a phone call and real fatigue are
identical in the data), so asking is how the coach avoids guessing — and guessing is what
produces the being-managed-by-a-machine feeling. If a physiological marker has actually
moved, that is the red-flag path reasoning from the marker, not from the gap.
"""

import logging
from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession

from ..models import Session, SessionResult
from ..plan.store import RUN_SESSION_TYPES
from ..util import utcnow as _utcnow

logger = logging.getLogger(__name__)

# Must stay BELOW `completion.ABANDONED_AFTER_DAYS` (10). Everything inside this window is
# still `missed` — liveable, keeping its 🏃 — and asking about a liveable session is the
# whole point. Raise it past the grace window and the coach starts asking about `abandoned`
# ones, which contradicts don't-chase: an abandoned session is history, not a question.
# Locked by a test.
LOOKBACK_DAYS = 7
NAME_LIMIT = 3
OPTIONAL_MARKER = "[optional]"


def outstanding_runs(db: DbSession, today: date) -> list[Session]:
    """Planned runs in the closed-day window with no activity against them, not yet
    raised. Runs only, never `[Optional]`, never gym/rest."""
    resulted = select(SessionResult.session_id).where(SessionResult.session_id.isnot(None))
    rows = db.scalars(
        select(Session).where(
            Session.status == "planned",
            Session.type.in_(RUN_SESSION_TYPES),
            Session.date < today,                                   # the day is closed
            Session.date >= today - timedelta(days=LOOKBACK_DAYS),
            Session.missed_asked_at.is_(None),                      # raised at most once
            Session.id.not_in(resulted),
        ).order_by(Session.date)
    ).all()
    return [s for s in rows if OPTIONAL_MARKER not in (s.title or "").lower()]


def _label(d: date, today: date) -> str:
    """'Monday's' inside the last six days. At exactly a week the weekday collides with
    today's and would read as a different session, so date it instead. (`%-d` is not
    portable to Windows — strip the zero by hand.)"""
    if (today - d).days <= 6:
        return f"{d.strftime('%A')}'s"
    return f"{d.strftime('%d %b').lstrip('0')}'s"


def _clean(title: str) -> str:
    return (title or "").lstrip("🏃🚶🏋️⚡🏁 ").strip()


def notice_text(sessions: list[Session], today: date) -> str:
    """The coach's wording. Deterministic — no LLM call, so it can't drift into implying
    a cause. NB their draft also asserted "your markers are all at or better than baseline";
    that is dropped deliberately, because it was true the morning they wrote it and this text
    ships every morning. "Nothing changes off the back of it" is doctrine and always true;
    a marker that HAS moved is the red-flag path's job, reasoning from the marker."""
    if len(sessions) > NAME_LIMIT:
        span = (today - sessions[0].date).days
        return (
            f"{len(sessions)} runs are sitting empty over the past {span} days. Nothing changes "
            "on my side and there's nothing to make up — the plan picks up from today as "
            "written. How's the week looking on your end?"
        )
    named = ", ".join(f"{_label(s.date, today)} {_clean(s.title)}" for s in sessions)
    head = "One run sitting empty" if len(sessions) == 1 else f"{len(sessions)} runs sitting empty"
    return (
        f"{head}: {named}. No story attached from my side, and nothing about the plan changes "
        "off the back of it — there's nothing to make up. Just flagging that I noticed. If "
        "something in the week ahead needs shifting, tell me and I'll rework it; otherwise we "
        "carry on from today as written."
    )


def pending_notice(db: DbSession, today: date | None = None) -> tuple[str, list[Session]] | None:
    """The text to append to this morning's brief, plus the sessions it covers. Marking
    is the CALLER's job (`mark_raised`), after the send succeeds — a notice that was
    never delivered must not burn its one chance to be raised."""
    if today is None:
        from .schedule import local_today
        today = local_today(db)
    sessions = outstanding_runs(db, today)
    if not sessions:
        return None
    return notice_text(sessions, today), sessions


def mark_raised(db: DbSession, sessions: list[Session]) -> None:
    now = _utcnow()
    for s in sessions:
        s.missed_asked_at = now
    db.commit()
    logger.info("Raised %d missed run(s): %s", len(sessions), [s.date.isoformat() for s in sessions])
