export type Me = { email: string };
export type HeartbeatResult = { count: number; latest_at: string | null };

export type SyncStatus = {
  running: boolean;
  last_run: {
    kind: string;
    status: string;
    started_at: string;
    finished_at: string | null;
    stats: Record<string, unknown> | null;
    detail: string | null;
  } | null;
  last_success_at: string | null;
  staleness_hours: number | null;
  garmin_sync_enabled: boolean;
};

export async function fetchMe(): Promise<Me | null> {
  const res = await fetch("/api/me");
  if (res.status === 401) return null;
  if (!res.ok) throw new Error(`GET /api/me failed: ${res.status}`);
  return res.json();
}

export async function pingHeartbeat(): Promise<HeartbeatResult> {
  const res = await fetch("/api/heartbeat", { method: "POST" });
  if (!res.ok) throw new Error(`POST /api/heartbeat failed: ${res.status}`);
  return res.json();
}

export async function fetchSyncStatus(): Promise<SyncStatus> {
  const res = await fetch("/api/sync/status");
  if (!res.ok) throw new Error(`GET /api/sync/status failed: ${res.status}`);
  return res.json();
}

export async function startSync(mode: "incremental" | "full" = "incremental"): Promise<void> {
  const res = await fetch(`/api/sync?mode=${mode}`, { method: "POST" });
  if (res.status === 409) return; // already running — status polling will show it
  if (!res.ok) throw new Error(`POST /api/sync failed: ${res.status}`);
}

// --- Context (Phase 2) ---

// One extracted/confirmable item. Fields are a superset across kinds; irrelevant ones are null.
export type ContextItem = {
  kind: string;
  summary: string;
  [key: string]: unknown;
};

export type ContextSnapshot = {
  timezone: string;
  athlete: {
    name: string | null;
    pronouns: string;
    age: number | null;
    language: string | null;
    data_caveats: string | null;
    configured: boolean;
  };
  diet: { diet: string; notes: string | null };
  blood_markers: {
    name: string; value: number; unit: string | null; measured_on: string; readings: number;
    flag: "low" | "high" | null; reference: string | null;
  }[];
  availability_windows: { id: number; type: string; start_date: string; end_date: string | null; note: string | null }[];
  injuries: { id: number; body_part: string; status: string; notes: string | null }[];
  preferences: { key: string; value: string }[];
  notes: { id: string; text: string; created_at: string }[];
  recent_lifestyle: { date: string; summary: string | null; flags: Record<string, string> }[];
};

async function jsonOrThrow(res: Response, what: string) {
  if (res.status === 503) throw new Error("AI not configured (ANTHROPIC_API_KEY unset)");
  if (!res.ok) throw new Error(`${what} failed: ${res.status}`);
  return res.json();
}

export async function fetchContext(): Promise<ContextSnapshot> {
  return jsonOrThrow(await fetch("/api/context"), "GET /api/context");
}

export async function extractContext(text: string): Promise<ContextItem[]> {
  const res = await fetch("/api/context/extract", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ text }),
  });
  return (await jsonOrThrow(res, "extract")).items;
}

export async function parseBloods(file: File): Promise<ContextItem[]> {
  const form = new FormData();
  form.append("file", file);
  const res = await fetch("/api/context/bloods/parse", { method: "POST", body: form });
  return (await jsonOrThrow(res, "parse bloods")).items;
}

export async function confirmContext(items: ContextItem[]): Promise<ContextSnapshot> {
  const res = await fetch("/api/context/confirm", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ items }),
  });
  return (await jsonOrThrow(res, "confirm")).context;
}

// --- Goal & plan (Phase 3) ---

export type GoalView = {
  goal: {
    format: string;
    loop_km: number | null;
    target_laps: number | null;
    distance_km: number | null;
    elevation_gain_m: number | null;
    target_time: string | null;
    race_date: string;
    days_to_race: number;
    floor_note: string | null;
    stretch_note: string | null;
  } | null;
  secondary_races: {
    name: string;
    date: string;
    distance_km: number;
    type: string;
    priority: string;
    days_to_race: number;
    note: string | null;
  }[];
};

