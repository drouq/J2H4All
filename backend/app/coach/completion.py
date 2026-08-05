"""How a planned session actually turned out — the single classifier behind the
calendar's marking, the coach's prompts, and the "what happened?" question.

Five states, deterministic (PRD §9 layer 3):

  planned    🏃/🚶/🏋️  still ahead — today's session included, the day isn't over
  done       ✅         completed within TOLERANCE of the planned duration/distance
  partial    ⚠️         completed, but more than TOLERANCE off plan
  missed     🏃/🚶/🏋️  the day closed with nothing against it — KEEPS its type icon
  abandoned  ❌         still not done after ABANDONED_AFTER_DAYS

**`missed` and `abandoned` are deliberately different things** (the athlete's distinction,
2026-08-05). A session he hasn't done *yet* is not the same event as one that is gone:
he logs late, he shifts a run by a day or two, and a run done later still links to its
session via the watch workout id. So a missed session keeps its 🏃 and can still become
✅ — only an abandoned one is crossed out. This replaced a single `missed` state that
meant "not done after 10 days", which forced the calendar to show a run as merely
planned for ten days while the coach's own signals called it missed on day one; the two
notions now have two names instead of one word doing both jobs. `coach/missed.py` asks
about a MISSED run the next morning; nothing chases an ABANDONED one.

Why `partial` is its own state: a session that came in a third short is NOT the
same event as one hit on the nose, and the difference is the coach's most useful
prompt to ask about. On 2026-08-01 a 3h long run came in at 2h01 for logistical
reasons; the coach read the shortfall as physical, flagged it, and proposed an
easier week off that assumption — the wrong week for the wrong reason. The status
is measured off the PRESCRIPTION (duration and/or distance), never off an execution
or quality score: the question is "did the session happen as planned", and only he
knows why not.
"""

from datetime import date, timedelta
from typing import NamedTuple

from ..models import Session, SessionResult

# How far off plan still counts as "done as planned".
TOLERANCE = 0.20

# ...but a percentage alone is too jumpy on a short session, because the denominator
# is small. Measured against his real July block: easy runs land 8-25% over plan as a
# matter of course (50→59, 55→65, 55→69 min) because he runs a loop or a round
# distance, not a stopwatch — while the one deviation that actually meant something
# was 180→121 min. Ten minutes over on an easy run is not a training event; an hour
# off a long run is. So a session must ALSO miss by this much in absolute terms
# before it counts as off-plan. This only ever makes the check stricter.
MIN_GAP = {"duration": 15.0, "distance": 2.0}  # minutes / km
# A missed session is only ABANDONED (❌) once it's this old — he sometimes logs late,
# and a run done on the day still links via the watch workout id. Before that it keeps
# its type icon and can still turn into a ✅.
ABANDONED_AFTER_DAYS = 10

# Session types whose planned duration is nominal rather than prescribed, so a
# delta means nothing: gym time includes rest between sets and warm-up faff (his
# 45-min gym sessions log 64-81 min routinely), and rest isn't measured at all.
# Everything else — every run type, present or future — gets the check.
NO_DELTA_TYPES = frozenset({"strength", "rest"})

PLANNED, DONE, PARTIAL = "planned", "done", "partial"
MISSED, ABANDONED = "missed", "abandoned"

# MISSED is deliberately absent: callers fall back to the session's type emoji, which is
# exactly the "keeps its 🏃 until completed or abandoned" rule.
STATUS_EMOJI = {DONE: "✅", PARTIAL: "⚠️", ABANDONED: "❌"}


class Delta(NamedTuple):
    metric: str      # "duration" | "distance"
    fraction: float  # signed, relative to plan (-0.33 = a third short)
    gap: float       # signed, absolute (minutes or km)


def delta(session: Session, result: SessionResult | None) -> Delta | None:
    """The planned metric that moved most — e.g. Delta("duration", -0.33, -59.0) for
    121 min against 180 planned. Pure measurement: it reports the gap and takes no
    view on whether the gap matters (that's `classify`). None when the session
    prescribed nothing comparable, or the type isn't delta-checked."""
    if result is None or session.type in NO_DELTA_TYPES:
        return None
    moves: list[Delta] = []
    for metric, planned, actual in (
        ("duration", session.duration_min, result.actual_duration_min),
        ("distance", session.distance_km, result.actual_distance_km),
    ):
        if planned and actual:
            moves.append(Delta(metric, (actual - planned) / planned, actual - planned))
    if not moves:
        return None
    return max(moves, key=lambda m: abs(m.fraction))


def off_plan(session: Session, result: SessionResult | None) -> bool:
    """Did this session miss its prescription by enough to be worth remarking on?
    BOTH a relative and an absolute miss — see MIN_GAP for why one alone isn't
    enough."""
    d = delta(session, result)
    return (d is not None
            and abs(d.fraction) > TOLERANCE
            and abs(d.gap) >= MIN_GAP[d.metric])


def classify(session: Session, result: SessionResult | None, today: date) -> str:
    """One of PLANNED / DONE / PARTIAL / MISSED / ABANDONED.

    Note the day-boundary: TODAY's unrun session is still PLANNED, not missed — he
    runs at 5-6pm and sometimes 21:00, so the day has to close first (the same rule
    `coach/missed.py` fires on)."""
    if result is not None and result.completed:
        return PARTIAL if off_plan(session, result) else DONE
    if session.date < today - timedelta(days=ABANDONED_AFTER_DAYS):
        return ABANDONED
    if session.date < today:
        return MISSED
    return PLANNED


def _fmt(metric: str, value: float) -> str:
    return f"{value:g} km" if metric == "distance" else f"{int(round(value))} min"


def delta_line(session: Session, result: SessionResult | None) -> str | None:
    """Plain statement of the gap, for a card or a prompt — the numbers only, no
    cause attached. e.g. "121 min against 180 min planned — 33% short"."""
    d = delta(session, result)
    if d is None:
        return None
    planned = session.duration_min if d.metric == "duration" else session.distance_km
    actual = result.actual_duration_min if d.metric == "duration" else result.actual_distance_km
    word = "short of" if d.fraction < 0 else "over"
    return (f"{_fmt(d.metric, actual)} against {_fmt(d.metric, planned)} planned — "
            f"{abs(d.fraction) * 100:.0f}% {word} plan")
