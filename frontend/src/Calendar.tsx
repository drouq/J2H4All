import { useEffect, useState } from "react";
import {
  disconnectCalendar,
  fetchCalendarStatus,
  syncCalendar,
  type CalendarStatus,
  type CalendarSyncResult,
} from "./api";

export default function CalendarPanel() {
  const [status, setStatus] = useState<CalendarStatus | null>(null);
  const [result, setResult] = useState<CalendarSyncResult | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [failed, setFailed] = useState(false);
  const justConnected = new URLSearchParams(window.location.search).get("calendar") === "connected";

  const load = async () => {
    // Distinguish a fetch FAILURE from "not connected" — swallowing the error to null
    // showed the "Connect Google Calendar" onboarding even when already connected.
    try {
      setStatus(await fetchCalendarStatus());
      setFailed(false);
    } catch {
      setFailed(true);
    }
  };
  useEffect(() => {
    load();
    // Refresh when a plan is approved elsewhere (Plan panel) so last-push/unsynced
    // state doesn't go stale until a full reload.
    const onChanged = () => load();
    window.addEventListener("j2h4all:plan-changed", onChanged);
    return () => window.removeEventListener("j2h4all:plan-changed", onChanged);
  }, []);

  const onSync = async () => {
    setBusy(true);
    setError(null);
    try {
      setResult(await syncCalendar());
      await load();
    } catch (e) {
      setError(String(e).replace("Error: ", ""));
    } finally {
      setBusy(false);
    }
  };

  const onDisconnect = async () => {
    setBusy(true);
    try {
      await disconnectCalendar();
      setResult(null);
      await load();
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="card">
      <h2>Google Calendar &amp; Garmin</h2>

      {failed && (
        <p className="err">Couldn't load calendar status just now — try refreshing.</p>
      )}

      {!failed && !status?.connected && (
        <>
          <p className="muted">
            Push your training sessions to a dedicated <strong>J2H4All Training</strong> calendar — one
            event per workout, with the full session in the description — and, when enabled, as
            scheduled structured workouts on your Garmin watch. The app only ever touches its own
            calendar, and never writes without your say-so.
          </p>
          {justConnected && <p className="err">Connection didn't complete — try again.</p>}
          <a className="button" href="/auth/calendar/connect">
            Connect Google Calendar
          </a>
        </>
      )}

      {status?.connected && (
        <>
          <p className="ok">✓ Connected — writing to the J2H4All Training calendar.</p>
          <p className="muted small">
            Last calendar push: {status.last_calendar_push ?? "never"}
            <br />
            Last watch push: {status.last_garmin_push ?? "never"}
          </p>
          {status.has_unsynced_sessions && (
            <p className="muted small">You have sessions not yet on the calendar.</p>
          )}
          {error && <p className="err">{error}</p>}
          {result && (
            <p className="muted small">
              Calendar: {result.calendar.created ?? 0} added · {result.calendar.updated ?? 0} updated ·{" "}
              {result.calendar.deleted ?? 0} removed
              {(result.calendar.completed_marked ?? 0) > 0 && <> · {result.calendar.completed_marked} marked done</>}
              .
              <br />
              Garmin workouts:{" "}
              {result.garmin.error
                ? `sync issue — ${result.garmin.error}`
                : result.garmin.skipped
                  ? "off"
                  : `${result.garmin.created ?? 0} added · ${result.garmin.updated ?? 0} updated · ${result.garmin.deleted ?? 0} removed.`}
            </p>
          )}
          <div className="row">
            <button disabled={busy} onClick={onSync}>
              {busy ? "Syncing…" : "Push plan"}
            </button>
            <button className="secondary" disabled={busy} onClick={onDisconnect}>
              Disconnect
            </button>
          </div>
          <p className="muted small">
            Approving a plan change also updates the calendar (and Garmin workouts, when enabled)
            automatically. This button is for pushing an already-approved plan.
          </p>
        </>
      )}
    </div>
  );
}