export type Phase = {
  name: string;
  start_date: string;
  end_date: string;
  focus: string;
  weekly_km_low: number;
  weekly_km_high: number;
  intensity_note: string;
};

export type PlanSession = {
  id?: number;
  date: string;
  type: string;
  title: string;
  duration_min: number | null;
  distance_km: number | null;
  target_zone: string | null;
  target_pace: string | null;
  purpose: string;
  fueling_note: string | null;
  done?: boolean;
  // The completion states the training calendar also shows (backend
  // coach/completion.py): planned / done / partial (>20% off plan) / missed / abandoned.
  status?: SessionStatus;
  deviation?: string | null;
  deviation_reason?: string | null;
};

export type SessionStatus =
  | "planned"
  | "done"
  | "partial"
  | "missed"      // day closed, nothing against it — still liveable, keeps its icon
  | "abandoned";  // past the grace window — the only state that gets a ❌

export type PlanView = {
  macro_plan: { rationale: string; b_race_approach: string; phases: Phase[] } | null;
  upcoming_sessions: PlanSession[];
};

export type Proposal = {
  id: number;
  kind: string;
  status: string;
  origin: string;
  summary: string;
  payload: {
    // All fields optional at runtime: a malformed model output can persist a
    // macro_plan missing `phases` (seen live 2026-07-12) — render defensively.
    macro_plan?: { rationale?: string; b_race_approach?: string; phases?: Phase[] };
    sessions?: PlanSession[];
    change_note?: string;
  };
  created_at: string;
};

export async function fetchGoal(): Promise<GoalView> {
  return jsonOrThrow(await fetch("/api/goal"), "GET /api/goal");
}

export async function fetchPlan(): Promise<PlanView> {
  return jsonOrThrow(await fetch("/api/plan"), "GET /api/plan");
}

export async function fetchProposals(): Promise<Proposal[]> {
  return (await jsonOrThrow(await fetch("/api/proposals"), "GET /api/proposals")).pending;
}

export async function draftPlan(): Promise<Proposal> {
  // Opus generation — can take up to a minute.
  const res = await fetch("/api/plan/draft", { method: "POST" });
  // 409 = setup incomplete. The backend refuses rather than periodizing backwards
  // from a race nobody chose, so surface its message instead of a bare status.
  if (res.status === 409) throw new Error((await res.json()).detail?.message ?? "Finish setup first.");
  if (res.status === 502)
    throw new Error((await res.json()).detail ?? "The coach produced a malformed draft — try again.");
  return jsonOrThrow(res, "draft");
}

export interface SideEffectResult {
  error?: string;
  skipped?: string;
  created?: number;
  updated?: number;
  deleted?: number;
}

export interface ApproveResult {
  plan: PlanView;
  applied?: {
    calendar?: SideEffectResult;
    garmin_workouts?: SideEffectResult;
  };
}

export async function approveProposal(id: number, payload?: object): Promise<ApproveResult> {
  const res = await fetch(`/api/proposals/${id}/approve`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ payload: payload ?? null }),
  });
  if (res.status === 409) throw new Error("Already resolved");
  const data = await jsonOrThrow(res, "approve");
  return { plan: data.plan, applied: data.applied };
}

export async function rejectProposal(id: number): Promise<void> {
  await jsonOrThrow(await fetch(`/api/proposals/${id}/reject`, { method: "POST" }), "reject");
}

export async function reviseProposal(id: number, instruction: string): Promise<Proposal> {
  const res = await fetch(`/api/proposals/${id}/revise`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ instruction }),
  });
  if (res.status === 409) throw new Error("Couldn't revise — it may already be resolved.");
  return jsonOrThrow(res, "revise");
}

// --- Google Calendar (Phase 4) ---

export type CalendarStatus = {
  connected: boolean;
  calendar_id: string | null;
  has_unsynced_sessions: boolean;
  last_calendar_push: string | null;
  last_garmin_push: string | null;
};

export type SyncCounts = {
  created?: number;
  updated?: number;
  deleted?: number;
  completed_marked?: number;
  skipped?: string;
  error?: string;
};

// One button, two surfaces: Google Calendar events + Garmin scheduled workouts.
export type CalendarSyncResult = {
  calendar: SyncCounts;
  garmin: SyncCounts;
};

