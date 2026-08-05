import { useEffect, useState } from "react";
import { fetchTrends, type LoadBand, type Trends } from "./api";

const W = 380;
const H = 96;
const PAD = 6;

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
function shortDate(iso: string): string {
  const [, m, d] = iso.split("-").map(Number);
  return `${MONTHS[m - 1]} ${d}`;
}

function fmt(n: number): string {
  return Number.isInteger(n) ? String(n) : n.toFixed(1);
}

function lastTwo(values: (number | null)[]): [number | null, number | null] {
  const nums = values.filter((v): v is number => v != null);
  const n = nums.length;
  return [n > 0 ? nums[n - 1] : null, n > 1 ? nums[n - 2] : null];
}

function scale(vals: number[], lo: number, hi: number, invert = false) {
  const min = Math.min(...vals);
  const max = Math.max(...vals);
  const span = max - min || 1;
  return (v: number) => {
    const t = (v - min) / span;
    return invert ? lo + t * (hi - lo) : hi - t * (hi - lo);
  };
}

function Line({ points, color }: { points: (number | null)[]; color: string }) {
  const nums = points.filter((p): p is number => p != null);
  if (nums.length < 2) return <p className="muted small">Not enough data yet.</p>;
  const y = scale(nums, PAD, H - PAD);
  const n = points.length;
  const x = (i: number) => PAD + (i / (n - 1)) * (W - 2 * PAD);
  let d = "";
  points.forEach((p, i) => {
    if (p == null) return;
    d += `${d && !d.endsWith("M") ? "L" : "M"} ${x(i).toFixed(1)} ${y(p).toFixed(1)} `;
  });
  return (
    <svg viewBox={`0 0 ${W} ${H}`} className="chart" preserveAspectRatio="none">
      <path d={d} fill="none" stroke={color} strokeWidth={2} />
    </svg>
  );
}

// Time-on-feet per week: actual (Garmin recorded minutes) vs planned (prescribed
// minutes). The plan is duration-based now, so minutes compare honestly on both
// sides; km would only exist for actuals.
function VolumeBars({ data, max }: { data: Trends["weekly_volume"]; max: number }) {
  const bw = (W - 2 * PAD) / data.length;
  return (
    <svg viewBox={`0 0 ${W} ${H}`} className="chart" preserveAspectRatio="none">
      {data.map((d, i) => {
        const x = PAD + i * bw;
        // clamp to the capped axis so an outlier week fills the chart rather than
        // overflowing the SVG viewport.
        const a = d.actual_min != null ? (Math.min(d.actual_min, max) / max) * (H - 2 * PAD) : 0;
        const p = d.planned_min != null ? (Math.min(d.planned_min, max) / max) * (H - 2 * PAD) : 0;
        return (
          <g key={i}>
            {d.planned_min != null && (
              <rect x={x + 1} y={H - PAD - p} width={bw - 2} height={p} fill="#334155" />
            )}
            {d.actual_min != null && (
              <rect x={x + 1} y={H - PAD - a} width={bw - 2} height={a} fill="#2563eb" />
            )}
          </g>
        );
      })}
    </svg>
  );
}

// Chart frame with y-axis min/max labels and an x-axis date range, so a chart is
// legible on its own (each line is self-scaled, so the numbers matter).
function ChartFrame({
  title,
  note,
  min,
  max,
  unit,
  xStart,
  xEnd,
  children,
}: {
  title: string;
  note?: string;
  min?: number;
  max?: number;
  unit?: string;
  xStart?: string;
  xEnd?: string;
  children: React.ReactNode;
}) {
  const u = unit ?? "";
  return (
    <div className="trend">
      <div className="trend-h">
        <strong>{title}</strong>
        {note && <span className="muted small">{note}</span>}
      </div>
      <div className="chart-wrap">
        {max != null && <span className="axis-y axis-max muted small">{fmt(max)}{u}</span>}
        {min != null && <span className="axis-y axis-min muted small">{fmt(min)}{u}</span>}
        {children}
      </div>
      {(xStart || xEnd) && (
        <div className="axis-x muted small">
          <span>{xStart}</span>
          <span>{xEnd}</span>
        </div>
      )}
    </div>
  );
}

