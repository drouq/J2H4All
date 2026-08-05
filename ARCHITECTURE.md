# Architecture & operational learnings

Design principles, system layout, and the things that were expensive to learn. If you're
about to change something here, the "Hard-won learnings" section is the part worth reading
first — several of those bugs took days to diagnose and are not obvious from the code.

---

## 1. Design posture

These principles override convenience wherever they conflict with it.

1. **Claude is the coach, not a rules engine.** Plan generation, adaptation, strategy and
   fueling live in the model reasoning over structured state. Deterministic logic
   (rollups, load calculations, red-flag thresholds) exists as *tools the model consults*,
   never as the brain. There is no periodization engine that "really" decides the plan.

2. **The store is truth; the model reasons over clean state.** A structured context store
   and plan schema keep the model grounded. It reasons over persisted, normalized data and
   rolled-up summaries — never raw dumps re-fetched each turn. Full multi-year history is
   stored, but the coach sees weekly/monthly rollups plus recent detail. This is what keeps
   even the expensive model calls affordable.

3. **Nothing side-effectful happens without approval.** *(Hard rule.)* Every Google
   Calendar write, every Garmin workout push, and every revision to an already-rendered
   plan session is **proposed** and executed **only on explicit approval**. Enforced by the
   `proposal` table: `approve()` is idempotent (409 on double-apply) and atomic (CAS
   claim), and writes the store *and* the calendar/watch together.

4. **Degrade loudly, never silently.** When a data source is stale or broken, the coach
   says so and adjusts its posture — it never makes confident decisions on stale data
   without flagging it. A generation step that produces an empty plan raises rather than
   shipping an empty proposal.

5. **Single user, hard gate.** *(Hard rule.)* Exactly one person gets in; everyone else
   bounces. Web is Google OAuth against a one-email allowlist (`ALLOWED_GOOGLE_EMAIL`);
   Telegram is locked to one chat ID (`TELEGRAM_CHAT_ID`) and silently ignores every other
   sender. There is no accounts system, no multi-tenancy, no sharing. **This is why
   self-hosting works so well: one deployment per person, complete data isolation by
   construction.**

6. **The coach is medically conservative.** *(Hard rule.)* It flags marker trends and says
   "worth discussing with a doctor". It never diagnoses and never prescribes dosages.

7. **Store UTC, render local.** The scheduler fires on the athlete's *local* clock. The
   timezone is set conversationally ("I'm in London") and follows them when they travel —
   never hardcode an offset. Any clock time shown to the athlete or handed to a prompt goes
   through `fmt_local`.

---

## 2. Layout

### Backend (`backend/app/`)

| Path | Role |
|---|---|
| `main.py` | App, routers, SPA serving, production boot guard |
| `config.py` | `Settings`, `DEFAULT_MODELS`, `model_for(task)` |
| `models.py` | Every SQLAlchemy model |
| `auth.py` | Google OAuth allowlist, `current_user`, dev bypass |
| `telegram.py` | Webhook, inline cards, Markdown→HTML rendering |
| `llm.py` | `call_tool` (forced tool use + streaming + salvage), `call_text` |
| `monitor.py` | Cooldown-throttled alerting; silence means healthy |
| `garmin/` | `client`, `sync`, `oauth2` (diauth refresh), `workouts`, `streams`, `weather`, diagnostics |
| `context/` | `store`, `extract` (conversational capture), `pdf`, `bloods` |
| `plan/` | `generate` (macro + sessions), `store` (apply/link/read), `proposals` (create/approve/reject), `summary` |
| `coach/` | `doctrine` ← **the coaching brain**, `signals`, `completion`, `postrun`, `redflag`, `weekly`, `brief`, `debrief`, `schedule`, `chat`, `adapt` |
| `calendar/` | `oauth`, `client`, `sync` (reconcile), `routes` |