export async function fetchCalendarStatus(): Promise<CalendarStatus> {
  return jsonOrThrow(await fetch("/api/calendar/status"), "GET /api/calendar/status");
}

export async function syncCalendar(): Promise<CalendarSyncResult> {
  const res = await fetch("/api/calendar/sync", { method: "POST" });
  if (res.status === 409) throw new Error("Calendar not connected");
  return jsonOrThrow(res, "calendar sync");
}

export async function disconnectCalendar(): Promise<void> {
  await jsonOrThrow(await fetch("/api/calendar/disconnect", { method: "POST" }), "disconnect");
}

// Coaching chat lives on Telegram, not the web. The web panel was removed
// 2026-07-13 and the /api/coach/* endpoints with it on 2026-08-03 — the bot calls
// coach/chat.py directly, so those routes had no caller at all.

// --- Trends (Phase 6, §19) ---

export type LoadBand = {
  load: number | null;
  target_min: number | null;
  target_max: number | null;
  status: "under" | "in_range" | "over" | null;
};

export type TrainingLoadBalance = {
  as_of: string | null;
  feedback: string | null;
  anaerobic: LoadBand;
  aerobic_low: LoadBand;
  aerobic_high: LoadBand;
} | null;

export type HeatAcclimation = {
  as_of: string | null;
  heat_acclimation_pct: number | null;
  previous_pct: number | null;
  trend: string | null;
} | null;

export type Trends = {
  weekly_volume: {
    week: string;
    actual_km: number | null;
    planned_km: number | null;
    actual_min: number | null;
    planned_min: number | null;
  }[];
  training_load_balance: TrainingLoadBalance;
  heat_acclimation: HeatAcclimation;
  acwr: { week: string; ratio: number | null }[];
  recovery: { date: string; hrv: number | null; rhr: number | null }[];
  vo2max: { date: string; vo2max: number }[];
  durability: { date: string; decoupling_pct: number | null; pace_cv_pct: number | null }[];
  blood_markers: Record<string, { date: string; value: number; unit: string | null }[]>;
};

export async function fetchTrends(): Promise<Trends> {
  return jsonOrThrow(await fetch("/api/trends"), "trends");
}

// --- Drive backup (Phase 6, §15) ---

export type BackupStatus = { connected: boolean; drive_authorized: boolean; last_export: string | null };

export async function fetchBackupStatus(): Promise<BackupStatus> {
  return jsonOrThrow(await fetch("/api/backup/status"), "backup status");
}

export async function runBackup(): Promise<{ file_id: string; name: string }> {
  const res = await fetch("/api/backup/run", { method: "POST" });
  if (res.status === 409) throw new Error((await res.json()).detail ?? "Reconnect Google to enable backups");
  return jsonOrThrow(res, "backup run");
}

// --- First-run setup (ROADMAP §3) ---

export type SetupStep = {
  key: string;
  label: string;
  done: boolean;
  detail: string;
  action: string;
  blocking: boolean;
};

export type SetupStatus = {
  steps: SetupStep[];
  complete: boolean;
  blockers: string[];
  next: string | null;
};

export type GoalIn = {
  format?: string;
  race_date?: string;
  loop_km?: number | null;
  target_laps?: number | null;
  distance_km?: number | null;
  elevation_gain_m?: number | null;
  target_time?: string | null;
};

export async function fetchSetup(): Promise<SetupStatus> {
  return jsonOrThrow(await fetch("/api/setup"), "setup status");
}

export async function saveGoal(goal: GoalIn): Promise<unknown> {
  const res = await fetch("/api/setup/goal", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(goal),
  });
  if (res.status === 422) throw new Error((await res.json()).detail ?? "Invalid goal");
  return jsonOrThrow(res, "save goal");
}

export async function saveGarminToken(token: string): Promise<{ saved: boolean; env_overrides: boolean }> {
  const res = await fetch("/api/setup/garmin-token", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ token }),
  });
  if (res.status === 422) throw new Error((await res.json()).detail ?? "Invalid token");
  return jsonOrThrow(res, "save garmin token");
}
