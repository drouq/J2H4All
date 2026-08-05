import { useCallback, useEffect, useState, type ReactNode } from "react";
import {
  fetchGoal,
  fetchSetup,
  fetchTelegramLink,
  pairTelegram,
  saveGarminToken,
  saveGoal,
  startSync,
  unpairTelegram,
  type GoalIn,
  type GoalView,
  type SetupStatus,
  type SetupStep,
  type TelegramLink,
} from "./api";

// The race formats the coach has doctrine for (backend coach/formats/). Kept as a
// fixed list rather than free text for the same reason the backend does: a rule
// that must hold on every session shouldn't depend on someone's spelling. The
// backend normalizes anyway, so a mismatch here degrades rather than breaks.
const FORMATS: { key: string; label: string; hint: string }[] = [
  { key: "backyard-ultra", label: "Backyard ultra", hint: "A fixed loop on the hour, every hour, until one runner is left." },
  { key: "trail-ultra", label: "Trail / mountain ultra", hint: "Distance and vertical, with cutoffs and aid stations." },
  { key: "road-ultra", label: "Road / flat ultra", hint: "50 km and beyond on tarmac or track — flat and relentless." },
  { key: "road-marathon", label: "Marathon / half", hint: "A paced road race, organised around a target time." },
];

// Which goal fields mean anything for which format. Asking a marathoner for a lap
// count is how a setup form teaches people the app isn't really for them.
const FIELDS: Record<string, ("loop" | "laps" | "distance" | "vert" | "time")[]> = {
  "backyard-ultra": ["loop", "laps"],
  "trail-ultra": ["distance", "vert", "time"],
  "road-ultra": ["distance", "time"],
  "road-marathon": ["distance", "time"],
};

function num(v: string): number | null {
  const n = Number(v);
  return v.trim() === "" || Number.isNaN(n) ? null : n;
}

/** One step in the guided flow.
 *
 * Done steps collapse to a single line, the current step opens with its controls,
 * and anything can be reopened by clicking it — a wizard that won't let you go back
 * and change your race is worse than the list it replaced. */
function StepRow({
  step, index, open, current, onToggle, children,
}: {
  step: SetupStep; index: number; open: boolean; current: boolean;
  onToggle: () => void; children?: ReactNode;
}) {
  const mark = step.done ? "✓" : step.blocking ? "✗" : "○";
  const tone = step.done ? "ok" : step.blocking ? "err" : "muted";
  return (
    <li className={`wizard-step${current ? " current" : ""}${open ? " open" : ""}`}>
      <button className="wizard-head" onClick={onToggle} aria-expanded={open}>
        <span className={`wizard-mark ${tone}`}>{mark}</span>
        <span className="wizard-n">{index + 1}</span>
        <strong>{step.label}</strong>
        <span className="muted small">{step.detail}</span>
      </button>
      {open && (
        <div className="wizard-body">
          {!step.done && step.action && <p className="muted small">{step.action}</p>}
          {children}
        </div>
      )}
    </li>
  );
}