**To tune the coach, edit `coach/doctrine.py` (shared) or `coach/formats/` (per race
type).** Every LLM system prompt composes from `full_doctrine(db)` (heavy surfaces) or
`compact_doctrine(db)` (frequent, cheap surfaces), so the coaching knowledge lives in one
place instead of drifting across prompts.

The coaching brain is split deliberately. `doctrine.py` holds the **shared endurance
core** — the aerobic engine, the ~10%/week ramp, decoupling as the durability KPI, gut
training, the medical line. That is the same for a first marathon and a 24-hour backyard,
and it is the part that has been tuned against real data. `coach/formats/` holds the ~30%
that genuinely differs: what the race demands, how it is executed, and the handful of
training sessions the format requires. `Goal.format` selects one.

Why layered rather than one doctrine per format: writing four standalone doctrines is how
you get four mediocre coaches, because the shared reasoning gets re-derived badly and
drifts apart. Formats are a **fixed registry, not free text** — a rule that must hold on
every session shouldn't depend on a model inventing the doctrine at onboarding, the same
reasoning that keeps the model tier in `config.py`. An unknown format resolves to
`generic`, which coaches general endurance and asks what the race is.

### Frontend (`frontend/src/`)

`App.tsx` with a sticky section nav and an error boundary; panels `Plan`, `Trends`,
`Calendar`, `Context`, `Backup`; `api.ts` for typed fetch. Charts are hand-rolled inline
SVG — no chart library. `tsconfig` targets ES2020, so no `Array.at()`.

### Model tiering

Tasks map to models in `config.DEFAULT_MODELS`, overridable at runtime via a
`MODEL_MAP_JSON` environment variable with no code change.

- **Opus tier** — heavy, infrequent, high-stakes: macro plan generation, weekly review,
  coaching chat. This is where coaching quality lives.
- **Sonnet tier** — frequent and lighter: morning brief, check-in parsing, context
  extraction, post-run reads, PDF parsing, red-flag checks.

A test asserts the tiering so a heavy surface can't silently drop to the cheap tier.

> **Set the model default in `config.py`, not in a host environment variable.** The
> weekly review runs on the *cron* service, not the web service, so an env-var override
> has to be set on multiple services by hand — and a missed one leaves that surface a
> model generation behind, silently. A code default applies everywhere at once and is
> reviewable in a diff. `MODEL_MAP_JSON` stays as the escape hatch for experiments and
> for rolling back without a deploy.

---

## 3. Deployment

`render.neon.yaml` declares a web service plus three crons:

| Service | Schedule | Job |
|---|---|---|
| web | always on | FastAPI + SPA; runs `alembic upgrade head` at boot |
| daily sync | `0 1 * * *` | Garmin incremental sync, then calendar reconcile |
| tick | `0 * * * *` | Hourly; fires the local-clock coaching beats |
| export | `0 6 1 * *` | Monthly JSON backup to Google Drive |

**Keep the tick hourly.** Coarsening it to save database compute breaks the weekly review:
with whole-hour UTC offsets, an every-2-hours cron only ever samples even (or odd) local
hours, so a 23:00 local beat is never reached.

---

## 4. Hard-won learnings

Each of these is a real incident. Don't re-break them.

### Garmin will not let you log in from a cloud host

Garmin's Cloudflare front door **blocks datacenter IP ranges outright** on its auth
endpoints. It is not a TLS-fingerprint problem, and it is not specific to one host —
CI runners are blocked identically. TLS impersonation, alternative client libraries and
headless browsers were all tested and all fail, because the verdict is rendered at the
network layer.

**What works:** only the OAuth1→2 *exchange* is blocked. The OAuth2 **refresh grant** on a
different host (`diauth.garmin.com`) is not, and it rolls forever — roughly 23-hour access
tokens with a fresh ~30-day refresh token each call. So you bootstrap once from a
residential IP (`python -m app.garmin.login`), and the deployment refreshes indefinitely
from there. If the refresh token fully lapses (an instance idle over 30 days), re-bootstrap
from home. Full investigation: [docs/garmin-connectivity-report.md](docs/garmin-connectivity-report.md).

