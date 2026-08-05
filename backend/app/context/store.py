"""Apply confirmed context items to the store, and read the current snapshot.

Writes happen ONLY here, only from confirmed items (PRD §19). The extract step
proposes; the user confirms; this applies. Idempotent where a natural key exists
(blood markers by name+date, preferences by key, timezone as the single row).
"""

from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..util import utcnow as _utcnow

from ..models import (
    AvailabilityWindow,
    BloodMarker,
    DietaryProfile,
    InjuryLog,
    Note,
    Preference,
    UserState,
)


def _parse_date(s: str | None, default: date | None = None) -> date | None:
    if not s:
        return default
    try:
        return date.fromisoformat(s[:10])
    except ValueError:
        return default


def apply_items(db: Session, items: list[dict], source: str = "chat") -> list[str]:
    """Apply each confirmed item; return a list of applied-summary strings."""
    applied: list[str] = []
    today = _utcnow().date()
    for item in items:
        kind = item.get("kind")
        if kind == "blood_marker":
            _apply_blood_marker(db, item, source, today)
        elif kind == "availability_window":
            _apply_window(db, item, today)
        elif kind == "injury":
            _apply_injury(db, item, today)
        elif kind == "dietary_note":
            _apply_dietary(db, item)
        elif kind == "preference":
            _apply_preference(db, item)
        elif kind == "timezone":
            _apply_timezone(db, item)
        elif kind == "note":
            _apply_note(db, item)
        else:
            continue
        applied.append(item.get("summary") or kind)
    db.commit()
    return applied


def _apply_blood_marker(db: Session, item: dict, source: str, today: date) -> None:
    name = (item.get("marker_name") or "").strip().lower()
    if not name or item.get("value") is None:
        return
    measured_on = _parse_date(item.get("measured_on"), today)
    existing = db.scalar(
        select(BloodMarker).where(BloodMarker.name == name, BloodMarker.measured_on == measured_on)
    )
    if existing is None:
        existing = BloodMarker(name=name, measured_on=measured_on, created_at=_utcnow())
        db.add(existing)
    existing.value = float(item["value"])
    existing.unit = item.get("unit")
    existing.source = source


def _apply_window(db: Session, item: dict, today: date) -> None:
    wtype = (item.get("window_type") or "treadmill").strip().lower()
    start = _parse_date(item.get("start_date"), today)
    end = _parse_date(item.get("end_date"))
    # Re-confirming the same window (same type + dates) must not add a duplicate row.
    existing = db.scalar(select(AvailabilityWindow).where(
        AvailabilityWindow.type == wtype,
        AvailabilityWindow.start_date == start,
        AvailabilityWindow.end_date == end,
    ))
    if existing is not None:
        if item.get("text"):
            existing.note = item["text"]
        return
    db.add(AvailabilityWindow(
        type=wtype,
        start_date=start,
        end_date=end,
        note=item.get("text"),
        created_at=_utcnow(),
    ))


def _apply_injury(db: Session, item: dict, today: date) -> None:
    body_part = (item.get("body_part") or "unspecified").strip().lower()
    status = item.get("status") or "active"
    # Update an open injury for the same body part rather than duplicating.
    existing = db.scalar(
        select(InjuryLog).where(InjuryLog.body_part == body_part, InjuryLog.status == "active")
    )
    if existing is None:
        existing = InjuryLog(
            body_part=body_part, start_date=today, created_at=_utcnow(), updated_at=_utcnow()
        )
        db.add(existing)
    existing.status = status
    if status == "resolved" and existing.end_date is None:
        existing.end_date = today
    if item.get("text"):
        existing.notes = item["text"]
    existing.updated_at = _utcnow()


def _apply_dietary(db: Session, item: dict) -> None:
    profile = db.scalar(select(DietaryProfile).limit(1))
    if profile is None:
        profile = DietaryProfile(diet="vegetarian", updated_at=_utcnow())
        db.add(profile)
    note = item.get("text")
    if note:
        profile.notes = f"{profile.notes}\n{note}" if profile.notes else note
    profile.updated_at = _utcnow()


def _apply_preference(db: Session, item: dict) -> None:
    key = (item.get("key") or "").strip().lower()
    value = item.get("text")
    if not key or value is None:
        return
    if _is_internal_pref(key):  # never let a chat-captured preference touch machine state
        return
    existing = db.scalar(select(Preference).where(Preference.key == key))
    if existing is None:
        existing = Preference(key=key, updated_at=_utcnow())
        db.add(existing)
    existing.value = value
    existing.updated_at = _utcnow()


def _apply_timezone(db: Session, item: dict) -> None:
    tz = (item.get("timezone") or "").strip()
    if not tz:
        return
    set_timezone(db, tz)


def _apply_note(db: Session, item: dict) -> None:
    text = item.get("text") or item.get("summary")
    if text:
        db.add(Note(text=text, created_at=_utcnow()))


# ------------------------------------------------------------------ user state

def get_or_create_state(db: Session) -> UserState:
    state = db.get(UserState, 1)
    if state is None:
        state = UserState(id=1, timezone="UTC", updated_at=_utcnow())
        db.add(state)
        db.commit()
    return state