export default function SetupPanel() {
  const [status, setStatus] = useState<SetupStatus | null>(null);
  const [goal, setGoal] = useState<GoalView | null>(null);
  // Distinguishes "still loading" from "failed to load" from "loaded and empty" —
  // the frontend error-state lesson from the original audit.
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [saved, setSaved] = useState<string | null>(null);

  const [form, setForm] = useState<Record<string, string>>({});
  const [token, setToken] = useState("");
  const [tg, setTg] = useState<TelegramLink | null>(null);
  const [pairCode, setPairCode] = useState<{ code: string; ttl_minutes: number } | null>(null);
  // null = follow the flow (open whatever is next). A click pins one open instead.
  const [pinned, setPinned] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    try {
      const [s, g, link] = await Promise.all([fetchSetup(), fetchGoal(), fetchTelegramLink()]);
      setStatus(s);
      setGoal(g);
      setTg(link);
      setError(null);
      if (g.goal) {
        setForm((f) => ({
          format: f.format ?? g.goal!.format ?? "",
          race_date: f.race_date ?? g.goal!.race_date ?? "",
          loop_km: f.loop_km ?? (g.goal!.loop_km?.toString() ?? ""),
          target_laps: f.target_laps ?? (g.goal!.target_laps?.toString() ?? ""),
          distance_km: f.distance_km ?? (g.goal!.distance_km?.toString() ?? ""),
          elevation_gain_m: f.elevation_gain_m ?? (g.goal!.elevation_gain_m?.toString() ?? ""),
          target_time: f.target_time ?? (g.goal!.target_time ?? ""),
        }));
      }
    } catch (e) {
      setError(String(e));
    }
  }, []);

  useEffect(() => {
    refresh();
  }, [refresh]);

  const run = async (what: () => Promise<string | void>) => {
    setBusy(true);
    setSaved(null);
    try {
      const msg = await what();
      if (msg) setSaved(msg);
      await refresh();
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  };

  const onSaveGoal = () =>
    run(async () => {
      const payload: GoalIn = { format: form.format || undefined, race_date: form.race_date || undefined };
      const shown = FIELDS[form.format] ?? [];
      if (shown.includes("loop")) payload.loop_km = num(form.loop_km ?? "");
      if (shown.includes("laps")) payload.target_laps = num(form.target_laps ?? "");
      if (shown.includes("distance")) payload.distance_km = num(form.distance_km ?? "");
      if (shown.includes("vert")) payload.elevation_gain_m = num(form.elevation_gain_m ?? "");
      if (shown.includes("time")) payload.target_time = form.target_time || null;
      await saveGoal(payload);
      setPinned(null);          // saved — let the flow move on
      return "Race saved.";
    });

  const onSaveToken = () =>
    run(async () => {
      const res = await saveGarminToken(token);
      setToken("");
      setPinned(null);
      return res.env_overrides
        ? "Saved — but GARTH_TOKEN is set in the environment and takes precedence."
        : "Garmin token saved.";
    });

  const onImport = () =>
    run(async () => {
      await startSync("full");
      return "Import started. It pulls about two years and takes a while — the Garmin sync card above shows progress.";
    });

  const onPair = () => run(async () => { setPairCode(await pairTelegram()); });

  const onUnpair = () =>
    run(async () => {
      await unpairTelegram();
      setPairCode(null);
      return "Unlinked. The bot now answers nobody until you pair it again.";
    });

  if (error && !status) {
    return (
      <div className="card">
        <h2>Setup</h2>
        <p className="err">Couldn't load setup status: {error}</p>
      </div>
    );
  }
  if (!status) return <div className="card"><h2>Setup</h2><p className="muted">Loading…</p></div>;

  const shown = FIELDS[form.format] ?? [];
  const placeholder = goal?.goal?.floor_note?.startsWith("PLACEHOLDER");
  const doneCount = status.steps.filter((s) => s.done).length;
  const openKey = pinned ?? status.next;

  // The control that belongs to each step, rendered inline when the step is open.
  const controls: Record<string, ReactNode> = {
    goal: (
      <>
        {placeholder && (
          <p className="muted small">
            This is still the placeholder race a fresh install ships with. The whole plan is
            built backwards from your race date, so set it before drafting anything.
          </p>
        )}
        <label>
          Format
          <select value={form.format ?? ""} onChange={(e) => setForm({ ...form, format: e.target.value })}>
            <option value="">— choose —</option>
            {FORMATS.map((f) => <option key={f.key} value={f.key}>{f.label}</option>)}
          </select>
        </label>
        {form.format && <p className="muted small">{FORMATS.find((f) => f.key === form.format)?.hint}</p>}
        <label>
          Race date
          <input type="date" value={form.race_date ?? ""}
                 onChange={(e) => setForm({ ...form, race_date: e.target.value })} />
        </label>
        {shown.includes("loop") && (
          <label>Loop distance (km)
            <input value={form.loop_km ?? ""} onChange={(e) => setForm({ ...form, loop_km: e.target.value })} /></label>
        )}
        {shown.includes("laps") && (
          <label>Target laps
            <input value={form.target_laps ?? ""} onChange={(e) => setForm({ ...form, target_laps: e.target.value })} /></label>
        )}
        {shown.includes("distance") && (
          <label>Distance (km)
            <input value={form.distance_km ?? ""} onChange={(e) => setForm({ ...form, distance_km: e.target.value })} /></label>
        )}
        {shown.includes("vert") && (
          <label>Climbing (m)
            <input value={form.elevation_gain_m ?? ""} onChange={(e) => setForm({ ...form, elevation_gain_m: e.target.value })} /></label>
        )}
        {shown.includes("time") && (
          <label>Target time
            <input placeholder="e.g. sub-3:30" value={form.target_time ?? ""}
                   onChange={(e) => setForm({ ...form, target_time: e.target.value })} /></label>
        )}
        <button onClick={onSaveGoal} disabled={busy || !form.format || !form.race_date}>
          {busy ? "Saving…" : "Save race"}
        </button>
      </>
    ),
    garmin: (
      <>
        <p className="muted small">
          Garmin blocks datacenter IPs on login, so this can't be done from the server. Run{" "}
          <code>python -m app.garmin.login</code> on your own machine at home, then paste the
          GARTH_TOKEN value here — you don't need to touch any environment variables.
        </p>
        <textarea rows={3} placeholder="Paste the GARTH_TOKEN blob…"
                  value={token} onChange={(e) => setToken(e.target.value)} />
        <button onClick={onSaveToken} disabled={busy || !token.trim()}>
          {busy ? "Saving…" : "Save Garmin token"}
        </button>
      </>
    ),
    history: (
      <>
        <p className="muted small">
          Pulls roughly two years of activities, sleep and recovery. The coach plans off what
          your body has already absorbed, so this is worth more than it looks. It runs in the
          background and takes a while.
        </p>
        <button onClick={onImport} disabled={busy}>
          {busy ? "Starting…" : "Import my history"}
        </button>
      </>
    ),
    telegram: (
      <>
        {!tg?.bot_configured && (
          <p className="muted small">
            No bot yet. Create one with @BotFather, set TELEGRAM_BOT_TOKEN, and come back —
            you won't need to hunt for a chat ID.
          </p>
        )}
        {tg?.bot_configured && tg.from_env && (
          <p className="muted small">
            Linked via TELEGRAM_CHAT_ID in the environment, which always wins. Unset it if you
            want to pair from here instead.
          </p>
        )}
        {tg?.bot_configured && !tg.from_env && tg.bound && (
          <>
            <p className="ok">✓ Linked to one chat. Every other sender is ignored.</p>
            <button onClick={onUnpair} disabled={busy}>Unlink this chat</button>
          </>
        )}
        {tg?.bot_configured && tg.pairable && !tg.bound && (
          <>
            <p className="muted small">
              Get a code, then send it to your bot as a normal message. Until you do, the bot
              answers nobody at all.
            </p>
            {pairCode ? (
              <p>
                Send <strong className="paircode">{pairCode.code}</strong> to your bot.
                Valid once, for {pairCode.ttl_minutes} minutes.
              </p>
            ) : (
              <button onClick={onPair} disabled={busy}>
                {busy ? "Working…" : "Get a pairing code"}
              </button>
            )}
          </>
        )}
      </>
    ),
    calendar: <p className="muted small">Use <a href="#calendar">Connect Google Calendar</a> in the Calendar panel.</p>,
    profile: <p className="muted small">Tell the coach who you are in <a href="#context">the Context panel</a>, or just message your bot.</p>,
    plan: <p className="muted small">Draft one from <a href="#plan">the Plan panel</a> — you review and approve before anything is written.</p>,
  };

  return (
    <div className="card">
      <h2>Setup</h2>
      {error && <p className="err">{error}</p>}
      {saved && <p className="ok">{saved}</p>}

      <p className="muted">
        <strong>{doneCount} of {status.steps.length} done.</strong>{" "}
        {status.complete
          ? "Everything is set up."
          : status.blockers.length > 0
            ? "The ✗ steps block a training plan — the coach won't draft one against a race nobody chose."
            : "The essentials are done. The rest make the coaching better, not possible."}
      </p>
      <div className="wizard-bar" aria-hidden>
        <span style={{ width: `${(doneCount / status.steps.length) * 100}%` }} />
      </div>

      <ol className="wizard">
        {status.steps.map((s, i) => (
          <StepRow key={s.key} step={s} index={i} open={openKey === s.key}
                   current={status.next === s.key}
                   onToggle={() => setPinned(openKey === s.key ? "" : s.key)}>
            {controls[s.key]}
          </StepRow>
        ))}
      </ol>
    </div>
  );
}
