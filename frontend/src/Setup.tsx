import { useCallback, useEffect, useState } from "react";
import {
  fetchGoal,
  fetchSetup,
  fetchTelegramLink,
  pairTelegram,
  saveGarminToken,
  saveGoal,
  unpairTelegram,
  type GoalIn,
  type GoalView,
  type SetupStatus,
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

  const onSaveGoal = async () => {
    setBusy(true);
    setSaved(null);
    try {
      const payload: GoalIn = { format: form.format || undefined, race_date: form.race_date || undefined };
      const shown = FIELDS[form.format] ?? [];
      if (shown.includes("loop")) payload.loop_km = num(form.loop_km ?? "");
      if (shown.includes("laps")) payload.target_laps = num(form.target_laps ?? "");
      if (shown.includes("distance")) payload.distance_km = num(form.distance_km ?? "");
      if (shown.includes("vert")) payload.elevation_gain_m = num(form.elevation_gain_m ?? "");
      if (shown.includes("time")) payload.target_time = form.target_time || null;
      await saveGoal(payload);
      setSaved("Race saved.");
      await refresh();
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  };

  const onSaveToken = async () => {
    setBusy(true);
    setSaved(null);
    try {
      const res = await saveGarminToken(token);
      setToken("");
      setSaved(
        res.env_overrides
          ? "Saved — but GARTH_TOKEN is set in the environment and takes precedence."
          : "Garmin token saved.",
      );
      await refresh();
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  };

  const onPair = async () => {
    setBusy(true);
    setSaved(null);
    try {
      setPairCode(await pairTelegram());
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  };

  const onUnpair = async () => {
    setBusy(true);
    setSaved(null);
    try {
      await unpairTelegram();
      setPairCode(null);
      setSaved("Unlinked. The bot now answers nobody until you pair it again.");
      await refresh();
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  };

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

  return (
    <div className="card">
      <h2>Setup</h2>
      {error && <p className="err">{error}</p>}
      {saved && <p className="ok">{saved}</p>}

      {status.complete ? (
        <p className="ok">✓ Everything is set up.</p>
      ) : (
        <p className="muted">
          {status.blockers.length > 0
            ? "Some steps still block a training plan — the coach won't draft one against a race nobody chose."
            : "The essentials are done. The rest make the coaching better, not possible."}
        </p>
      )}

      <ul className="setup-steps">
        {status.steps.map((s) => (
          <li key={s.key} className={s.done ? "ok" : s.blocking ? "err" : "muted"}>
            <strong>
              {s.done ? "✓" : s.blocking ? "✗" : "○"} {s.label}
            </strong>{" "}
            {s.detail}
            {!s.done && s.action && <div className="muted small">{s.action}</div>}
          </li>
        ))}
      </ul>

      <h3>Your race</h3>
      {placeholder && (
        <p className="muted small">
          This is still the placeholder race a fresh install ships with. The entire plan is built
          backwards from your race date, so set it before drafting anything.
        </p>
      )}
      <label>
        Format
        <select
          value={form.format ?? ""}
          onChange={(e) => setForm({ ...form, format: e.target.value })}
        >
          <option value="">— choose —</option>
          {FORMATS.map((f) => (
            <option key={f.key} value={f.key}>{f.label}</option>
          ))}
        </select>
      </label>
      {form.format && (
        <p className="muted small">{FORMATS.find((f) => f.key === form.format)?.hint}</p>
      )}
      <label>
        Race date
        <input
          type="date"
          value={form.race_date ?? ""}
          onChange={(e) => setForm({ ...form, race_date: e.target.value })}
        />
      </label>
      {shown.includes("loop") && (
        <label>
          Loop distance (km)
          <input value={form.loop_km ?? ""} onChange={(e) => setForm({ ...form, loop_km: e.target.value })} />
        </label>
      )}
      {shown.includes("laps") && (
        <label>
          Target laps
          <input value={form.target_laps ?? ""} onChange={(e) => setForm({ ...form, target_laps: e.target.value })} />
        </label>
      )}
      {shown.includes("distance") && (
        <label>
          Distance (km)
          <input value={form.distance_km ?? ""} onChange={(e) => setForm({ ...form, distance_km: e.target.value })} />
        </label>
      )}
      {shown.includes("vert") && (
        <label>
          Climbing (m)
          <input
            value={form.elevation_gain_m ?? ""}
            onChange={(e) => setForm({ ...form, elevation_gain_m: e.target.value })}
          />
        </label>
      )}
      {shown.includes("time") && (
        <label>
          Target time
          <input
            placeholder="e.g. sub-3:30"
            value={form.target_time ?? ""}
            onChange={(e) => setForm({ ...form, target_time: e.target.value })}
          />
        </label>
      )}
      <button onClick={onSaveGoal} disabled={busy || !form.format || !form.race_date}>
        {busy ? "Saving…" : "Save race"}
      </button>

      <h3>Garmin token</h3>
      <p className="muted small">
        Garmin blocks datacenter IPs on login, so this can't be done from the server. Run{" "}
        <code>python -m app.garmin.login</code> on your own machine at home, then paste the
        GARTH_TOKEN value here — you don't need to touch any environment variables.
      </p>
      <textarea
        rows={3}
        placeholder="Paste the GARTH_TOKEN blob…"
        value={token}
        onChange={(e) => setToken(e.target.value)}
      />
      <button onClick={onSaveToken} disabled={busy || !token.trim()}>
        {busy ? "Saving…" : "Save Garmin token"}
      </button>

      <h3>Telegram</h3>
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
              Send <strong style={{ fontSize: "1.4em", letterSpacing: "0.15em" }}>{pairCode.code}</strong>{" "}
              to your bot. Valid once, for {pairCode.ttl_minutes} minutes.
            </p>
          ) : (
            <button onClick={onPair} disabled={busy}>
              {busy ? "Working…" : "Get a pairing code"}
            </button>
          )}
        </>
      )}
    </div>
  );
}
