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

Writable from chat too: the context extractor has a `profile` item kind, so "call me Sam,
they/them, and my sleep score is always awful because of restless legs" lands in the right
fields via the usual extract → confirm → write loop. The web Context panel shows who the
coach thinks it's coaching, so wrong pronouns are visible at a glance rather than
discovered in a morning brief.

## 2. Format-agnostic doctrine — ✅ DONE

`coach/formats/` holds one module per race format; `coach/doctrine.py` keeps the shared
endurance core. `Goal.format` selects. Composition is: who + goal (rendered by the format)
→ what THIS race demands → the shared core → the format's training additions → optionally
race-day execution → the cross-cutting guardrails.

Shipped: **backyard-ultra**, **trail-ultra**, **road-ultra**, **road-marathon**, and
**generic** (the honest fallback — coaches sound general endurance and asks what the race
is, rather than inheriting whichever format happened to be the default). Aliases resolve
near-misses (`marathon`, `UTMB`, `100k`); anything unrecognised degrades to generic and
never raises. `Goal` gained `distance_km`, `elevation_gain_m` and `target_time`
(migration 0016), since `loop_km`/`target_laps` mean nothing outside a backyard. The macro
prompt takes its phase vocabulary from the format, so a marathon build is no longer asked
for a "backyard-specific" block.

Tests lock the property that matters in both directions: backyard concepts must not leak
into a marathon, and marathon concepts must not leak into a backyard.

⚠️ **Only the backyard format is validated.** The other three were written from
established coaching principle and reviewed, but never run against a real athlete's season
or a prompt eval — `prompt_eval.py` needs real state and only exercises two surfaces. They
are a starting point to tune, not finished work. If you build a season on one, expect to
edit its module.

Still open here:
- Two signals are still framed for the original race and should key off the goal:
  `heat_acclimation` reads as a hot-race signal regardless of where the race is, and
  pace-CV is labelled "the metronomic loop signal" when the metric is generic pace
  consistency.
- The web Plan panel still says "days to the backyard" rather than naming the format.
- Nothing lets an athlete SET their format yet except editing the database — it needs to
  join the goal step of onboarding (§3).

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

## 4. Setup documentation — ✅ DONE

[SETUP.md](SETUP.md) walks through all six provisioning tracks in order, leading with the
two known time-sinks (the Garmin residential bootstrap, and the Google consent screen that
must be set to "In production" or the refresh token dies after 7 days), and ends with a
symptom→cause troubleshooting table.

`python -m app.jobs doctor` is a read-only, no-network preflight that reports what is
configured, what is missing, and **what to do about each gap**. Exit code 0/1/2 (clean /
warnings / broken) so it can gate a deploy. It also looks in the database for the two
things that are invisible from the environment: whether an athlete profile exists, and
whether the goal is still the placeholder.

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