`garth` is pinned to **0.6.3**. Version 0.7+ uses a mobile login flow that Cloudflare
blocks; 0.6.3 uses the older web SSO flow that still works. Stored tokens work on any
version.

### Never let a polled endpoint touch the database

A health-check endpoint ran `SELECT 1` on every call and was configured as the host's
health check path. The host polls it continuously, so every poll reset the database's
autosuspend timer and the compute **never** suspended — burning almost an entire monthly
free-tier budget. It was also the wrong semantic: a transient database blip would fail the
liveness probe and restart the web service.

**Rule: nothing on a timer or a poll may open a database connection.** `/healthz` is
process-only; `/healthz/db` is explicit, manual and authenticated. What matters for
scale-to-zero billing is the number of *distinct wake events*, not query volume — every
wake costs at least the full autosuspend window.

### Reconcile must treat the calendar as ground truth

The old reconcile trusted store-side `calendar_event_id` links. When a plan revision
carried an event id over to a new session, the event's content wasn't reliably rewritten,
and events whose store row lost its link became **ghosts** invisible to store-side cleanup.
The calendar drifted days behind the plan.

Now `reconcile` lists live events and treats the **calendar** as ground truth:
force-correcting a carried-over event via delete-and-reinsert when its summary or date
don't match, and sweeping every event — future *and* past, within a bounded window — that
doesn't back a currently planned session.

A related trap: **a PUT on a cancelled Google Calendar event revives it**, same id, status
flipped back to confirmed. That silently masked a tug-of-war between two competing sync
processes for weeks.

### Scheduled beats need a catch-up window

Exact-hour matching meant one skipped or delayed tick silently lost that day's beat
forever, and cron schedules do slip. A beat now fires on the **first tick at or after** its
local slot, within a bounded catch-up window. The window deliberately never crosses
midnight, so a long outage can't deliver a "morning" brief in the evening.

### A capture surface that costs a sentence loses to one that costs a tap

The evening debrief shipped with the subjective feel as a tap and the lifestyle log as free
text. After six weeks: the feel check-in had a row **every single day**, and the lifestyle
log had **one row total**. Alcohol, illness and sleep flags never reached the coach, and an
illness red-flag rule that needed a logged flag to corroborate could never fire.

**If one half of a merged prompt is answered daily and the other never is, the difference
is input cost, not content.** The flags are now one tap each, multi-select, and each tap
toggles so a mis-tap is undoable.

### When the data cannot distinguish two causes, ask

A planned 3-hour long run came in at 2 hours. The post-run read called it "a large
unexplained shortfall", flagged it, and the red-flag path proposed an easier week off that
reading. **The actual cause was logistical — the athlete ran out of time.**

Nothing in the data can separate "ran out of time" from "legs died". The coach was
inventing a physical cause and then adapting to it, which reads to an athlete as being
managed by something that isn't listening. Now an off-plan session triggers a plain "what
happened?", the answer is stored, and any adaptation is built on the stated cause. The
red-flag detector refuses to raise an *unexplained* off-plan session at all.

### Off-plan detection needs an absolute floor, not just a percentage

A percentage alone is too jumpy on short sessions. Easy runs routinely land 8–25% over plan
because people run a loop or a round number rather than a stopwatch. The classifier
requires **both** a percentage deviation **and** an absolute gap before it asks about a
session. Strength sessions are exempt entirely — watch timers count rest between sets, so
the delta measures nothing there.

### Alert cards must dedupe on the cause, not on pending-ness

One cut-short long run produced **five** red-flag proposals over three days, because the
detector kept the run visible for two days while the proposer only declined to stack when
another card was still *pending*. Cards the athlete is meant to read stop being read — and
the approval gate is the one mechanism everything else depends on. Flags now carry a stable
key per cause, and any card raised in the last week (approved, rejected *or* superseded —
all mean "they have seen it") covers its keys.

