"""First-run setup state: what is done, what is next, and what blocks a plan.

`doctor` answers the same question for someone at a terminal, deliberately without
touching the network and mostly from configuration. This answers it for someone in
the web app, and looks at the DATABASE as well - whether history has actually been
imported, whether the athlete has told the coach who they are, whether the goal is
still the placeholder. Those are the parts that are invisible from the environment
and are exactly where a new install stalls.

Two rules, same as `doctor`:
- **Never raises.** This is what someone loads when the app is misbehaving.
- **Never writes.** Safe to poll from a panel.

`blocking=True` marks a step that makes plan generation actively WRONG rather than
merely incomplete - see `blockers()`. Everything else degrades: a coach with no
Telegram is a worse coach, but it still coaches.
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass

logger = logging.getLogger(__name__)


@dataclass
class Step:
    key: str
    label: str
    done: bool
    detail: str
    # What to do about it, in the athlete's terms - not the variable name.
    action: str = ""
    # True if drafting a plan without this produces a confidently wrong plan.
    blocking: bool = False


def _safe(fn, default):
    try:
        return fn()
    except Exception as exc:  # noqa: BLE001 - a broken probe must not hide the rest
        logger.warning("setup probe failed: %s", exc)
        return default


def steps(db) -> list[Step]:
    from .config import get_settings
    from .context.store import profile_view
    from .models import Activity, Goal, MacroPlan
    from .plan import store as plan_store
    from sqlalchemy import func, select

    s = get_settings()
    out: list[Step] = []

    # --- the coach itself ---
    out.append(Step(
        "anthropic", "AI coach", bool(s.anthropic_api_key),
        "Connected." if s.anthropic_api_key else "No Anthropic API key - the coach cannot think.",
        "Set ANTHROPIC_API_KEY (see SETUP.md step 1).", blocking=True,
    ))

    # --- who is being coached ---
    profile = _safe(lambda: profile_view(db), {"configured": False, "name": None, "pronouns": "they/them"})
    out.append(Step(
        "profile", "About you", bool(profile.get("configured")),
        (f"Coaching {profile.get('name') or 'you'} ({profile.get('pronouns')})."
         if profile.get("configured") else "The coach doesn't know who you are yet."),
        "Tell it in chat or the Context panel: your name, pronouns, age, and anything "
        "that makes your data read wrong (restless legs, night shifts, medication).",
    ))

    # --- the race everything is planned backwards from ---
    # A FAILED read is not the same as "no race set", and reporting it as one sends
    # someone to fill in a form that was never the problem. The usual cause is an
    # unmigrated database, so say that instead. (Degrade loudly - see ARCHITECTURE.)
    sentinel = object()
    goal = _safe(lambda: db.scalar(select(Goal).where(Goal.status == "active").limit(1)), sentinel)
    if goal is sentinel:
        out.append(Step(
            "goal", "Your race", False,
            "Couldn't read the goal - the database looks out of date.",
            "Run `alembic upgrade head` from backend/, and check DATABASE_URL.",
            blocking=True,
        ))
    else:
        placeholder = plan_store.is_placeholder(goal)
        out.append(Step(
            "goal", "Your race", bool(goal) and not placeholder,
            ("No race set." if goal is None else
             f"Still the placeholder ({goal.race_date})." if placeholder else
             f"{goal.format} on {goal.race_date}."),
            "Set your race format and date - the whole plan is built backwards from it.",
            blocking=True,
        ))

    # --- physiology ---
    n_activities = _safe(lambda: db.scalar(select(func.count(Activity.id))) or 0, 0)
    out.append(Step(
        "garmin", "Garmin", bool(s.garth_token) and s.garmin_sync_enabled,
        ("Sync is switched off." if not s.garmin_sync_enabled else
         "Connected." if s.garth_token else "No Garmin token - no physiology data."),
        "Run `python -m app.garmin.login` on your home machine (Garmin blocks datacenter "
        "IPs), then paste the token below.",
    ))
    out.append(Step(
        "history", "Training history", n_activities > 0,
        (f"{n_activities} activities imported." if n_activities else "Nothing imported yet."),
        "Run `python -m app.jobs full_import` to pull your history. The coach plans off "
        "what your body has already absorbed, so this matters more than it looks.",
    ))

    # --- where the coach reaches you, and what it writes to ---
    connected = _safe(lambda: __import__("app.calendar.oauth", fromlist=["oauth"]).is_connected(db), False)
    out.append(Step(
        "calendar", "Google Calendar", bool(connected),
        "Connected." if connected else "Not connected - sessions won't reach your calendar.",
        "Use Connect Google Calendar in the Calendar panel.",
    ))
    # Bound may come from the env var OR from pairing, so ask the resolver rather
    # than the setting - reading only the env would report a paired bot as missing.
    from . import telegram_link

    bound = _safe(lambda: telegram_link.bound_chat_id(db), None)
    out.append(Step(
        "telegram", "Telegram", bool(s.telegram_bot_token and bound),
        ("Connected." if s.telegram_bot_token and bound else
         "Bot configured but not linked to a chat yet - it currently answers nobody."
         if s.telegram_bot_token else
         "Not configured - no morning brief, evening debrief or approval cards."),
        ("Get a pairing code below and send it to your bot."
         if s.telegram_bot_token else
         "Create a bot with @BotFather and set TELEGRAM_BOT_TOKEN (see SETUP.md step 5). "
         "You can then link it from here - no need to hunt for a chat ID."),
    ))

    # --- the output ---
    has_plan = _safe(
        lambda: db.scalar(select(MacroPlan.id).where(MacroPlan.status == "active").limit(1)) is not None,
        False)
    out.append(Step(
        "plan", "Training plan", bool(has_plan),
        "Active plan." if has_plan else "No plan yet.",
        "Draft one from the Plan panel once the steps above are done - you review and "
        "approve before anything is written.",
    ))
    return out


def blockers(db) -> list[Step]:
    """Steps that make a DRAFTED PLAN WRONG, not merely incomplete.

    Only two things qualify. Without an API key nothing generates at all. Without a
    real race, the coach periodizes backwards from a date nobody chose - and it will
    do that perfectly confidently, which is worse than refusing, because the athlete
    has no way to tell the difference by looking at it.

    Deliberately NOT blocking: Garmin history, calendar, Telegram, and the athlete
    profile. Those degrade the plan without falsifying it, and someone who wants a
    plan before connecting their watch should be allowed one - it just won't be
    grounded in their load. Refusing there would be paternalistic.
    """
    return [s for s in steps(db) if s.blocking and not s.done]


def view(db) -> dict:
    rows = steps(db)
    return {
        "steps": [asdict(s) for s in rows],
        "complete": all(s.done for s in rows),
        "blockers": [s.key for s in rows if s.blocking and not s.done],
        "next": next((s.key for s in rows if not s.done), None),
    }
