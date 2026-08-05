import { useEffect, useState } from "react";
import {
  approveProposal,
  draftPlan,
  fetchGoal,
  fetchPlan,
  fetchProposals,
  rejectProposal,
  reviseProposal,
  type GoalView,
  type PlanSession,
  type PlanView,
  type Proposal,
  type SessionStatus,
} from "./api";

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

// Human label per race format (backend coach/formats/). This panel used to say
// "days to the backyard" with a lap count for every athlete, which reads as
// nonsense to a marathoner.
const RACE_LABEL: Record<string, string> = {
  "backyard-ultra": "the backyard",
  "trail-ultra": "your trail ultra",
  "road-ultra": "your ultra",
  "road-marathon": "your marathon",
  generic: "your race",
};

// Browser-local today as YYYY-MM-DD (plan dates are local, and toISOString would
// shift a UTC+8 evening back a day). Used to flag today's session.
function localToday(): string {
  const d = new Date();
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
}

// Monday-of-week key for grouping (parse from string parts to avoid TZ drift).
function weekStart(iso: string): string {
  const [y, m, d] = iso.split("-").map(Number);
  const dt = new Date(y, m - 1, d);
  dt.setDate(dt.getDate() - ((dt.getDay() + 6) % 7)); // 0 = Monday
  return `${dt.getFullYear()}-${String(dt.getMonth() + 1).padStart(2, "0")}-${String(dt.getDate()).padStart(2, "0")}`;
}

function shortDate(iso: string): string {
  const [, m, d] = iso.split("-").map(Number);
  return `${MONTHS[m - 1]} ${d}`;
}

type SessionRow = {
  date: string;
  type: string;
  title: string;
  purpose: string;
  fueling_note: string | null;
  done?: boolean;
  // Same completion states the training calendar shows (backend coach/completion.py).
  status?: SessionStatus;
  deviation?: string | null;
  deviation_reason?: string | null;
};

// ✓ hit as planned · ⚠️ done but >20% off · ❌ abandoned (still undone past the grace
// window). Nothing for `planned`, the common case, and nothing for `missed` either:
// a missed session keeps its plain look because it can still be run and turn into a ✓
// — only abandonment is a cross. See coach/completion.py.
function statusBadge(s: SessionRow) {
  if (s.status === "partial") return <span className="warn small">⚠️ off plan</span>;
  if (s.status === "abandoned") return <span className="muted small">❌ not done</span>;
  if (s.status === "done" || s.done) return <span className="ok small">✓ done</span>;
  return null;
}

function groupByWeek<T extends { date: string }>(sessions: T[]): { week: string; items: T[] }[] {
  const groups: { week: string; items: T[] }[] = [];
  const idx: Record<string, number> = {};
  for (const s of sessions) {
    const w = weekStart(s.date);
    if (idx[w] === undefined) {
      idx[w] = groups.length;
      groups.push({ week: w, items: [] });
    }
    groups[idx[w]].items.push(s);
  }
  return groups;
}

function sessionMeta(s: PlanSession): string {
  const parts: string[] = [];
  if (s.duration_min) parts.push(`${s.duration_min} min`);
  if (s.distance_km) parts.push(`${s.distance_km} km`);
  if (s.target_zone) parts.push(s.target_zone);
  if (s.target_pace) parts.push(s.target_pace);
  return parts.join(" · ");
}

