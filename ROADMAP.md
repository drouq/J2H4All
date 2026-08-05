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

A later sweep found three **per-surface prompts** that still named one format in their own
text, on top of the doctrine: the morning brief told every athlete their session built
toward "the backyard (durability, time-on-feet, fueling practice, walk/run rehearsal)",
the post-run read called even pacing "the backyard-relevant trait", and the context
extractor opened by asserting the athlete was "an ultra-runner training for a backyard
ultra". Each now defers to the doctrine block it already includes, and a test covers the
class. The signal framing (`heat_acclimation`, pace-CV) and the Plan panel's "days to the
backyard" are fixed too.

## 3. Onboarding — ✅ DONE

A **Setup panel** is the first thing in the app. It reports every step (AI key, profile,
race, Garmin, history, calendar, Telegram, plan), says what to DO about each gap, and
distinguishes what merely degrades the coaching from what makes it wrong.

**Setting your race** — format, date, and only the fields that format actually uses.
Asking a marathoner for a lap count is how a setup form teaches someone the app isn't
really for them. `format` is normalized through the doctrine registry, so "marathon"
lands on marathon doctrine and a typo lands on `generic`. Switching format clears the
previous format's fields.

**The plan-draft guard.** `POST /api/plan/draft` returns 409 while a blocker stands, and
names it. Only two things block: no API key, and a placeholder race. A plan periodized
backwards from a date nobody chose looks completely normal — that is exactly why refusing
beats producing it, because the athlete cannot tell the difference by reading it.
Deliberately NOT blocking: Garmin, history, calendar, Telegram, profile. Those degrade a
plan without falsifying it, and refusing there would be paternalistic.

**Garmin token paste.** `POST /api/setup/garmin-token` accepts the blob from
`python -m app.garmin.login`, validates it by loading it (a truncated paste fails there
with a clear message rather than at 01:00 in a cron), and stores it as an *internal*
preference so it never reaches the context panel or a prompt. The environment variable
still wins if set. **Nobody has to edit a host environment variable for Garmin any more** —
that was the step most likely to strand a self-hoster.

**Telegram pairing** (`coach`-adjacent module `telegram_link.py`). Click "Get a pairing
code", send the 8 digits to your bot, done — no more digging a numeric chat ID out of
`getUpdates`. Only the SOURCE of the bound chat id changed; the gate is exactly as strict:

- The environment variable **always wins**, so an operator-set gate can't be altered from
  inside the app, and pairing is refused entirely when it is set.
- **Unbound means NOBODY**, never everybody. With no env var and nothing paired the gate
  rejects every sender. It fails closed, and three tests assert it directly — verified by
  deliberately inverting the gate and confirming they catch it.
- One chat, ever: a second chat can't pair over an existing binding without an explicit
  unpair. The code is 8 digits from `secrets`, expires in 10 minutes, and is single-use
  **even when wrong**, so one armed window isn't unlimited guesses.
- A wrong guess gets silence, so the bot can't be probed to reveal that a pairing is in
  progress.
- The configured (env) case resolves without a database query at all, and the paired value
  is cached for a minute — message spam can't be used to keep a scale-to-zero database awake.

`TELEGRAM_CHAT_ID` is no longer required to boot in production: unbound is the safe state,
and requiring it would make pairing impossible in the one place it matters.

**The guided flow.** The Setup panel now walks the steps in order: done steps collapse
to a line, the next incomplete step opens with its controls inline, and a progress bar
tracks the count. Any step can be reopened by clicking it — a wizard that won't let you
go back and change your race would be worse than the list it replaced. **Training
history imports from the panel too** (the button drives the same `/api/sync?mode=full`
path as the CLI job).

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

## 5. Housekeeping — ✅ DONE

The ~150 `PRD §N` comment citations are gone: they pointed at a private spec that isn't
published, so they were dead references for anyone reading this code. Most were pure
parentheticals and came out mechanically; the ~15 that carried meaning in prose were
rewritten to keep the substance ("PRD §11.4: approval writes the store AND the calendar"
became "Approval writes the store AND the calendar").

`docs/garmin-workout-push-plan.md` (an implementation plan full of resolved sign-offs)
became `docs/garmin-workout-push.md`, a reference for the shipped behaviour — the API
surface, the session→workout mapping, the lifecycle rules and the accepted risks are the
parts anyone touching `garmin/workouts.py` actually needs. Ruff's `I` (import sort) and
`UP` (modernization) rules are on; the sweep was mechanical (79 fixes) and retired the
`E702` carve-out by writing the one throttle idiom it protected as a normal import.
`scripts/home_sync.ps1` stays: it is the residential re-bootstrap path when a Garmin
refresh token lapses, which is an operational fallback rather than cruft.

---

## Deliberately not planned

**A hosted, multi-tenant version.** Every table is effectively single-row-global
(`user_state.id` is pinned to 1, preference keys are globally unique), and the single-user
gate is load-bearing throughout. Multi-tenancy would be a rewrite, not a refactor — and
self-hosting gives complete data isolation for free. One deployment per athlete is the
design, not a limitation.
