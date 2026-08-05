# J2H4All

A **self-hosted AI endurance running coach for one athlete — you.**

You run your own copy. It syncs your Garmin data, learns your life context from
conversation, generates and continuously adapts a periodized training plan using Claude,
pushes sessions to your own Google Calendar **and to your watch as structured workouts**,
and reaches you proactively over Telegram — morning brief, evening debrief, Sunday weekly
review, red-flag pings — with depth on a mobile-first web app.

Guiding sentence:

> *A real endurance coach, for one athlete, that reads their physiology and their life,
> builds and continuously adapts their plan, and reaches them wherever they are — but
> never changes their calendar or their plan without asking first.*

The name is a leftover: the original was "J2H — Journey to Hundred", built by one runner
chasing 100 miles in a backyard ultra. This is that app, generalised so anyone can run
their own.

---

## ⚠️ Read this before you start

**This is a working app that is still mid-generalisation.**

**Supported race formats:** backyard ultra, trail/mountain ultra, road/flat ultra, road
marathon and half. Set `Goal.format` and the coach reasons about *your* race — a
marathoner gets goal-pace and threshold doctrine, a trail runner gets descent
conditioning and cutoffs. An unrecognised format falls back to sound general endurance
coaching and asks what the race actually is, rather than guessing.

Two honest caveats:

1. **Only the backyard format has been validated against a real athlete's season.** The
   others were written from established endurance-coaching principle and reviewed, but
   have not been run against a real build or a prompt eval. Treat them as a good starting
   point to tune — [`coach/formats/`](backend/app/coach/formats/) is one small file per
   format, deliberately easy to edit.
2. **Onboarding is a status list, not a guided wizard.** The Setup panel tells you what's
   done, what's missing and what to do about each gap, and lets you set your race, paste
   your Garmin token and pair your Telegram bot with a code. It won't hold your hand
   through the steps in order. See [ROADMAP.md](ROADMAP.md).

**No personal data from the original install ships in this repo** — no athlete, no race,
no medical history, no credentials. Every identifier is yours to fill in.

**This is not medical advice.** The coach flags trends in your data and will tell you when
something is worth discussing with a doctor. It does not diagnose and does not prescribe.
The software comes with no warranty of any kind — see [LICENSE](LICENSE).

---

## What it does

| Subsystem | What it gives you |
|---|---|
| **Garmin sync** | Full history import + daily incremental. Activities, HR zones, sleep, HRV, resting HR, skin temp, respiration, VO2max, training load. Per-run weather and per-second durability streams (aerobic decoupling, HR drift, pace consistency). |
| **Context store** | Talk to it — "ferritin came back at 28", "I'm in London for two weeks", "my calf is sore". Claude extracts to typed fields, confirms, then writes. PDF blood-panel parsing. |
| **Plan pipeline** | A periodized macro plan to race day plus a rolling ~30-day block of daily sessions, each carrying its "why". Duration-first, not distance-first. |
| **Approval gate** | Nothing side-effectful happens without your explicit tap. Every calendar write, watch push and plan revision is *proposed* first. |
| **Calendar + watch** | A dedicated Google Calendar, one event per session, with ✅ / ⚠️ / ❌ completion marking. Approved runs also become scheduled Garmin structured workouts. |
| **Adaptation loop** | Morning brief, evening debrief (feel + life factors, one tap each), Sunday weekly review that re-plans the next 30 days, and red-flag pings on illness/fatigue signals. |
| **Setup** | A first-run panel: what's configured, what's missing, what to do about it. Sets your race, and takes a pasted Garmin token so you never edit a host environment variable. Refuses to draft a plan against a race you haven't set. |
| **Web app** | Plan, Trends (load balance, volume vs plan, heat acclimation, bloods), Calendar, Context, Backup. Monthly JSON export to your own Google Drive. |

## Architecture in one line

Python 3.12 / FastAPI / Postgres on the backend, `garth` for Garmin, a React SPA served by
FastAPI, the Anthropic Claude API doing the coaching, and Telegram as the conversational
surface. Deployed on Render with a Neon Postgres database.

Read [ARCHITECTURE.md](ARCHITECTURE.md) for the design principles, the layout, and — most
valuable — the **hard-won operational learnings**. Several of them (the Garmin/Cloudflare
datacenter block, the never-poll-the-database rule) will cost you real time if you
rediscover them yourself.

## Layout

```
backend/           FastAPI app (app/), Alembic migrations, tests (pytest), prompt_eval.py
frontend/          React 18 + Vite + TypeScript SPA, built into frontend/dist
docs/              Garmin connectivity report, workout-push reference
scripts/           Home-sync fallback (see the Garmin note in ARCHITECTURE.md)
render.neon.yaml   Deployment blueprint: web service + 3 cron jobs
.env.example       Every environment variable the app reads
```

## Setup

**[SETUP.md](SETUP.md) is the step-by-step guide.** Run the preflight check at any point to
see what's still missing — it never writes anything and makes no network calls:

```bash
cd backend && python -m app.jobs doctor
```

Self-hosting means running your own everything. Budget **~2 hours** for first setup, and
roughly **$12–25/month**:

| Service | For | Cost |
|---|---|---|
| **Anthropic API key** | The coach itself | ~$5–15/mo depending on use |
| **Render** | Web service + 3 crons | ~$7/mo (Starter) |
| **Neon** | Postgres | Free tier is enough |
| **Google Cloud project** | Sign-in, Calendar, Drive backup | Free |
| **Telegram bot** | The conversational surface | Free |
| **Garmin account** | Your physiology | You have one |

Two setup steps reliably cost people time, so they're called out here rather than buried:

- **Garmin login must be bootstrapped from your home network.** Garmin's Cloudflare front
  door blocks datacenter IPs outright, so you cannot log in from Render. You run
  `python -m app.garmin.login` once on your own machine and paste the resulting token.
  Full explanation in [docs/garmin-connectivity-report.md](docs/garmin-connectivity-report.md).
- **Set your Google OAuth consent screen to "In production".** Left in "Testing", your
  refresh token silently expires after 7 days and the calendar integration dies.

## Local development

```powershell
cd backend
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements-dev.txt
copy ..\.env.example .env
alembic upgrade head
uvicorn app.main:app --reload --port 8000
```

```powershell
cd frontend
npm install
npm run dev
```

Tests and lint — both also run in CI on every push:

```bash
cd backend && python -m pytest && python -m ruff check .
```

The suite runs on in-memory SQLite. It needs no network, no credentials and no
services, so it runs anywhere.

## Contributing

This is a personal-scale project shared with friends. [ROADMAP.md](ROADMAP.md) is the
honest list of what's missing. If you generalise something that is still athlete-specific,
that is the most useful contribution there is.

## License

MIT — see [LICENSE](LICENSE).