export default function PlanPanel() {
  const [goal, setGoal] = useState<GoalView | null>(null);
  const [plan, setPlan] = useState<PlanView | null>(null);
  const [proposal, setProposal] = useState<Proposal | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [loadFailed, setLoadFailed] = useState(false);

  const load = async () => {
    setLoadFailed(false);
    setGoal(await fetchGoal().catch(() => null));
    // Distinguish a load FAILURE from a genuinely-empty plan: swallowing the error
    // into null would render the "No plan yet — Draft my plan" CTA over an existing
    // plan and invite a needless ~1-min redraft that supersedes it on approval.
    try {
      setPlan(await fetchPlan());
    } catch {
      setPlan(null);
      setLoadFailed(true);
    }
    const pend = await fetchProposals().catch(() => []);
    // Show any pending proposal — the onboarding draft, or a weekly-review /
    // red-flag change. Onboarding drafts take priority if several exist.
    setProposal(pend.find((p) => p.kind === "onboarding_draft") ?? pend[0] ?? null);
  };
  useEffect(() => {
    load();
  }, []);

  const onDraft = async () => {
    setBusy(true);
    setError(null);
    try {
      setProposal(await draftPlan());
    } catch (e) {
      setError(String(e).replace("Error: ", ""));
    } finally {
      setBusy(false);
    }
  };

  const onApprove = async () => {
    if (!proposal) return;
    setBusy(true);
    setError(null);
    try {
      const { plan: next, applied } = await approveProposal(proposal.id);
      setPlan(next);
      setProposal(null);
      // Approval pushed to calendar/Garmin — tell the (already-mounted) Calendar panel
      // to refresh its "last push" / unsynced state instead of showing stale values.
      window.dispatchEvent(new Event("j2h4all:plan-changed"));
      // The plan is saved either way, but a failed calendar/Garmin push must be
      // visible (degrade loudly) — the Telegram card shows this; the web didn't.
      const issues: string[] = [];
      if (applied?.calendar?.error) issues.push(`calendar sync failed (${applied.calendar.error})`);
      if (applied?.garmin_workouts?.error)
        issues.push(`Garmin workout push failed (${applied.garmin_workouts.error})`);
      if (issues.length)
        setError(`Plan saved, but ${issues.join(" and ")} — use "Push plan" on the Calendar tab to retry.`);
    } catch (e) {
      setError(String(e).replace("Error: ", ""));
    } finally {
      setBusy(false);
    }
  };

  const onReject = async () => {
    if (!proposal) return;
    setBusy(true);
    setError(null);
    try {
      await rejectProposal(proposal.id);
      setProposal(null);
    } catch (e) {
      // Already resolved elsewhere (409) or transient — surface it and refresh so the
      // card reflects reality instead of throwing an unhandled rejection silently.
      setError(String(e).replace("Error: ", ""));
      await load();
    } finally {
      setBusy(false);
    }
  };

  // Correct the draft in conversation (PRD §11/§14): redraft per free-text feedback.
  const onRevise = async (instruction: string) => {
    if (!proposal) return;
    setBusy(true);
    setError(null);
    try {
      setProposal(await reviseProposal(proposal.id, instruction));
    } catch (e) {
      setError(String(e).replace("Error: ", ""));
    } finally {
      setBusy(false);
    }
  };

  const g = goal?.goal;
  return (
    <div className="card">
      <h2>The plan</h2>
      {g && (
        <div className="goalbar">
          <span className="big">{g.days_to_race}</span>
          {/* Describe the race the athlete actually entered. This used to say "the
              backyard" with a lap count for everyone, which read as nonsense to a
              marathoner. Falls back to the bare date when nothing is filled in. */}
          <span className="muted">
            days to {RACE_LABEL[g.format] ?? "your race"}
            {g.target_laps ? ` — ${g.target_laps} laps` : ""}
            {g.distance_km ? ` — ${g.distance_km} km` : ""}
            {g.elevation_gain_m ? ` / ${g.elevation_gain_m} m` : ""}
            {g.target_time ? ` — target ${g.target_time}` : ""}
            {`, ${g.race_date}`}
          </span>
          {goal!.secondary_races.map((r) => (
            <div key={r.name} className="muted small">
              {r.name}: {r.distance_km} km {r.type} · {r.date} ({r.days_to_race}d, {r.priority}-race)
            </div>
          ))}
        </div>
      )}

      {error && <p className="err">{error}</p>}

      {/* Load failed → don't masquerade as an empty plan; offer a retry. */}
      {loadFailed && (
        <div className="review">
          <p className="err">
            Couldn't load your plan just now — it's still saved. Check your connection and try again.
          </p>
          <button disabled={busy} onClick={load}>
            Retry
          </button>
        </div>
      )}

      {/* No plan yet + no pending draft → offer to generate (only when the load succeeded) */}
      {!loadFailed && !plan?.macro_plan && !proposal && (
        <div className="review">
          <p className="muted">
            No plan yet. The coach can draft a full periodized plan to race day from your Garmin history
            and context — you review and approve before anything is saved.
          </p>
          <button disabled={busy} onClick={onDraft}>
            {busy ? "Drafting (Opus, ~1 min)…" : "Draft my plan"}
          </button>
        </div>
      )}

      {/* Pending proposal → review + approve/edit/reject (PRD §11) */}
      {proposal && (
        <ProposalReview
          proposal={proposal}
          busy={busy}
          onApprove={onApprove}
          onReject={onReject}
          onRevise={onRevise}
        />
      )}

      {/* Active plan */}
      {plan?.macro_plan && !proposal && <ActivePlan plan={plan} onRedraft={onDraft} busy={busy} />}
    </div>
  );
}

