import { Component, useCallback, useEffect, useRef, useState, type ReactNode } from "react";
import BackupPanel from "./Backup";
import CalendarPanel from "./Calendar";
import ContextPanel from "./Context";
import PlanPanel from "./Plan";
import TrendsPanel from "./Trends";
import {
  fetchMe,
  fetchSyncStatus,
  pingHeartbeat,
  startSync,
  type HeartbeatResult,
  type Me,
  type SyncStatus,
} from "./api";

type State =
  | { phase: "loading" }
  | { phase: "anonymous" }
  | { phase: "authed"; me: Me; heartbeat: HeartbeatResult | null; error: string | null };

// One crashing panel must not blank the whole app (a malformed proposal payload
// did exactly that on 2026-07-12): catch render errors per panel and show an
// inline error card instead.
class PanelBoundary extends Component<{ name: string; children: ReactNode }, { error: string | null }> {
  state: { error: string | null } = { error: null };
  static getDerivedStateFromError(e: unknown) {
    return { error: String(e) };
  }
  render() {
    if (this.state.error) {
      return (
        <div className="card">
          <h2>{this.props.name}</h2>
          <p className="err">This panel hit a rendering error: {this.state.error}</p>
        </div>
      );
    }
    return this.props.children;
  }
}

function SyncCard() {
  const [status, setStatus] = useState<SyncStatus | null>(null);
  const [error, setError] = useState<string | null>(null);
  const pollTimer = useRef<number | null>(null);

  const refresh = useCallback(async () => {
    try {
      const s = await fetchSyncStatus();
      setStatus(s);
      setError(null);
      if (s.running && pollTimer.current === null) {
        pollTimer.current = window.setInterval(async () => {
          const next = await fetchSyncStatus().catch(() => null);
          if (next) setStatus(next);
          if (next && !next.running && pollTimer.current !== null) {
            window.clearInterval(pollTimer.current);
            pollTimer.current = null;
          }
        }, 4000);
      }
    } catch (e) {
      setError(String(e));
    }
  }, []);

  useEffect(() => {
    refresh();
    return () => {
      if (pollTimer.current !== null) window.clearInterval(pollTimer.current);
    };
  }, [refresh]);

  const onSyncNow = async () => {
    try {
      await startSync("incremental");
      await refresh();
    } catch (e) {
      setError(String(e));
    }
  };

  const last = status?.last_run;
  const homeOnly = status?.garmin_sync_enabled === false;
  return (
    <div className="card">
      <h2>Garmin sync</h2>
      {error && <p className="err">{error}</p>}
      {status && (
        <p className="muted">
          {status.running
            ? "Sync running…"
            : last
              ? `Last: ${last.kind} — ${last.status}${
                  status.staleness_hours !== null ? ` · ${status.staleness_hours}h since last success` : ""
                }`
              : "No syncs yet."}
        </p>
      )}
      {/* GARMIN_SYNC_ENABLED=false. The old copy here said syncing "runs from the
          home PC twice daily", which was true only of a superseded deploy shape and
          would send a new self-hoster looking for a schedule that doesn't exist.
          Only the one-time login bootstrap needs a home network — see SETUP.md §4. */}
      {homeOnly && (
        <p className="muted">
          Garmin sync is turned off (GARMIN_SYNC_ENABLED=false), so no data is being
          pulled. Set it to true once you've bootstrapped your Garmin token.
        </p>
      )}
      {last?.status === "failure" && last.detail && (
        <p className="err">{last.detail.split("\n")[0]}</p>
      )}
      <button onClick={onSyncNow} disabled={homeOnly || (status?.running ?? false)}>
        {homeOnly ? "Sync disabled" : "Sync now"}
      </button>
    </div>
  );
}

export default function App() {
  const [state, setState] = useState<State>({ phase: "loading" });

  useEffect(() => {
    (async () => {
      try {
        const me = await fetchMe();
        if (!me) {
          setState({ phase: "anonymous" });
          return;
        }
        try {
          const heartbeat = await pingHeartbeat();
          setState({ phase: "authed", me, heartbeat, error: null });
        } catch (e) {
          setState({ phase: "authed", me, heartbeat: null, error: String(e) });
        }
      } catch (e) {
        setState({ phase: "anonymous" });
      }
    })();
  }, []);

  return (
    <main>
      <h1>J2H4All</h1>
      {/* Deliberately says nothing about a specific race. This header renders on
          every panel, including before any goal has loaded — the live goal (laps,
          date, countdown) belongs in the Plan panel, which reads it from the store. */}
      <p className="muted">Your own endurance coach</p>

      {state.phase === "loading" && <div className="card">Loading…</div>}

      {state.phase === "anonymous" && (
        <div className="card">
          <p>Sign in to continue. One athlete only.</p>
          <a className="button" href="/auth/login">
            Sign in with Google
          </a>
        </div>
      )}

      {state.phase === "authed" && (
        <>
          <div className="card">
            <p>
              Signed in as <strong>{state.me.email}</strong>
            </p>
            {state.heartbeat ? (
              <p className="ok">✓ API + database connected</p>
            ) : (
              <p className="err">DB heartbeat failed: {state.error}</p>
            )}
            <a className="muted" href="/auth/logout">
              Sign out
            </a>
          </div>
          <SyncCard />
          <nav className="tabs">
            <a href="#plan">Plan</a>
            <a href="#trends">Trends</a>
            <a href="#calendar">Calendar</a>
            <a href="#context">Context</a>
            <a href="#backup">Backup</a>
          </nav>
          <section id="plan"><PanelBoundary name="The plan"><PlanPanel /></PanelBoundary></section>
          <section id="trends"><PanelBoundary name="Trends"><TrendsPanel /></PanelBoundary></section>
          <section id="calendar"><PanelBoundary name="Calendar"><CalendarPanel /></PanelBoundary></section>
          <section id="context"><PanelBoundary name="Context"><ContextPanel /></PanelBoundary></section>
          <section id="backup"><PanelBoundary name="Backup"><BackupPanel /></PanelBoundary></section>
        </>
      )}
    </main>
  );
}
