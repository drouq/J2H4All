# Garmin workout push — reference

How approved sessions reach the watch. This was originally an implementation plan
with sign-offs and a rollout checklist; it is now a reference for the shipped
behaviour, because the API surface and the lifecycle rules are the parts anyone
touching `garmin/workouts.py` actually needs.

Flag: `GARMIN_WORKOUT_PUSH_ENABLED` (default **false**, so a dev environment can
never write to a real Garmin account by accident).

## What it does

Every approved planned **running** session is pushed to Garmin Connect as a
structured workout **scheduled on its date**, so the watch proposes it that day with
duration/distance and pace or HR-zone targets.

## API

Garmin's private workout service — the same `connectapi` family the app already
reads from. Data calls were never IP-blocked from a datacenter (only auth is; see
[garmin-connectivity-report.md](garmin-connectivity-report.md)), and the diauth
OAuth2 token is minted as the mobile app's client, which writes workouts in normal
use.

| Call | Purpose |
|---|---|
| `POST /workout-service/workout` | create structured workout → `workoutId` |
| `PUT /workout-service/workout/{id}` | update in place |
| `DELETE /workout-service/workout/{id}` | remove |
| `POST /workout-service/schedule/{workoutId}` `{date}` | pin to a date → `scheduleId` |
| `DELETE /workout-service/schedule/{scheduleId}` | unschedule |

## Mapping

One session → one running workout.

| Session field | Workout |
|---|---|
| `title` | workout name, prefixed `J2H4All: ` (identifiable, and greppable for cleanup) |
| `duration_min` / `distance_km` | step end condition (distance preferred when both are set) |
| `target_pace` (e.g. "6:10–6:40/km") | pace-band target — **pace wins over HR when both are present** |
| `target_zone` (e.g. "Z2") | HR-zone target, used when there is no pace |
| `purpose` | workout description |
| `structure` | interval steps; one level of repeat renders as a `RepeatGroupDTO` (`plan/structure.py`) |

**Runs only.** `rest` never pushes. `race` days are skipped — the athlete is racing,
not following a step list.

**`strength` is deliberately skipped**, and it is worth knowing why, because it looks
like an easy win. A pushed gym workout would carry a `workoutId`, which is exactly
the missing signal that stops a *late* gym session auto-marking as done. But a real
strength workout means prescribing exercises, and structured strength programming is
explicitly out of scope (see ARCHITECTURE.md §5). The late-gym gap is not worth
trading a scope rule for.

## Both ends of a workout get a target-free step

A pace or HR target is wrong at both ends of a session for one underlying reason:
**HR lags the effort.** At the start a Z2 target arms at second one and alerts about
ten seconds in — nobody is in Z2 ten seconds into a run. At the end, coming off
threshold work, HR takes minutes to fall through a cooldown ceiling, so the watch
alerts the athlete *for recovering correctly*. A gentler target fixes neither end,
because HR starts below Z1 too.

`_with_lead_in` (every workout) and `_with_ease_out` (any *prescribed* cooldown)
each carve `EASE_MIN` = 5 minutes **out of** the step it belongs to, never added on
top — otherwise every session runs long by design, and that invisible extra volume
never trips the off-plan check. Order matters: ease-out runs before lead-in, or a
session opening on a cooldown gets carved twice.

**Plain easy and long runs get no trailing free step.** Nothing alerts at the end of
an easy run, so it would be a step the athlete looks at on every run forever to
change nothing — and it costs most where it helps least: a 30-minute recovery jog
would become 5 free + 20 in zone + 5 free, stripping the ceiling out of the one
session whose entire purpose is holding it.

This is a **watch-rendering detail only**: `build_workout` never mutates the store's
`structure`, so the plan, the calendar description and the completion check are
unaffected. It lives in the payload builder rather than in a prompt deliberately — a
rule that must hold on every single session shouldn't depend on a model remembering
it.

## Lifecycle

`garmin/workouts.py::reconcile(db)` mirrors the calendar reconcile exactly:

- Future planned run sessions → create-or-update workout + schedule, with stable ids
  carried over on supersede (never duplicate).
- Orphans (superseded, or flipped to rest) → unschedule and delete.
- Past sweep: superseded past → delete; **completed past → leave alone.** Garmin
  auto-links a scheduled workout to the recorded activity, and deleting would erase
  that linkage.

Triggered from the same two places as the calendar reconcile: proposal `approve()`
(bundled and failure-isolated, so approval still lands if Garmin is down, with a
`monitor.py` alert) and the manual "Push plan" button / Telegram `/push`. **No silent
writes to a Garmin account** — the approval gate extends to this surface verbatim.

Delivery caveat: only *scheduling* events trigger delivery to the watch, so reconcile
refreshes the schedule (delete and recreate on the same date) after every content
update. Scheduled workouts appear in the watch's **Training Calendar**, not under
Workouts, and delivery needs a phone-app sync.

Migration **0011** added `Session.garmin_workout_id` and `Session.garmin_schedule_id`,
the same role as `calendar_event_id`.

## Accepted risks

- **Undocumented API.** It could change without notice. Blast radius is contained:
  workouts stop pushing, everything else is unaffected, and the existing cron-failure
  alert path surfaces it.
- **Duplication with Google Calendar.** Same session on two surfaces, but different
  jobs — Google is the planning view on the phone, Garmin is execution on the wrist.
  The `J2H4All: ` prefix keeps the Garmin account tidy.
- **Account writes.** Mistakes are visible in a real Connect account. Mitigated by the
  default-off flag, the name prefix (easy manual cleanup) and stable ids (no duplicate
  spam).
