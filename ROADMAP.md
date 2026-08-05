# Roadmap

The honest list of what stands between this and "clone it and go". Ordered roughly by how
much it blocks a new athlete.

This repo was generalised from a single-athlete app. **All personal data is gone** — no
name, no race, no medical history, no credentials, in the tree or in the git history. What
remains is *shape*: assumptions that made sense when there was exactly one athlete and one
race format.

---

## 1. Typed athlete profile — ✅ DONE

`AthleteProfile` (migration 0015) carries name, pronouns, birthdate, language and a
free-text `data_caveats` field for the physiology quirks that change how the data should be
*read* rather than how the athlete should be trained. `doctrine.identity_line()` renders it
onto both prompt surfaces, and `data_caveats` lands next to the generic sleep-composite rule
it qualifies.

The ~115 inherited gendered pronouns are gone from the prompts; static prose is neutral and
the coach is told the athlete's actual pronouns, defaulting to they/them rather than
guessing. `dietary_profile.diet` no longer defaults to one athlete's diet.

Still open here: the profile is only writable through code. It needs to be settable from
chat (extend the context extractor with a `profile` item kind) and from the web Context
panel — folded into the onboarding work in §3.

## 2. Format-agnostic doctrine *(blocks non-backyard athletes)*

`coach/doctrine.py` is backyard-ultra specific: hourly laps, the fueling reset, night laps,
the crewed pit routine. A marathoner gets coached in the wrong idiom.

**Don't write one doctrine per format** — that's how you end up with five mediocre coaches.
Layer it instead. Reading the current file closely, `TRAINING_DOCTRINE` is already about
70% universal endurance principle: time-on-feet, 80/20, the load-balance read, decoupling as
the durability KPI, ~10%/week ramps with down-weeks, never volume and intensity in the same
week, gut training, taper principles. Only a few items are format-specific.

**Wanted:** a shared endurance core (kept exactly as tuned) plus a thin per-format layer of
race demands, race-day execution, and a few training additions — roughly 60 lines of new
prose per format instead of 300. Support a **fixed enum** of formats, not free text: a rule
that must hold every session shouldn't depend on a model inventing the doctrine at
onboarding.

Related schema work:

- `Goal` doesn't fit other formats. `loop_km` and `target_laps` are backyard-only; a
  marathon needs a target time, a trail ultra needs distance, vert and cutoffs.
- Phase names in the macro prompt (`base → build → backyard-specific → taper`) should come
  from the format rather than being hardcoded in the prompt and the tool schema.
- Two signals need *relabelling*, not rebuilding: heat acclimation is framed as a signal
  for a hot race and should key off the race's actual climate; pace-CV is framed as "the
  metronomic loop signal" when the metric itself is generic pace consistency.

`completion.py`, `postrun.py`, the calendar and the workout push are already
format-neutral.

**Budget eval time inside this work, not after it.** `prompt_eval.py` only exercises two
surfaces against real state, and it cannot validate a marathon doctrine at all.

## 3. Onboarding *(blocks everyone; painful without it)*

A fresh install seeds a **placeholder goal** (`plan/store.py::ensure_seed`) that must be
replaced by hand, and nothing guards plan generation against a half-configured install — it
will happily draft a plan for the placeholder.

**Wanted:** a first-run wizard — sign in → connect Garmin → Calendar → Telegram → athlete
profile → goal and races → preferences → full history import → first plan draft. Plus a
guard that refuses plan generation until a profile and a real goal exist.

Two specific bits of UX are worth more than any amount of documentation, because they
remove the two steps most likely to strand someone:

- **Garmin token paste.** The wizard can't do the login server-side (see the Cloudflare
  block in ARCHITECTURE.md), but it *can* accept the pasted token blob. The runtime
  credential is already a database-stored rotating token, so accepting it through the web
  form means **nobody ever edits a host environment variable for Garmin.**
- **Telegram pairing code.** Instead of making people hunt for a numeric chat ID: the web
  shows a code, they message it to their bot, the bot binds that chat.

> ⚠️ The pairing code moves the single-user Telegram gate from an environment variable to
> the database. That is one of the two hard rules. The gate must stay exactly as strict —
> only its *source* changes, with the environment variable keeping precedence — and it
> needs explicit test coverage before it ships. Do not slip this in as wizard plumbing.

## 4. Setup documentation

A `SETUP.md` walking through all six provisioning tracks in order, leading with the two
known time-sinks (the Garmin residential bootstrap, and the Google consent screen that must
be set to "In production" or the refresh token dies after 7 days).

A `python -m app.jobs doctor` preflight that checks every credential and prints exactly
what's missing pays for itself on the first install.

## 5. Housekeeping

- **~150 `PRD §N` comment citations** point at a private spec that isn't published. Strip
  them, ideally during the doctrine work when those files are open anyway.
- `docs/garmin-workout-push-plan.md` is a historical design record with resolved decisions.
  Keep for rationale, or fold the conclusions into ARCHITECTURE.md.
- `scripts/home_sync.ps1` is a residential-IP fallback from before the native refresh grant
  worked. Still useful for re-bootstrapping; not needed in normal operation.
- Optional lint hardening: enable ruff's `I` (import sort) and `UP` (modernization) rules
  if the repo-wide churn is ever wanted.

---

## Deliberately not planned

**A hosted, multi-tenant version.** Every table is effectively single-row-global
(`user_state.id` is pinned to 1, preference keys are globally unique), and the single-user
gate is load-bearing throughout. Multi-tenancy would be a rewrite, not a refactor — and
self-hosting gives complete data isolation for free. One deployment per athlete is the
design, not a limitation.
