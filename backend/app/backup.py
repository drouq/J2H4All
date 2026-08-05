"""Monthly JSON state export to Google Drive (PRD §15): the coach's full state
dumped to the user's own Drive — his data in his hands, independent of Render, and
the natural migration path off the host.

Uses the drive.file scope (least privilege — only touches files it creates). Runs
from the monthly cron or the manual "Export now" button. Degrades loudly if the
Drive scope hasn't been granted yet (reconnect Google)."""

import json
import logging
import uuid
from datetime import date, datetime

import httpx
from sqlalchemy import func, select
from sqlalchemy.orm import Session as DbSession

from .calendar import oauth
from .util import utcnow as _utcnow
from .models import (
    Activity, AvailabilityWindow, BloodMarker, Checkin, DietaryProfile, FitnessMarker,
    Goal, InjuryLog, MacroPlan, Message, Note, Preference, Proposal, SecondaryRace,
    Session, SessionResult, SyncRun, UserState, WellnessDaily,
)

logger = logging.getLogger(__name__)

FOLDER_NAME = "J2H4All Backups"
FOLDER_PREF = "drive_backup_folder_id"
LAST_EXPORT_PREF = "last_drive_export"
DRIVE_FILES = "https://www.googleapis.com/drive/v3/files"
DRIVE_UPLOAD = "https://www.googleapis.com/upload/drive/v3/files?uploadType=multipart"


class DriveNotAuthorized(RuntimeError):
    """Drive scope not granted — reconnect Google to enable backups."""


def _rows(db, model) -> list[dict]:
    """Serialize every column of every row of a table to JSON-safe dicts."""
    out = []
    for obj in db.scalars(select(model)).all():
        d = {}
        for c in model.__table__.columns:
            v = getattr(obj, c.name)
            if isinstance(v, (datetime, date)):
                v = v.isoformat()
            d[c.name] = v
        out.append(d)
    return out


def assemble_state(db: DbSession) -> dict:
    """The full coach state as a JSON-safe dict (PRD §15). Excludes secrets."""
    return {
        "exported_at": _utcnow().isoformat(),
        "schema": "j2h4all-state-v1",
        "goal": _rows(db, Goal),
        "secondary_race": _rows(db, SecondaryRace),
        "macro_plan": _rows(db, MacroPlan),
        "session": _rows(db, Session),
        "session_result": _rows(db, SessionResult),
        "dietary_profile": _rows(db, DietaryProfile),
        "blood_marker": _rows(db, BloodMarker),
        "availability_window": _rows(db, AvailabilityWindow),
        "injury_log": _rows(db, InjuryLog),
        "preference": _rows(db, Preference),
        "note": _rows(db, Note),
        "checkin": _rows(db, Checkin),
        "proposal": _rows(db, Proposal),
        "message": _rows(db, Message),
        "user_state": _rows(db, UserState),
        "sync_run": _rows(db, SyncRun),
        "fitness_marker": _rows(db, FitnessMarker),
        # Garmin activity/wellness are large (raw payloads); back up as counts here.
        # The full 2-year Garmin history is covered by Render's Postgres backups.
        "activity_count": db.scalar(select(func.count(Activity.id))),
        "wellness_daily_count": db.scalar(select(func.count(WellnessDaily.date))),
    }


def _pref(db: DbSession, key: str) -> Preference | None:
    return db.scalar(select(Preference).where(Preference.key == key))


def _set_pref(db: DbSession, key: str, value: str) -> None:
    p = _pref(db, key)
    if p is None:
        db.add(Preference(key=key, value=value, updated_at=_utcnow()))
    else:
        p.value = value
        p.updated_at = _utcnow()
    db.commit()


def _ensure_folder(db: DbSession, token: str) -> str:
    cached = _pref(db, FOLDER_PREF)
    if cached:
        return cached.value
    headers = {"Authorization": f"Bearer {token}"}
    # drive.file search only sees files we created — so this finds our own folder.
    q = f"name='{FOLDER_NAME}' and mimeType='application/vnd.google-apps.folder' and trashed=false"
    r = httpx.get(DRIVE_FILES, headers=headers, params={"q": q, "fields": "files(id)"}, timeout=20)
    if r.status_code == 403:
        raise DriveNotAuthorized("Drive access not granted — reconnect Google Calendar to enable backups")
    r.raise_for_status()
    files = r.json().get("files", [])
    if files:
        folder_id = files[0]["id"]
    else:
        cr = httpx.post(DRIVE_FILES, headers=headers,
                        json={"name": FOLDER_NAME, "mimeType": "application/vnd.google-apps.folder"},
                        timeout=20)
        cr.raise_for_status()
        folder_id = cr.json()["id"]
    _set_pref(db, FOLDER_PREF, folder_id)
    return folder_id


def _upload(token: str, folder_id: str, name: str, payload: dict) -> dict:
    boundary = uuid.uuid4().hex
    metadata = {"name": name, "parents": [folder_id], "mimeType": "application/json"}
    body = (
        f"--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n"
        + json.dumps(metadata)
        + f"\r\n--{boundary}\r\nContent-Type: application/json\r\n\r\n"
        + json.dumps(payload, default=str)
        + f"\r\n--{boundary}--"
    ).encode("utf-8")
    r = httpx.post(
        DRIVE_UPLOAD,
        headers={"Authorization": f"Bearer {token}",
                 "Content-Type": f"multipart/related; boundary={boundary}"},
        content=body, timeout=60,
    )
    if r.status_code == 403:
        raise DriveNotAuthorized("Drive access not granted — reconnect Google Calendar to enable backups")
    r.raise_for_status()
    return r.json()


def run_export(db: DbSession, today: date | None = None) -> dict:
    """Assemble state and upload it to Drive. Returns file info. Raises
    CalendarNotConnected / DriveNotAuthorized to degrade loudly (PRD §15/§4)."""
    today = today or date.today()
    token = oauth.access_token(db)  # raises CalendarNotConnected if not connected
    folder_id = _ensure_folder(db, token)
    name = f"j2h4all-state-{today.isoformat()}.json"
    result = _upload(token, folder_id, name, assemble_state(db))
    _set_pref(db, LAST_EXPORT_PREF, _utcnow().isoformat())
    logger.info("Drive export uploaded: %s (%s)", name, result.get("id"))
    return {"file_id": result.get("id"), "name": name}


def status(db: DbSession) -> dict:
    last = _pref(db, LAST_EXPORT_PREF)
    return {
        "connected": oauth.is_connected(db),
        "drive_authorized": oauth.drive_authorized(db),
        "last_export": last.value if last else None,
    }
