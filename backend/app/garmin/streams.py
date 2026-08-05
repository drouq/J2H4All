"""Per-second activity streams → durability metrics.

Durability - holding the same effort without drifting - is what decides every
endurance race, whichever format. The per-activity summary can't show it; the
intra-run time-series can. From Garmin's activity `details` stream we derive a compact rollup
(never the raw per-second firehose — the coach reasons over rollups, not dumps):

- **aerobic decoupling** — does HR creep up at constant pace? The first-vs-second-half
  drop in speed-per-beat efficiency. The single best aerobic-durability marker.
- **HR drift (bpm)** — plain average-HR rise, half to half.
- **pacing consistency** — per-km pace coefficient of variation. Athletes often run
  without auto-laps (one lap for the whole run), so we split the distance stream
  ourselves. Low variance late in a run is evenness under fatigue: the metronomic
  repeatability a backyard needs, and the pace discipline a marathon needs.

`compute_stream_metrics` is a pure function of the details payload, so it's testable
and reusable by the live sync and the backfill job alike.
"""

from statistics import mean, pstdev


def _pace(mps: float | None) -> str | None:
    if not mps or mps <= 0:
        return None
    s = round(1000.0 / mps)
    return f"{s // 60}:{s % 60:02d}/km"


def _index_map(details: dict) -> dict:
    return {md.get("key"): md.get("metricsIndex") for md in details.get("metricDescriptors", [])}


def _per_km_pace_cv(samples: list[tuple]) -> tuple[float | None, int]:
    """Per-km pace coefficient of variation from cumulative-distance samples.
    samples: (t_s, hr, speed_mps, cumulative_dist_m). Returns (cv_pct, n_km)."""
    withdist = [(t, d) for (t, _hr, _sp, d) in samples if d is not None]
    if len(withdist) < 2:
        return None, 0
    total = withdist[-1][1]
    n_km = int(total // 1000)
    if n_km < 2:
        return None, n_km
    # Time at each whole-km boundary (first sample past the threshold).
    boundary_t = {}
    for km in range(1, n_km + 1):
        thresh = km * 1000
        for t, d in withdist:
            if d >= thresh:
                boundary_t[km] = t
                break
    times = [boundary_t[km] for km in range(1, n_km + 1) if km in boundary_t]
    if len(times) < 2:
        return None, n_km
    start = withdist[0][0]
    prev = start
    paces = []  # seconds per km
    for t in times:
        paces.append(t - prev)
        prev = t
    paces = [p for p in paces if p > 0]
    if len(paces) < 2:
        return None, n_km
    m = mean(paces)
    return (round(pstdev(paces) / m * 100, 1) if m else None), len(paces)


def compute_stream_metrics(details: dict) -> dict | None:
    """Derive the durability rollup from a Garmin activity `details` payload. Returns
    None when the stream lacks the channels we need (e.g. HR or speed missing)."""
    idx = _index_map(details)
    rows = details.get("activityDetailMetrics") or []
    hr_i, sp_i = idx.get("directHeartRate"), idx.get("directSpeed")
    dist_i = idx.get("sumDistance")
    t_i = idx.get("sumMovingDuration")
    if t_i is None:
        t_i = idx.get("sumElapsedDuration")
    if hr_i is None or sp_i is None or t_i is None or len(rows) < 10:
        return None

    samples: list[tuple] = []
    hi = max(hr_i, sp_i, t_i)
    for r in rows:
        m = r.get("metrics") or []
        if len(m) <= hi:
            continue
        hr, sp, t = m[hr_i], m[sp_i], m[t_i]
        dist = m[dist_i] if (dist_i is not None and dist_i < len(m)) else None
        if hr and sp and sp > 0 and t is not None:
            samples.append((t, hr, sp, dist))
    if len(samples) < 10:
        return None
    samples.sort(key=lambda s: s[0])

    half = len(samples) // 2
    first, second = samples[:half], samples[half:]

    def eff(seg):  # speed per beat — distance covered per heartbeat
        return mean(s[2] for s in seg) / mean(s[1] for s in seg)

    e1, e2 = eff(first), eff(second)
    decoupling = round((e1 - e2) / e1 * 100, 1) if e1 else None
    hr1, hr2 = mean(s[1] for s in first), mean(s[1] for s in second)
    sp1, sp2 = mean(s[2] for s in first), mean(s[2] for s in second)
    pace_cv, n_km = _per_km_pace_cv(samples)

    return {
        "aerobic_decoupling_pct": decoupling,   # + = HR drifted up / efficiency fell
        "hr_drift_bpm": round(hr2 - hr1, 1),
        "pace_cv_pct": pace_cv,                 # lower = more metronomic
        "km_analyzed": n_km,
        "halves": {
            "first": {"avg_hr": round(hr1), "avg_pace": _pace(sp1)},
            "second": {"avg_hr": round(hr2), "avg_pace": _pace(sp2)},
        },
        "sample_count": len(samples),
    }