def set_timezone(db: Session, tz: str) -> None:
    state = get_or_create_state(db)
    state.timezone = tz
    state.updated_at = _utcnow()


# ------------------------------------------------------------------ snapshot

# The Preference table doubles as an internal key-value store (rotating Garmin
# OAuth2 token, alert cooldowns, Drive export state, pending-edit marker). That
# is machine state — and the token is a SECRET — so none of it may reach the web
# context panel or an LLM prompt. Only coaching preferences pass through here.
_INTERNAL_PREF_KEYS = {
    "garmin_oauth2_token", "awaiting_checkin_reply", "pending_edit_proposal",
    "pending_context_items", "drive_backup_folder_id", "last_drive_export",
    "last_push_calendar_at", "last_push_garmin_at", "awaiting_lifestyle_reply",
    "awaiting_debrief_reply", "awaiting_deviation_reason",
}
_INTERNAL_PREF_PREFIXES = ("alert_",)


def _is_internal_pref(key: str) -> bool:
    return key in _INTERNAL_PREF_KEYS or key.startswith(_INTERNAL_PREF_PREFIXES)


def stamp_meta(db: Session, key: str, value: str | None = None) -> None:
    """Upsert an internal machine-state preference (filtered from prompts/snapshot).
    `value` defaults to now (UTC ISO) — used for last-push timestamps. The key MUST
    be internal (see `_INTERNAL_PREF_KEYS`), else it would leak into the web context
    panel and LLM prompts."""
    assert _is_internal_pref(key), f"stamp_meta refuses non-internal key {key!r}"
    if value is None:
        value = _utcnow().isoformat()
    pref = db.scalar(select(Preference).where(Preference.key == key))
    if pref is None:
        db.add(Preference(key=key, value=value, updated_at=_utcnow()))
    else:
        pref.value = value
        pref.updated_at = _utcnow()
    db.commit()


def get_meta(db: Session, key: str) -> str | None:
    pref = db.scalar(select(Preference).where(Preference.key == key))
    return pref.value if pref else None


def snapshot(db: Session) -> dict:
    """Current context for display (PRD §19 web trend/context views)."""
    profile = db.scalar(select(DietaryProfile).limit(1))
    markers = db.scalars(
        select(BloodMarker).order_by(BloodMarker.name, BloodMarker.measured_on.desc())
    ).all()
    # Latest reading per marker + count for trend, plus a reference-range flag (typical
    # population range, NOT lab-specific / not diagnostic — the coach flags + defers, §13).
    from .bloods import flag_marker, marker_reference
    latest: dict[str, dict] = {}
    for m in markers:
        if m.name not in latest:
            latest[m.name] = {
                "name": m.name, "value": m.value, "unit": m.unit,
                "measured_on": m.measured_on.isoformat(), "readings": 0,
                "flag": flag_marker(m.name, m.value, m.unit),      # 'low' | 'high' | None
                "reference": marker_reference(m.name, m.unit),     # e.g. '30–400 ng/mL'
            }
        latest[m.name]["readings"] += 1

    windows = db.scalars(
        select(AvailabilityWindow).order_by(AvailabilityWindow.start_date.desc())
    ).all()
    injuries = db.scalars(
        select(InjuryLog).order_by(InjuryLog.updated_at.desc())
    ).all()
    prefs = db.scalars(select(Preference).order_by(Preference.key)).all()
    # 50, not 20: the training-history notes must never age out of prompts just
    # because newer small notes accumulated.
    notes = db.scalars(select(Note).order_by(Note.created_at.desc()).limit(50)).all()
    state = get_or_create_state(db)

    return {
        "timezone": state.timezone,
        "diet": {"diet": profile.diet, "notes": profile.notes} if profile else {"diet": "vegetarian", "notes": None},
        "blood_markers": list(latest.values()),
        "availability_windows": [
            {"id": w.id, "type": w.type, "start_date": w.start_date.isoformat(),
             "end_date": w.end_date.isoformat() if w.end_date else None, "note": w.note}
            for w in windows
        ],
        "injuries": [
            {"id": i.id, "body_part": i.body_part, "status": i.status, "notes": i.notes}
            for i in injuries
        ],
        "preferences": [{"key": p.key, "value": p.value} for p in prefs if not _is_internal_pref(p.key)],
        "notes": [{"id": n.id, "text": n.text, "created_at": n.created_at.isoformat()} for n in notes],
        "recent_lifestyle": _recent_lifestyle_view(db),
    }


def _recent_lifestyle_view(db: Session, days: int = 14) -> list[dict]:
    """Recent end-of-day logs for the web Context panel (most-recent first)."""
    from .. import models
    from ..util import utcnow
    since = utcnow().date() - timedelta(days=days)
    rows = db.scalars(
        select(models.LifestyleLog).where(models.LifestyleLog.date >= since)
        .order_by(models.LifestyleLog.date.desc())
    ).all()
    out = []
    for r in rows:
        data = r.data or {}
        out.append({
            "date": r.date.isoformat(),
            "summary": data.get("summary") or r.raw_text,
            "flags": {k: v for k, v in data.items() if v and k != "summary"},
        })
    return out
