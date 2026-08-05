import { useEffect, useRef, useState } from "react";
import {
  confirmContext,
  extractContext,
  fetchContext,
  parseBloods,
  type ContextItem,
  type ContextSnapshot,
} from "./api";

export default function ContextPanel() {
  const [snap, setSnap] = useState<ContextSnapshot | null>(null);
  const [text, setText] = useState("");
  const [pending, setPending] = useState<ContextItem[] | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const fileRef = useRef<HTMLInputElement>(null);

  const refresh = () => fetchContext().then(setSnap).catch((e) => setError(String(e)));
  useEffect(() => {
    refresh();
  }, []);

  const run = async (fn: () => Promise<ContextItem[]>) => {
    setBusy(true);
    setError(null);
    try {
      const items = await fn();
      if (items.length === 0) setError("Nothing to capture from that.");
      setPending(items.length ? items : null);
    } catch (e) {
      setError(String(e).replace("Error: ", ""));
    } finally {
      setBusy(false);
    }
  };

  const onConfirm = async () => {
    if (!pending) return;
    setBusy(true);
    try {
      const next = await confirmContext(pending);
      setSnap(next);
      setPending(null);
      setText("");
    } catch (e) {
      setError(String(e).replace("Error: ", ""));
    } finally {
      setBusy(false);
    }
  };

  const drop = (i: number) => {
    if (!pending) return;
    const next = pending.filter((_, idx) => idx !== i);
    setPending(next.length ? next : null);
  };

  return (
    <div className="card">
      <h2>Tell the coach</h2>
      <p className="muted">
        Bloods, treadmill trips, niggles, where you are this week — say it in plain words; the coach
        files it after you confirm.
      </p>
      <textarea
        rows={2}
        value={text}
        placeholder="e.g. Ferritin came back at 28 last week; I'm in Tokyo on the treadmill until Friday"
        onChange={(e) => setText(e.target.value)}
      />
      <div className="row">
        <button disabled={busy || !text.trim()} onClick={() => run(() => extractContext(text))}>
          {busy ? "…" : "Capture"}
        </button>
        <button className="secondary" disabled={busy} onClick={() => fileRef.current?.click()}>
          Upload blood PDF
        </button>
        <input
          ref={fileRef}
          type="file"
          accept="application/pdf"
          style={{ display: "none" }}
          onChange={(e) => {
            const f = e.target.files?.[0];
            if (f) run(() => parseBloods(f));
            e.target.value = "";
          }}
        />
      </div>

      {error && <p className="err">{error}</p>}

      {pending && (
        <div className="review">
          <p className="muted">Confirm what I should save:</p>
          {pending.map((it, i) => (
            <div className="chip" key={i}>
              <span>
                <strong>{it.kind}</strong> — {it.summary}
              </span>
              <button className="x" onClick={() => drop(i)} title="Drop">
                ✕
              </button>
            </div>
          ))}
          <div className="row">
            <button disabled={busy} onClick={onConfirm}>
              Save {pending.length}
            </button>
            <button className="secondary" disabled={busy} onClick={() => setPending(null)}>
              Discard
            </button>
          </div>
        </div>
      )}

      {snap && <Snapshot snap={snap} />}
    </div>
  );
}

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

function Snapshot({ snap }: { snap: ContextSnapshot }) {
  const bloodDates = snap.blood_markers.map((m) => m.measured_on).sort();
  const latestBlood = bloodDates[bloodDates.length - 1];
  const bloodLabel = latestBlood ? `${MONTHS[Number(latestBlood.split("-")[1]) - 1]} ${latestBlood.split("-")[0]}` : "";
  return (
    <div className="snapshot">
      <h3>What the coach knows</h3>
      <p className="muted">
        Timezone: <strong>{snap.timezone}</strong> · Diet: {snap.diet.diet}
        {snap.diet.notes ? ` (${snap.diet.notes.replace(/\n/g, "; ")})` : ""}
      </p>
      {snap.blood_markers.length > 0 && (
        <details className="bloods">
          <summary>
            <span className="label">Bloods</span>{" "}
            <span className="muted small">
              {snap.blood_markers.length} markers{bloodLabel ? ` · ${bloodLabel}` : ""}
            </span>
          </summary>
          <p className="bloods-list">
            {snap.blood_markers.map((m, i) => (
              <span key={m.name}>
                {i > 0 ? " · " : ""}
                <span className={m.flag ? "blood-flag" : undefined}>
                  {m.name} {m.value}
                  {m.unit ? " " + m.unit : ""}
                  {m.readings > 1 ? ` (${m.readings})` : ""}
                  {m.flag ? ` ⚠ ${m.flag}${m.reference ? ` (ref ${m.reference})` : ""}` : ""}
                </span>
              </span>
            ))}
          </p>
          <p className="muted small">
            Flags compare to a typical reference range, not your lab's — mention them to your doctor.
          </p>
        </details>
      )}
      {snap.recent_lifestyle.length > 0 && (
        <details className="bloods">
          <summary>
            <span className="label">Lifestyle log</span>{" "}
            <span className="muted small">last {snap.recent_lifestyle.length} days</span>
          </summary>
          <div className="lifestyle-list">
            {snap.recent_lifestyle.map((l) => {
              const flags = Object.entries(l.flags);
              return (
                <div key={l.date} className="muted small">
                  <strong>{l.date.slice(5)}</strong> {l.summary || "—"}
                  {flags.length > 0 && (
                    <span> · {flags.map(([k, v]) => `${k}: ${v}`).join(" · ")}</span>
                  )}
                </div>
              );
            })}
          </div>
        </details>
      )}
      {snap.availability_windows.length > 0 && (
        <p>
          <span className="label">Windows</span>{" "}
          {snap.availability_windows
            .map((w) => `${w.type} ${w.start_date}→${w.end_date ?? "open"}`)
            .join(" · ")}
        </p>
      )}
      {snap.injuries.length > 0 && (
        <p>
          <span className="label">Injuries</span>{" "}
          {snap.injuries.map((i) => `${i.body_part} (${i.status})`).join(" · ")}
        </p>
      )}
      {snap.preferences.length > 0 && (
        <p>
          <span className="label">Prefs</span>{" "}
          {snap.preferences.map((p) => `${p.key}: ${p.value}`).join(" · ")}
        </p>
      )}
    </div>
  );
}
