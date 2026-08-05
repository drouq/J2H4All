"""Thin Google Calendar v3 REST client over httpx (PRD §10).

Kept dependency-free (no google-api-python-client) and deliberately narrow: it
only ever creates/touches our own dedicated "J2H4All Training" calendar and its
events. One event per session, all-day (sessions are date-only), with the full
workout in the description so it's readable on the phone without opening the app.
"""

import logging

import httpx

logger = logging.getLogger(__name__)

BASE = "https://www.googleapis.com/calendar/v3"

_EMOJI = {
    "long_run": "🏃", "easy": "🏃", "recovery": "🚶", "intervals": "⚡",
    "tempo": "⚡", "strength": "🏋️", "race": "🏁", "rest": "😴",
}


def event_emoji(session_type: str) -> str:
    return _EMOJI.get(session_type, "🏃")


class CalendarClient:
    def __init__(self, access_token: str):
        self._h = {"Authorization": f"Bearer {access_token}"}

    def create_calendar(self, summary: str, description: str = "") -> str:
        r = httpx.post(f"{BASE}/calendars", headers=self._h,
                       json={"summary": summary, "description": description}, timeout=20)
        r.raise_for_status()
        return r.json()["id"]

    def get_calendar(self, calendar_id: str) -> dict | None:
        r = httpx.get(f"{BASE}/calendars/{calendar_id}", headers=self._h, timeout=20)
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.json()

    def list_events(self, calendar_id: str, time_min_date: str) -> list[dict]:
        """All non-cancelled events starting on/after `time_min_date` (ISO date).
        Returns [{'id','summary','start_date'}]. Used for the calendar-authoritative
        sweep — the calendar itself is the ground truth for what events exist, so a
        ghost whose store row lost its event-id link is still found and removed."""
        out: list[dict] = []
        params = {"timeMin": f"{time_min_date}T00:00:00Z", "singleEvents": "true",
                  "maxResults": 250, "showDeleted": "false"}
        page: str | None = None
        while True:
            p = dict(params)
            if page:
                p["pageToken"] = page
            r = httpx.get(f"{BASE}/calendars/{calendar_id}/events", headers=self._h, params=p, timeout=20)
            r.raise_for_status()
            data = r.json()
            for e in data.get("items", []):
                if e.get("status") == "cancelled":
                    continue
                start = e.get("start") or {}
                sd = (start.get("date") or start.get("dateTime") or "")[:10]
                out.append({"id": e["id"], "summary": e.get("summary", ""), "start_date": sd})
            page = data.get("nextPageToken")
            if not page:
                return out

    def insert_event(self, calendar_id: str, body: dict) -> str:
        r = httpx.post(f"{BASE}/calendars/{calendar_id}/events", headers=self._h, json=body, timeout=20)
        r.raise_for_status()
        return r.json()["id"]

    def update_event(self, calendar_id: str, event_id: str, body: dict) -> None:
        r = httpx.put(f"{BASE}/calendars/{calendar_id}/events/{event_id}",
                      headers=self._h, json=body, timeout=20)
        # Google returns 410 Gone (not 404) for an event the user deleted.
        if r.status_code in (404, 410):
            raise EventGone(event_id)
        r.raise_for_status()

    def delete_event(self, calendar_id: str, event_id: str) -> None:
        r = httpx.delete(f"{BASE}/calendars/{calendar_id}/events/{event_id}", headers=self._h, timeout=20)
        # 404/410 mean it's already gone — that's fine, the end state is what we want.
        if r.status_code not in (200, 204, 404, 410):
            r.raise_for_status()


class EventGone(RuntimeError):
    """The event we tried to update no longer exists (deleted in Google)."""


def build_event_body(session: dict, result: dict | None = None,
                     status: str | None = None) -> dict:
    """Map a planned session to an all-day Calendar event (title + rich description).

    `status` (coach.completion) drives the leading glyph so the calendar reads at a
    glance: the type emoji while planned, ✅ done as planned, ⚠️ done but >20% off
    plan, ❌ ABANDONED (still not done after the grace window). A merely MISSED session
    — the day closed with nothing against it — keeps its type emoji on purpose, because
    it can still be run and marked ✅; only abandonment is a cross. Defaults to
    done-if-there-are-actuals for callers that don't classify. `result` carries the
    Garmin actuals; `delta_line` states the gap for a ⚠️ without guessing at a cause
    (see completion.py)."""
    from ..coach.completion import ABANDONED, DONE, PARTIAL, STATUS_EMOJI
    if status is None:
        status = DONE if result is not None else "planned"
    done = status in (DONE, PARTIAL)          # has actuals to show
    glyph = STATUS_EMOJI.get(status) or event_emoji(session["type"])
    title = f"{glyph} {session['title']}"

    lines: list[str] = []
    if session.get("purpose"):
        lines.append(session["purpose"])
    meta: list[str] = []
    if session.get("distance_km"):
        meta.append(f"{session['distance_km']} km")
    if session.get("duration_min"):
        meta.append(f"{session['duration_min']} min")
    if session.get("target_zone"):
        meta.append(session["target_zone"])
    if session.get("target_pace"):
        meta.append(session["target_pace"])
    if meta:
        lines.append("")
        planned_prefix = done or status == ABANDONED
        lines.append("Planned: " + " · ".join(meta) if planned_prefix else " · ".join(meta))
    if session.get("structure"):
        from ..plan.structure import describe_structure
        lines.append("")
        lines.append("Structure:")
        lines.extend(describe_structure(session["structure"]))
    if done:
        actual: list[str] = []
        if result.get("distance_km"):
            actual.append(f"{result['distance_km']} km")
        if result.get("duration_min"):
            actual.append(f"{int(round(result['duration_min']))} min")
        if result.get("avg_hr"):
            actual.append(f"avg HR {int(result['avg_hr'])}")
        head = "⚠️ Done, off plan" if status == PARTIAL else "✅ Done"
        lines.append("")
        lines.append(head + (f": {' · '.join(actual)}" if actual else ""))
        if status == PARTIAL and result.get("delta_line"):
            lines.append(result["delta_line"])
            lines.append(result.get("deviation_reason") or "Reason: not logged yet.")
    elif status == ABANDONED:
        lines.append("")
        lines.append("❌ Not completed.")
    if session.get("fueling_note") and status == "planned":
        lines.append("")
        lines.append(f"⛽ Fueling: {session['fueling_note']}")
    lines.append("")
    lines.append("— J2H4All (do not edit here; tell the coach to change the plan)")

    d = str(session["date"])[:10]
    return {
        "summary": title,
        "description": "\n".join(lines),
        # All-day event: end.date is exclusive, so it's the day after.
        "start": {"date": d},
        "end": {"date": _next_day(d)},
        "transparency": "transparent",  # doesn't block time as 'busy'
    }


def _next_day(iso_date: str) -> str:
    from datetime import date, timedelta
    d = date.fromisoformat(iso_date)
    return (d + timedelta(days=1)).isoformat()