function LineSeries({
  title,
  values,
  dates,
  color,
  unit,
  hint,
}: {
  title: string;
  values: (number | null)[];
  dates: string[];
  color: string;
  unit?: string;
  hint?: string;
}) {
  const nums = values.filter((v): v is number => v != null);
  if (nums.length < 2) {
    return (
      <ChartFrame title={title}>
        <p className="muted small">Not enough data yet.</p>
      </ChartFrame>
    );
  }
  const min = Math.min(...nums);
  const max = Math.max(...nums);
  const firstIdx = values.findIndex((v) => v != null);
  let lastIdx = firstIdx;
  values.forEach((v, i) => {
    if (v != null) lastIdx = i;
  });
  return (
    <ChartFrame
      title={title}
      note={hint}
      min={min}
      max={max}
      unit={unit}
      xStart={shortDate(dates[firstIdx])}
      xEnd={shortDate(dates[lastIdx])}
    >
      <Line points={values} color={color} />
    </ChartFrame>
  );
}

function Kpi({
  label,
  values,
  unit,
  goodDown,
}: {
  label: string;
  values: (number | null)[];
  unit: string;
  goodDown?: boolean;
}) {
  const [latest, prior] = lastTwo(values);
  if (latest == null) return null;
  let arrow = "";
  let cls = "";
  if (prior != null && latest !== prior) {
    const up = latest > prior;
    arrow = up ? "▲" : "▼";
    cls = (goodDown ? !up : up) ? "up-good" : "up-bad";
  }
  return (
    <div className="kpi">
      <div className="kpi-label muted small">{label}</div>
      <div className="kpi-value">
        {fmt(latest)}
        <span className="kpi-unit">{unit}</span>
        {arrow && <span className={`kpi-delta ${cls}`}>{arrow}</span>}
      </div>
    </div>
  );
}

// Garmin's monthly intensity distribution vs its own target bands — an 80/20 read.
function LoadRow({ label, band }: { label: string; band: LoadBand }) {
  const { load, target_min, target_max, status } = band;
  if (load == null || target_min == null || target_max == null) {
    return (
      <div className="lb-row">
        <div className="lb-label">{label}</div>
        <div className="muted small">no data</div>
      </div>
    );
  }
  const axisMax = Math.max(target_max, load) * 1.15 || 1;
  const p = (v: number) => (v / axisMax) * 100;
  const color = status === "over" ? "#f87171" : status === "under" ? "#f59e0b" : "#4ade80";
  return (
    <div className="lb-row">
      <div className="lb-label">{label}</div>
      <div className="lb-track">
        <div className="lb-band" style={{ left: `${p(target_min)}%`, width: `${p(target_max - target_min)}%` }} />
        <div className="lb-marker" style={{ left: `${p(load)}%`, background: color }} />
      </div>
      <div className="lb-val muted small" style={{ color }}>
        {Math.round(load)}
        <span className="lb-target"> ({Math.round(target_min)}–{Math.round(target_max)})</span>
      </div>
    </div>
  );
}

function LoadBalance({ lb }: { lb: NonNullable<Trends["training_load_balance"]> }) {
  const phrase = (lb.feedback || "").replace(/_/g, " ").toLowerCase();
  return (
    <div className="trend">
      <div className="trend-h">
        <strong>Training load balance (80/20)</strong>
        <span className="muted small">Garmin monthly · load vs target</span>
      </div>
      <LoadRow label="Low aerobic" band={lb.aerobic_low} />
      <LoadRow label="High aerobic" band={lb.aerobic_high} />
      <LoadRow label="Anaerobic" band={lb.anaerobic} />
      {phrase && <div className="muted small lb-feedback">Garmin read: {phrase}</div>}
    </div>
  );
}

function HeatAccl({ h }: { h: NonNullable<Trends["heat_acclimation"]> }) {
  const pct = h.heat_acclimation_pct ?? 0;
  const delta = h.previous_pct != null ? pct - h.previous_pct : null;
  const trend = (h.trend || "").replace(/_/g, " ").toLowerCase();
  return (
    <div className="trend">
      <div className="trend-h">
        <strong>Heat acclimation</strong>
        <span className="muted small">
          {trend}
          {delta ? ` · ${delta > 0 ? "+" : ""}${delta}%` : ""}
        </span>
      </div>
      <div className="heat-bar">
        <div className="heat-fill" style={{ width: `${Math.min(100, pct)}%` }} />
        <span className="heat-pct">{pct}%</span>
      </div>
    </div>
  );
}

// Nth-percentile of the positive values (linear, no interpolation) — used to cap the
// volume axis so a single outlier week doesn't squash the rest.
function pctile(vals: number[], p: number): number {
  const s = vals.filter((v) => v > 0).sort((a, b) => a - b);
  if (!s.length) return 0;
  return s[Math.min(s.length - 1, Math.floor(p * (s.length - 1)))];
}

