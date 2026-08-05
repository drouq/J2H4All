# Garmin Workout Push — Implementation Plan

**Status: APPROVED & IMPLEMENTED (2026-07-10).** Signed off with three calls:
(1) pace preferred over HR when both present; (2) ONE manual button, two result lines;
(3) interval step structures built NOW (the coach can prescribe `structure` on any
session — see `plan/structure.py`), not deferred. Steps 0–2 of the rollout below are
done (probe passed first try, incl. a separate PUT-update probe; builder payload
verified live end-to-end). Remaining: flip `GARMIN_WORKOUT_PUSH_ENABLED=true` on the
Render **web** service and verify on the watch (steps 3–4).

(This was a scope addition on top of the PRD; workout push is not among the §18
exclusions, but it is a new side-effect surface, so it gets the same approval-gate
treatment as Google Calendar and one PRD-level sign-off — this doc.)

## Goal

Every approved planned running session is also pushed to Garmin Connect as a
**structured workout scheduled on its date**, so the watch proposes it that day
with duration/distance and pace/HR-zone targets — most valuable for the September
backyard-sim block (repeated metronomic loops with target alerts).

## How (API)

Garmin's private workout service, same connectapi family we already read from
(data calls were never IP-blocked from Render; the diauth OAuth2 token is minted
as `GARMIN_CONNECT_MOBILE_ANDROID_DI`, the mobile app's client, which writes
workouts in normal use — write scope is expected to work, verified in step 0):

- `POST /workout-service/workout` — create structured workout → `workoutId`
- `PUT  /workout-service/workout/{id}` — update in place
- `DELETE /workout-service/workout/{id}` — remove
- `POST /workout-service/schedule/{workoutId}` `{date}` — pin to a calendar date → `scheduleId`
- `DELETE /workout-service/schedule/{scheduleId}` — unschedule

Payload shapes mapped from python-garminconnect / garmin-workouts reference
implementations; confirmed live in step 0 before any code lands.

## Mapping (v1, deliberately simple)

One session → one running workout with a single main step:

| Session field | Workout |
|---|---|
| `title` | workout name, prefixed `J2H4All: ` (identifiable + greppable for cleanup) |
| `duration_min` / `distance_km` | step end condition (prefer distance when both set) |
| `target_pace` (e.g. "6:10–6:40/km") | pace-band target |
| `target_zone` (e.g. "Z2") | HR-zone target (used when no pace) |
| `purpose` | workout description |

- **Runs only.** `rest` never pushes; `strength` skipped in v1 (different workout
  schema, low value); `race` days skipped (he's racing, not following a step).
  - *Re-examined 2026-08-03 and deliberately kept.* Tempting now, because he DOES record
    gym on the watch (11 `strength_training` activities in 90 days) and a pushed gym
    workout would carry a `workoutId` — which is exactly the missing signal that stops a
    LATE gym session auto-marking. But a real strength workout means prescribing
    exercises, and **PRD §18 rules out structured strength programming**; and the
    late-gym gap measures as not currently hurting. Not worth trading a
    hard rule for.
- ~~No warmup/cooldown steps in v1~~ — **superseded 2026-08-03.** Coach-prescribed
  interval structure ships (`plan/structure.py` → RepeatGroupDTO), and both ends of a
  pushed workout now get a **target-free step**, carved out of the step it belongs to
  (`workouts._with_lead_in` / `_with_ease_out`, `EASE_MIN` = 5): the opening of every
  workout, and the opening of any *prescribed* cooldown. HR lags the effort at both
  ends — it starts below the floor and finishes above the ceiling — so the watch
  alerted him ~10 s into a run and again the moment a Z1 cooldown began. A gentler
  target is not a fix either way. **Plain easy/long runs get no trailing free step**
  (asked and declined — nothing alerts at the end of an easy run). See ARCHITECTURE.md for
  why it's carved out rather than added on top.

## Store + lifecycle (mirrors the calendar pattern exactly)

- Migration **0011**: `Session.garmin_workout_id`, `Session.garmin_schedule_id`
  (nullable strings), same role as `calendar_event_id`.
- New `garmin/workouts.py::reconcile(db)`:
  - future planned run sessions → create-or-update workout + schedule (stable IDs,
    never duplicate; carry-over on supersede like `apply_sessions` does for events);
  - orphans (superseded / flipped to rest) → unschedule + delete;
  - past sweep: superseded past → delete; completed past → **leave alone** (Garmin
    auto-links a scheduled workout to the recorded activity on its own — deleting
    would erase that linkage).
- Called from the **same two triggers** as the calendar reconcile: proposal
  `approve()` (bundled, failure-isolated so approval still lands if Garmin is down,
  with a `monitor.py` alert) and a manual "sync to Garmin" action. No silent writes
  to the Garmin account — the §2.3 hard rule extends to this surface verbatim.
- Feature flag `GARMIN_WORKOUT_PUSH_ENABLED` (default **false**) so deploys are
  inert until the live verification below passes.

## Verification / rollout order

1. **Step 0 (residential or Render one-off, no app code):** standalone probe script
   (like `refresh_probe.py`) that creates one throwaway `J2H4All: probe` workout,
   schedules it tomorrow, confirms it appears in Connect + on the watch, then
   deletes it. Proves auth scope + payload shape before anything is built.
2. Implement (migration, `workouts.py`, approve bundling, flag, manual button).
3. Flag on in prod → push a **single** upcoming session, verify on watch, verify
   update-in-place by approving a revision, verify delete on supersede.
4. Full plan push. Watch the next weekly-review approval do the whole cycle.

## Risks (accepted / mitigated)

- **Undocumented API** — could change without notice. Blast radius: workouts stop
  pushing; calendar + everything else unaffected. Alert via the existing cron/beat
  failure path.
- **Duplication with Google Calendar** — same workout on two surfaces. Different
  jobs though: Google = planning view on the phone, Garmin = execution on the
  wrist. The `J2H4All: ` prefix keeps Garmin tidy.
- **Account writes** — mistakes are visible in a real Connect account. Mitigated
  by the flag, the step-0 probe, the prefix (easy manual cleanup), and stable IDs
  (no duplicate spam).

**Effort:** ~1 focused session including live verification. **Cost:** zero LLM
calls — purely deterministic plumbing.

## Open questions — ALL ANSWERED, kept for the record

1. ~~Pace target vs HR target when a session carries both?~~ **Pace wins**
   (`_target_fields`), the athlete's call.
2. ~~One manual trigger or two?~~ **One button, two result lines** — shipped as the web
   "Push plan" button and Telegram `/push`, and the approval card reports both surfaces
   separately (including a *skipped* Garmin push, so a flag drifting off can't look like
   a successful watch update).
3. ~~Any appetite for interval step structures now?~~ **Shipped** — `plan/structure.py`
   defines the schema all four session generators emit, and `build_workout` renders one
   level of repeat as a RepeatGroupDTO.

Nothing here is open. Live gaps are tracked in ROADMAP.md, not in this file.