### Only the web service runs migrations

`alembic upgrade head` is in the web service's start command; the crons just run jobs. So
between a push and the web service finishing its boot, a cron can wake on new code against
the old schema and crash. Push a migration just after the top of the hour, or apply it to
the database first. Migrations here are additive and nullable, so pre-applying is safe in
both directions.

### Environment variables marked `sync: false` are never set by a blueprint sync

Every new service needs its secrets entered by hand, and **nothing warns you until that
service next runs.** A monthly cron hides the mistake for a month — which is exactly what
happened: the backup cron had never once succeeded, and nobody noticed until someone
checked Drive and found a single file from deploy day.

**Corollary: only a real run proves a fix here.** A "run it now" button in the web app
executes on the *web* service with its own working credentials, so it succeeds regardless
and proves nothing about the cron.

### LLM calls must stream, and tool output must be salvaged

A non-streaming, plan-sized call had minutes of time-to-first-byte and idle middleboxes
killed the connection. All calls stream internally. Separately, the model occasionally
crams an entire parameter set as XML-ish text into the first string field — a salvage
routine reconstructs and type-coerces those fields, and refuses on truncated JSON. Plan
generation retries once and then **raises** rather than shipping an empty proposal.

### Both ends of a pushed watch workout need a target-free step

A heart-rate or pace target is wrong at both ends of a session for one underlying reason:
**HR lags effort.** At the start, a Z2 target arms at second one and alerts about ten
seconds in — nobody is in Z2 ten seconds into a run. At the end, coming off threshold work,
HR takes minutes to fall through a cooldown ceiling, so the watch alerts the athlete *for
recovering correctly*. A gentler target fixes neither end, because HR starts below Z1 too.

The fix carves a free block **out of** the step it belongs to, never added on top —
otherwise every session runs long by design, and invisible extra volume never trips the
off-plan check. This lives in the payload builder rather than in a prompt, deliberately: a
rule that must hold on every single session shouldn't depend on a model remembering it.

### Watch delivery only happens on a scheduling event

Only *scheduling* events trigger delivery to the watch, so the workout reconcile refreshes
the schedule (delete and recreate on the same date) after every content update. Scheduled
workouts appear in the watch's **Training Calendar**, not under Workouts, and delivery needs
a phone-app sync. The legacy device-message queue is a dead end for modern watches.

### Your local database is a stale mirror, not truth

Do not point a local server at real syncs, or production and local diverge. A forgotten
local scheduled task once ran a sync against a weeks-old local database every morning —
and because the calendar token resolves from the *database*, it rewrote the production
calendar daily from a stale plan. There is now a hard guard: the post-sync calendar
reconcile no-ops unless the app is running in production.

---

## 5. Deliberately out of scope

The coach may *mention* these, but the app does not build systems for them:

- Structured strength and mobility programming
- Gear and shoe-rotation tracking
- Heat/altitude acclimatization *protocols*
- A formal HRV-guided daily readiness *scoring system*
- Two-way calendar sync (the calendar is an output; changes go through chat)
- Multi-user accounts or sharing
- Automatic timezone/location detection (set conversationally instead)
- Multiple simultaneous primary goals

## 6. Conventions

- Metric units throughout. English by default; the coach mirrors the athlete's language.
- Proposal kinds: `onboarding_draft`, `macro_plan`, `sessions`. Statuses: `pending`,
  `approved`, `rejected`, `superseded`.
- Jobs: `python -m app.jobs <job>` — `daily_sync`, `full_import`, `tick`, `morning_brief`,
  `daily_debrief`, `weekly_review`, `monthly_export`, and the various backfills.
- Secrets live in `backend/.env` (gitignored). `.env.example` at the repo root lists every
  key with no values.
- Tests must anchor dates to a *relative* today wherever the code windows on it. A test
  pinned to a literal date goes red the day it ages out of the window.