export default function TrendsPanel() {
  const [t, setT] = useState<Trends | null>(null);
  const [failed, setFailed] = useState(false);
  useEffect(() => {
    fetchTrends().then(setT).catch(() => setFailed(true));
  }, []);
  // Distinguish load-failure from loading — a bare `return null` rendered the panel as
  // an indistinguishable blank void on any fetch error (PanelBoundary only catches
  // render errors, not rejected fetches).
  if (failed)
    return <div className="card"><h2>Trends</h2><p className="err">Couldn't load your trends just now — try refreshing.</p></div>;
  if (!t) return <div className="card"><h2>Trends</h2><p className="muted">Loading…</p></div>;

  const last = <T,>(a: T[]): T | undefined => a[a.length - 1];
  const recDates = t.recovery.map((r) => r.date);
  const durDates = t.durability.map((d) => d.date);
  // Cap the volume axis at the 90th-percentile week so one outlier (a peak/race week
  // like May's UTA block) doesn't flatten every normal week to a sliver; taller weeks
  // clip at the top of the chart.
  const volMaxMin = Math.max(
    pctile(t.weekly_volume.flatMap((d) => [d.actual_min ?? 0, d.planned_min ?? 0]), 0.9),
    1,
  );
  // Blood values live in the Context panel ("What the coach knows"). Here we
  // only chart markers with a real trend (2+ readings) — a single blood panel
  // gives one reading per marker, which would render 70+ empty "not enough
  // data" charts. This stays empty until a second panel lands.
  const trendingBloods = Object.keys(t.blood_markers).filter(
    (name) => t.blood_markers[name].length >= 2,
  );

  return (
    <div className="card">
      <h2>Trends</h2>

      <div className="kpi-strip">
        <Kpi label="HRV" values={t.recovery.map((r) => r.hrv)} unit=" ms" />
        <Kpi label="Rest HR" values={t.recovery.map((r) => r.rhr)} unit=" bpm" goodDown />
        <Kpi label="VO₂max" values={t.vo2max.map((v) => v.vo2max)} unit="" />
        <Kpi label="Decoupling" values={t.durability.map((d) => d.decoupling_pct)} unit="%" goodDown />
        <Kpi label="Pace CV" values={t.durability.map((d) => d.pace_cv_pct)} unit="%" goodDown />
      </div>

      {t.training_load_balance && <LoadBalance lb={t.training_load_balance} />}
      {t.heat_acclimation?.heat_acclimation_pct != null && <HeatAccl h={t.heat_acclimation} />}

      <ChartFrame
        title="Weekly time on feet vs plan"
        note="blue = actual · grey = planned"
        min={0}
        max={+(volMaxMin / 60).toFixed(1)}
        unit=" h"
        xStart={t.weekly_volume[0] ? shortDate(t.weekly_volume[0].week) : undefined}
        xEnd={last(t.weekly_volume) ? shortDate(last(t.weekly_volume)!.week) : undefined}
      >
        <VolumeBars data={t.weekly_volume} max={volMaxMin} />
      </ChartFrame>

      <LineSeries title="HRV" values={t.recovery.map((r) => r.hrv)} dates={recDates} color="#4ade80" unit=" ms" />
      <LineSeries title="Resting HR" values={t.recovery.map((r) => r.rhr)} dates={recDates} color="#f59e0b" unit=" bpm" />
      <LineSeries
        title="Load — acute:chronic (volume proxy)"
        values={t.acwr.map((a) => a.ratio)}
        dates={t.acwr.map((a) => a.week)}
        color="#a855f7"
        hint="≈1.0 steady · >1.3 ramping fast"
      />
      <LineSeries title="VO₂max" values={t.vo2max.map((v) => v.vo2max)} dates={t.vo2max.map((v) => v.date)} color="#38bdf8" />
      <LineSeries
        title="Aerobic decoupling"
        values={t.durability.map((d) => d.decoupling_pct)}
        dates={durDates}
        color="#f472b6"
        unit="%"
        hint="lower = better"
      />
      <LineSeries
        title="Pace consistency (per-km CV)"
        values={t.durability.map((d) => d.pace_cv_pct)}
        dates={durDates}
        color="#22d3ee"
        unit="%"
        hint="lower = metronomic"
      />

      {trendingBloods.map((name) => (
        <LineSeries
          key={name}
          title={name}
          values={t.blood_markers[name].map((m) => m.value)}
          dates={t.blood_markers[name].map((m) => m.date)}
          color="#fb7185"
          unit={last(t.blood_markers[name])?.unit ? ` ${last(t.blood_markers[name])!.unit}` : ""}
        />
      ))}
    </div>
  );
}