function ProposalReview({
  proposal,
  busy,
  onApprove,
  onReject,
  onRevise,
}: {
  proposal: Proposal;
  busy: boolean;
  onApprove: () => void;
  onReject: () => void;
  onRevise: (instruction: string) => void;
}) {
  const [editing, setEditing] = useState(false);
  const [instruction, setInstruction] = useState("");
  const mp = proposal.payload.macro_plan;
  const phases = mp?.phases ?? [];
  const sessions = proposal.payload.sessions ?? [];
  const changeNote = proposal.payload.change_note;
  const originLabel =
    proposal.origin === "weekly_review" ? "Weekly review" :
    proposal.origin === "red_flag" ? "🚩 Red flag" : "Draft";

  const submitRevision = () => {
    const text = instruction.trim();
    if (!text) return;
    onRevise(text);
    setInstruction("");
    setEditing(false);
  };

  return (
    <div className="review">
      <p className="muted">
        <span className="badge">{originLabel}</span> Proposed — nothing is saved until you approve.
      </p>
      <p>{proposal.summary}</p>
      {mp?.b_race_approach && (
        <p className="ff">
          <strong>B-race:</strong> {mp.b_race_approach}
        </p>
      )}
      {phases.length > 0 && <PhaseList phases={phases} />}
      {mp && phases.length === 0 && (
        <p className="err">
          This draft came back without its phase breakdown (malformed model output) — reject it and
          draft again.
        </p>
      )}
      {changeNote && (
        <p className="ff">
          <strong>What changed:</strong> {changeNote}
        </p>
      )}
      <h3>{mp ? `First ${sessions.length} days` : `${sessions.length} sessions`}</h3>
      <SessionList sessions={sessions} today={localToday()} />

      {editing && (
        <div className="revise">
          <textarea
            placeholder="Tell the coach what to change, e.g. 'move the long run to Sunday and keep it easier'"
            value={instruction}
            disabled={busy}
            onChange={(e) => setInstruction(e.target.value)}
            rows={2}
          />
          <div className="row">
            <button disabled={busy || !instruction.trim()} onClick={submitRevision}>
              {busy ? "Redrafting…" : "Redraft"}
            </button>
            <button className="secondary" disabled={busy} onClick={() => setEditing(false)}>
              Cancel
            </button>
          </div>
        </div>
      )}

      {!editing && (
        <div className="row">
          <button disabled={busy} onClick={onApprove}>
            Approve &amp; save
          </button>
          <button className="secondary" disabled={busy} onClick={() => setEditing(true)}>
            Suggest a change
          </button>
          <button className="secondary" disabled={busy} onClick={onReject}>
            Reject
          </button>
        </div>
      )}
    </div>
  );
}

function ActivePlan({ plan, onRedraft, busy }: { plan: PlanView; onRedraft: () => void; busy: boolean }) {
  const today = localToday();
  return (
    <div className="review">
      <TodayCard sessions={plan.upcoming_sessions} today={today} />
      <p className="muted">{plan.macro_plan!.rationale}</p>
      <PhaseList phases={plan.macro_plan!.phases ?? []} />
      <h3>Upcoming sessions</h3>
      <SessionList sessions={plan.upcoming_sessions} today={today} />
      <div className="row">
        <button className="secondary" disabled={busy} onClick={onRedraft}>
          {busy ? "…" : "Re-draft"}
        </button>
      </div>
    </div>
  );
}

// Daily-glance callout: what's on today (there can be two — e.g. a gym session
// sharing a long-run day), or a clear "nothing scheduled".
function TodayCard({ sessions, today }: { sessions: PlanSession[]; today: string }) {
  const todays = sessions.filter((s) => s.date === today);
  if (todays.length === 0) {
    return <div className="today-card empty muted">No session scheduled today.</div>;
  }
  return (
    <div className="today-card">
      <div className="today-label">Today · {shortDate(today)}</div>
      {todays.map((s, i) => {
        const meta = sessionMeta(s);
        return (
          <div key={i} className="today-session">
            <div className="today-title">
              <strong>{s.title}</strong>
              {statusBadge(s)}
            </div>
            {meta && <div className="muted small">{meta}</div>}
            {s.status === "partial" && s.deviation && (
              <div className="warn small">
                {s.deviation}
                {s.deviation_reason ? ` — ${s.deviation_reason}` : " · reason not logged yet"}
              </div>
            )}
            <div className="muted small">{s.purpose}</div>
            {s.fueling_note && <div className="fuel small">⛽ {s.fueling_note}</div>}
          </div>
        );
      })}
    </div>
  );
}

function PhaseList({ phases }: { phases: { name: string; start_date: string; end_date: string; focus: string; weekly_km_low: number; weekly_km_high: number }[] }) {
  return (
    <div className="phases">
      {phases.map((p, i) => (
        <div className="phase" key={i}>
          <div className="phase-h">
            <strong>{p.name}</strong>
            <span className="muted small">
              {p.start_date} → {p.end_date} · {p.weekly_km_low}–{p.weekly_km_high} km/wk
            </span>
          </div>
          <div className="muted small">{p.focus}</div>
        </div>
      ))}
    </div>
  );
}

function SessionList({ sessions, today }: { sessions: SessionRow[]; today?: string }) {
  const weeks = groupByWeek(sessions);
  return (
    <div className="sessions">
      {weeks.map((g) => (
        <div className="week" key={g.week}>
          <div className="week-h muted small">Week of {shortDate(g.week)}</div>
          {g.items.map((s, i) => {
            const isToday = today != null && s.date === today;
            return (
              <details key={i} className={`session t-${s.type}${isToday ? " today" : ""}`}>
                <summary>
                  <span className="sdate">{s.date.slice(5)}</span>
                  <span className="stitle">{s.title}</span>
                  {isToday && <span className="badge today-badge">Today</span>}
                  {statusBadge(s)}
                </summary>
                {s.status === "partial" && s.deviation && (
                  <div className="warn small">
                    {s.deviation}
                    {s.deviation_reason ? ` — ${s.deviation_reason}` : " · reason not logged yet"}
                  </div>
                )}
                <div className="muted small">{s.purpose}</div>
                {s.fueling_note && <div className="fuel small">⛽ {s.fueling_note}</div>}
              </details>
            );
          })}
        </div>
      ))}
    </div>
  );
}
