# Setup

Getting your own copy running. Budget **~2 hours** the first time, and **$12–25/month**
once it's up.

Do the steps in order — later ones depend on earlier ones. After each section you can run
the preflight check, which tells you exactly what is still missing:

```bash
cd backend && python -m app.jobs doctor
```

It never writes anything and makes no network calls, so it's safe to run at any time.
Exit code 0 means clean, 1 means warnings, 2 means something is broken.

> **Read [ROADMAP.md](ROADMAP.md) before you invest the two hours.** The coaching doctrine
> is still backyard-ultra specific and there is no onboarding wizard. If you're training
> for a road marathon, you will want to hand-edit `backend/app/coach/doctrine.py`.

---

## The two things that cost people the most time

Both are called out again in context below, but they cause the majority of failed setups,
so here they are up front:

1. **You cannot log into Garmin from your server.** Garmin's Cloudflare front door blocks
   datacenter IPs. You must run the login once from your home network and paste the
   resulting token. Details in §4.
2. **Set your Google OAuth consent screen to "In production".** Left in "Testing", Google
   issues refresh tokens that expire after **7 days** — so everything works for a week and
   then the calendar silently dies. Details in §3.

---

## 1. Anthropic API key

The coach is Claude. Without this, nothing works.

1. Create a key at [console.anthropic.com](https://console.anthropic.com).
2. Add credit to the account — a new key with a zero balance fails at the first brief.
3. Set `ANTHROPIC_API_KEY`.

**Cost:** roughly $5–15/month. The expensive calls are the Sunday weekly review and long
coaching conversations; the daily brief and debrief are cheap. A full plan re-draft is
about $0.15.

## 2. Database

Any Postgres works. [Neon](https://neon.tech)'s free tier is enough.

1. Create a project and copy the connection string into `DATABASE_URL`.
2. Cap the compute at a small size. This app is idle almost all the time.

> **Never point a health check or any other polled endpoint at the database.** On a
> scale-to-zero database, every poll resets the autosuspend timer and the compute never
> sleeps — this burned an entire monthly free-tier budget once. `/healthz` is deliberately
> process-only; `/healthz/db` is manual and authenticated. See ARCHITECTURE.md.

Local development can use SQLite (`sqlite:///./j2h4all_dev.sqlite3`). Production refuses to
boot on anything but Postgres.

Then create the schema:

```bash
cd backend && alembic upgrade head
```

## 3. Google Cloud project

Used for three things: signing in to the web app, writing your training calendar, and the
monthly backup to your own Drive.

1. Create a project at [console.cloud.google.com](https://console.cloud.google.com).
2. Enable the **Google Calendar API** and the **Google Drive API**.
3. Configure the OAuth consent screen:
   - User type **External**, with yourself as the only user.
   - **Publish it — set it to "In production".** ⚠️ This is the 7-day expiry trap. In
     "Testing" mode your refresh token dies after a week and the calendar stops updating
     with no error you'll notice.
   - You'll see an "unverified app" warning when you sign in. That's expected — it's your
     own project, with only you on it. Click through it.
4. Create an **OAuth client ID** of type *Web application*, with two authorized redirect
   URIs:
   - `https://<your-app>/auth/callback`
   - `https://<your-app>/auth/calendar/callback`
5. Set `GOOGLE_CLIENT_ID` and `GOOGLE_CLIENT_SECRET`.
6. Set `ALLOWED_GOOGLE_EMAIL` to your own Google address. **This is the single-user gate** —
   every other account is rejected after sign-in.
7. After the app is running, use **Connect Google Calendar** in the web app, then copy the
   resulting refresh token into `GOOGLE_REFRESH_TOKEN`.

> Step 7 matters more than it looks. The web service stores the token in its database, but
> the **cron jobs** read it from the environment. Skip it and your daily calendar updates
> and monthly backups silently do nothing.

## 4. Garmin

**Read this whole section before starting.** Garmin's Cloudflare front door blocks
datacenter IP ranges outright on its auth endpoints — Render, GitHub Actions, everything.
This is not a bug you can configure around; see
[docs/garmin-connectivity-report.md](docs/garmin-connectivity-report.md) for the full
investigation.

What *does* work: only the initial token exchange is blocked. The OAuth2 **refresh grant**
runs on a different host that isn't blocked, and it rolls forever. So you bootstrap once
from home, and your server refreshes indefinitely from there.

**On your own machine, at home:**

```bash
cd backend
python -m app.garmin.login
```

It prompts for your Garmin email, password and MFA code, then writes `GARTH_TOKEN` into
`backend/.env`. Copy that value into your deployment's environment.

- Keep `garth` pinned at **0.6.3**. Newer versions use a mobile login flow that Cloudflare
  blocks.
- If your instance sits idle for over 30 days the refresh token can lapse. Re-run the
  command from home to re-bootstrap.
- Set `GARMIN_WORKOUT_PUSH_ENABLED=true` once you're comfortable with the app writing
  scheduled workouts to your Garmin account. It defaults to false so a dev environment
  can't touch your real account by accident.

## 5. Telegram

This is where the coach actually reaches you.

1. Message [@BotFather](https://t.me/BotFather), send `/newbot`, and copy the token into
   `TELEGRAM_BOT_TOKEN`.
2. Send your new bot any message.
3. Fetch your chat id and put it in `TELEGRAM_CHAT_ID`:
   ```bash
   curl "https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates"
   ```
   Look for `"chat":{"id":...}`. **This is the single-user gate for Telegram** — every
   other sender is silently ignored.
4. Set `TELEGRAM_WEBHOOK_SECRET` to any random string.
5. Once deployed, register the webhook:
   ```bash
   curl -F "url=https://<your-app>/telegram/webhook" \
        -F "secret_token=<YOUR_WEBHOOK_SECRET>" \
        "https://api.telegram.org/bot<YOUR_TOKEN>/setWebhook"
   ```

## 6. Deploy

`render.neon.yaml` is a Render blueprint declaring a web service and three cron jobs.

1. Create a Blueprint instance from the file, or create the services by hand.
2. Set every environment variable from `.env.example` on the **web service**.
3. ⚠️ **Set `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET` and `GOOGLE_REFRESH_TOKEN` on the
   cron services too.** Variables marked `sync: false` in a blueprint are *never* set by a
   blueprint sync — each service needs them entered by hand, and nothing warns you until
   that service next runs. A monthly backup cron hides the mistake for a month. This has
   happened; see ARCHITECTURE.md.
4. Verify the schedules survived: daily sync `0 1 * * *`, tick `0 * * * *`, export
   `0 6 1 * *`. **Keep the tick hourly** — coarsening it breaks the Sunday-evening weekly
   review.
5. Set `APP_ENV=production` and a real `SECRET_KEY`.

**Cost:** ~$7/month for a Starter web service. The crons are free. Neon's free tier is
enough if you follow the polling rule in §2.

## 7. First run

1. `python -m app.jobs doctor` — everything should be `ok` or an intentional warning.
2. Sign in to the web app with your allowlisted Google account.
3. Import your history:
   ```bash
   cd backend && python -m app.jobs full_import
   ```
   This pulls roughly two years of Garmin data and takes a while.
4. **Tell the coach who you are.** In Telegram or the web Context panel, say your name,
   your pronouns, your age, and — most valuable — anything that makes your *data* read
   wrong rather than your training: restless legs wrecking your sleep score while recovery
   is fine, night shifts, medication that caps your heart rate.
5. **Set your real race.** A fresh install carries a clearly-labelled placeholder goal. The
   entire plan is built backwards from your race date, so replace it before drafting a plan.
6. Draft your first plan from the web app, and approve it.

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| Calendar stopped updating after about a week | Consent screen left in "Testing" (§3) |
| Calendar never updates, but the web app's push button works | `GOOGLE_*` not set on the cron service (§6.3) |
| No Garmin data | `GARTH_TOKEN` missing or lapsed — re-bootstrap from home (§4) |
| Bot ignores you | `TELEGRAM_CHAT_ID` doesn't match your chat (§5) |
| No Sunday weekly review | The tick cron isn't hourly (§6.4) |
| Coach doesn't know who you are | No athlete profile yet (§7.4) |
| Everything looks configured but nothing happens | `python -m app.jobs doctor` |
