# Garmin Connectivity from Cloud Hosts — Incident & Investigation Report

**Project:** J2H4All (Journey to Hundred, for All) — single-user AI running coach
**Period:** 2026-07-06 → 2026-07-08
**Status at time of writing:** **RESOLVED.** The root fix — Garmin's OAuth2 *refresh
grant* on `diauth.garmin.com` is not IP-blocked (only the OAuth1 exchange on
`connectapi.garmin.com` is), and connectapi *data* calls were never blocked — lets Render
sync natively again, with no home PC and no proxy (§4.7). The HYBRID home sync and the
residential-proxy plumbing are retained as fallbacks.

---

## 1. Executive summary

After deploying J2H4All to Render (2026-07-06), the automated Garmin data sync — the
pipeline that feeds the coach all physiological data — could not run: every attempt
failed with **HTTP 429 on Garmin's OAuth token exchange**. Three days of investigation
established the root cause conclusively: **Garmin's Cloudflare front door blocks
datacenter IP ranges outright** on its auth endpoints. It is not a TLS-fingerprint
problem, not a token/credential problem, not a rate-limit in the ordinary sense, and
not specific to Render — GitHub Actions runners are blocked identically.

Every remediation that kept the sync on a cloud host failed for the same structural
reason: the connection's *origin* is judged before the request is read. The two
workable designs are therefore both "borrow a residential IP":

1. **HYBRID (implemented, live):** the sync runs on the athlete's own home PC (residential IP)
   and writes directly to the production Neon database; everything else stays on Render.
2. **Residential proxy (built, pending signup):** Render keeps the sync but its Garmin
   traffic exits through a pay-as-you-go residential proxy (~$1/GB; a daily sync is a
   few MB). This removes the home-PC dependency and is the intended end state.

## 2. Background

J2H4All syncs Garmin Connect data (activities, wellness, fitness markers) via `garth`, a
community Python client for Garmin's **unofficial** web API — the same endpoints the
Garmin Connect website uses. There is no official alternative for individuals: Garmin's
Connect Developer Program (Health/Activity APIs) is **restricted to business use**.

Authentication works in two stages:

- A **long-lived OAuth1 credential** (~1 year), obtained once by interactive login
  (email + password + MFA). Stored as the `GARTH_TOKEN` blob.
- A **short-lived OAuth2 access token** (~1 hour), obtained by exchanging the OAuth1
  credential at `POST /oauth-service/oauth/exchange`. Because the access token is
  nearly always expired by the next sync, **every unattended sync performs this
  exchange** — making it the single point of failure.

Garmin fronts these endpoints with Cloudflare bot protection, which scores connections
on (at least) two independent layers:

1. **TLS/client fingerprint** — does the handshake look like a real browser?
2. **IP/ASN reputation** — does the connection originate from consumer ISP space or
   from cloud/hosting ranges?

A request must pass **both**. This layering is the key to understanding every result
below.

## 3. Timeline of the problem

| Date | Event |
|---|---|
| 2026-07-06 | Production deployed to Render (`render.neon.yaml`: web + 3 crons + external Neon Postgres). |
| 2026-07-07 | Discovered the cron services had **never run** — the Blueprint deploy hadn't propagated env secrets to them. Secrets added; crons started running… |
| 2026-07-07 | …and `daily_sync` immediately failed: **429 on the OAuth exchange**. Investigation began. |
| 2026-07-07 | TLS impersonation built and deployed (`curl_cffi`, see §4.1) — still 429. Hybrid fallback designed; final confirmation deferred to the next scheduled run. |
| 2026-07-08 | 05:00 UTC auto-run 429'd again. Token hypothesis tested and refuted (§4.2). HYBRID implemented, verified, and made live the same day (§5). GitHub Actions tested and found blocked (§4.3); official API confirmed unavailable (§4.4); alternative libraries assessed as irrelevant to the failure layer (§4.5). Residential-proxy support built and pushed (§6). |

## 4. What we tried, and why each failed

### 4.1 TLS fingerprint impersonation — failed (ruled out layer 1)

**Hypothesis:** Cloudflare rejects garth's Python HTTP stack (requests/urllib3) because
its TLS handshake doesn't look like a browser.

**Action:** built `app/garmin/impersonate.py` (commit `187491d`): routes garth's data
calls through a `curl_cffi` session that presents Chrome's exact TLS fingerprint, and
reimplements the one call garth performs outside its injectable session — the OAuth1→2
exchange, with manual OAuth1 signing — over `curl_cffi` as well.

**Result:** the identical impersonated exchange **succeeds from a residential IP and
429s from Render**. Since the request bytes and TLS presentation are now the same in
both places, the block discriminates on the only remaining variable: **source IP**.
(The impersonation was kept — it's harmless and removes the fingerprint variable
permanently.)

### 4.2 Stale-credential hypothesis (missed 2FA re-login) — refuted (ruled out auth)

**Hypothesis:** the Garmin login was never redone after the Render deploy,
so the deployed token might be stale or wrong, and re-running the MFA login would fix it.

**Tests (2026-07-08):**

1. **Blob comparison:** the `GARTH_TOKEN` staged for Render is **byte-identical**
   (same SHA-256) to the working local one — no paste truncation, no divergence.
2. **Decode:** the OAuth1 credential is present; the OAuth2 refresh token was still
   valid for ~27 days. Only the ~1-hour access token was expired — which is normal and
   is precisely why the exchange runs each sync.
3. **Live proof:** using that exact token, the exchange + two authenticated data calls
   **succeeded from the residential IP on the first attempt**.

**Conclusion:** the credential was never the problem. Also diagnostic: a bad token
produces **401/403** after the request is validated; a **429 emitted before
validation** is Cloudflare speaking, not Garmin's auth service. No re-login needed —
and none would have helped.

### 4.3 GitHub Actions as a free cloud runner — failed (ruled out "some other cloud")

**Hypothesis:** the block might be specific to Render's egress ASN (poisoned by
neighbors sharing its NAT). GitHub Actions runners (Azure) might pass — several public
projects appear to run Garmin syncs on Actions schedules.

**Action:** built a connectivity probe (`app/garmin/probe.py`) that performs the exact
failing path — token load → impersonated exchange → authenticated data call — plus a
manually-triggered workflow (`.github/workflows/garmin-probe.yml`) running it on a
GitHub runner with `GARTH_TOKEN` as a repo secret.

**Result:** **429 on both runs, on two different runners** (2026-07-08, runs
`28936343182` and `28936382941`). The same probe passes from the residential IP.
Garmin's Cloudflare rules cover GitHub's Azure egress too — the block is
**blanket-datacenter**, not one bad ASN. (Public projects that "work on Actions"
most plausibly benefit from cached, still-valid OAuth2 tokens between runs, target
different endpoints, or predate the current rules.)

### 4.4 Official Garmin API — unavailable (ruled out the sanctioned path)

The Garmin Connect Developer Program (Health, Activity, Training APIs) would eliminate
the problem entirely — it's push-based (Garmin calls *your* endpoint), so origin-IP
reputation never comes into play. Checked 2026-07-08: the program is free but
**explicitly business-use only; individual/hobby developers are ineligible**.

### 4.5 Alternative client libraries — not applicable (wrong layer)

Assessed `python-garminconnect`, `GarminDB`, and `garmin-data-export` on the
suggestion. All three are clients of the **same unofficial API** (two of them literally
authenticate via garth or a garth-derived flow) and are designed to run locally.
Cloudflare's verdict is rendered at the network layer — *where the TCP connection comes
from* — before any application-layer difference between libraries can matter. A
byte-identical request already succeeds residentially and fails from datacenters, so no
client swap can change the outcome.

One nuance checked and set aside: `python-garminconnect`'s web-widget SSO strategy
bypasses rate limits on the **fresh-login** endpoint. J2H4All logs in interactively about
once a year, from home, successfully — our failing call is the daily **token exchange**
from a datacenter, a different endpoint and a different mechanism.

### 4.6 Headless browser / JS-challenge hypothesis — refuted (classified the 429 itself)

**Hypothesis:** Cloudflare might be serving an invisible **JavaScript challenge**
(Turnstile / managed challenge) to datacenter IPs. `curl_cffi` spoofs Chrome's TLS but
does not execute JS, so it would fail such a challenge; a headless browser (Playwright/
Puppeteer + stealth) might solve it, obtain a `cf_clearance` cookie, and let the exchange
through.

**Test (cheap-first):** rather than build a headless-Chromium stack on Render, classify
the failing response directly. `app/garmin/diag.py` performs the exact exchange request
and dumps the raw status, all headers, and the body instead of raising; run from a GitHub
datacenter runner (`.github/workflows/garmin-diag.yml`) that reproduces the block.

**Result (2026-07-08, run `28937540728`):**

- Status **429**; body was the **12-byte plain string `Rate limited`** — not an HTML
  interstitial.
- **No challenge signature:** no `cf-mitigated: challenge` header, no
  `challenge-platform` / `cf_chl_opt` / Turnstile markup, no `retry-after`. (`server:
  cloudflare`, `cf-ray: …-ORD`.)

**Conclusion:** it is a **bare block, not a solvable challenge** — there is nothing for a
browser to execute. The rejection is applied at the edge on the source IP/ASN *before*
any content is served, and it fired on the *first* request from a fresh runner (so it is
an aggressive per-ASN rule, not literal over-use). This *strengthens* the origin-IP
conclusion: no client-side technique — TLS spoofing, JS execution, cookies, stealth —
operates at the layer where the block lives. (Even counterfactually, headless browsers
are themselves widely detected and blocked from solving managed challenges from
datacenter IPs, so the approach would likely have looped regardless.) `diag.py` is kept
as a permanent one-command classifier.

### 4.7 OAuth2 refresh grant on a different host — SUCCEEDED (the actual fix)

**Hypothesis:** the token blob carries a ~27-day OAuth2 **refresh token** that we never
tried to use. garth refreshes by habit via the OAuth1→2 *exchange* on
`connectapi.garmin.com` (429'd from datacenters), but the standard OAuth2 **refresh
grant** is a *different endpoint on a different host* (`diauth.garmin.com`) that may carry
different Cloudflare rules. If the refresh grant works from a datacenter, the blocked
exchange is only needed to bootstrap.

**Corroboration:** garth's own `http.py` comments that a refresh path exists but it
re-runs the exchange anyway — so the grant was genuinely untested here.

**Findings (2026-07-08, probes run residentially then from GitHub datacenter runners):**

- **Endpoint & client:** `POST https://diauth.garmin.com/di-oauth2-service/oauth/token`,
  `grant_type=refresh_token`, `client_id=GARMIN_CONNECT_MOBILE_ANDROID_DI` (the public
  mobile client, read from the access-token JWT), no secret. (The consumer key/secret used
  for the OAuth1 exchange are *not* the OAuth2 client_id — an early 401 revealed this.)
- **Works from a datacenter:** the refresh grant returns **200 from GitHub runners**
  (`cf-ray …-IAD/-SJC`), where the exchange 429s. Different host, different rules —
  confirmed.
- **Rolls forever:** each refresh returns a fresh **~23h access token** and a **new ~30-day
  refresh token**; chaining a second refresh with the new token also 200s with a fresh
  ~30-day TTL. So refreshing at least once per 30 days (trivially, daily) sustains
  indefinitely — the blocked exchange is never needed again after the initial login.
- **connectapi data calls were never blocked:** with a diauth-obtained access token, real
  data calls (profile, activity list, wellness) all return **200 from a datacenter**. The
  exchange had simply failed *first* and masked this.

**Implementation:** `app/garmin/oauth2.py` performs the refresh; `GarminClient(db=…)`
adopts the freshest persisted token, refreshes via diauth, and persists the rotated token
in `Preference('garmin_oauth2_token')` so successive cron runs chain off the latest refresh
token. If the refresh ever fails (refresh token finally expired), it falls back to the
OAuth1 exchange — which works only residentially and is the intended re-bootstrap path.
Verified end-to-end: a full sync via the diauth path, rotated token persisted to Neon with
its refresh TTL reset to 30 days. The probe scripts (`refresh_probe.py`, `refresh_e2e.py`,
`probe.py`, `diag.py`) remain, runnable residentially, for re-verification; their CI
workflows and the `GARTH_TOKEN` repo secret were removed once the fix was live (§6).

**Result: the sync runs natively on Render again** — the hybrid home PC and the proxy
become fallbacks, not the primary path.

## 5. Interim solution — HYBRID (live 2026-07-08, now a fallback)

Ingestion moved to the one machine that Garmin trusts; everything else stayed put:

- **Home PC → Neon:** `scripts/home_sync.ps1` runs `daily_sync` against the production
  Neon database (process-scoped `DATABASE_URL` override; the local dev DB is untouched),
  scheduled via Windows Task Scheduler ("J2H4All Home Garmin Sync", 06:30 + 21:30 daily,
  missed-run catch-up). Verified end-to-end, including unattended runs.
  > **If this fallback is ever re-enabled, the `DATABASE_URL` override is the whole safety
  > story.** A *second*, forgotten task (`\J2H4All Daily Garmin Sync`) ran a different script
  > with **no** override, hit the stale local dev mirror, and — once the post-sync calendar
  > reconcile shipped — rewrote the production Google Calendar from a weeks-old plan every
  > morning. Days to diagnose. That script is now deleted and a hard `APP_ENV=production`
  > guard exists, but re-read the stale-mirror learning in ARCHITECTURE.md before scheduling
  > anything locally again.
- **Render never calls Garmin:** a `GARMIN_SYNC_ENABLED=false` flag guards the single
  chokepoint (`garmin.sync.run_sync`), turning the web *Sync now*, the Telegram sync
  command, and the pre-beat refreshes into honest no-ops (transient skipped result — no
  false failures, no staleness-watchdog interference). The doomed `j2h4all-cron` service was
  retired. Both user surfaces (Telegram + web) state that syncing runs from home.
- **Safety net:** the hourly tick's independent staleness watchdog alerts via Telegram
  if no successful sync lands for 30+ hours — covering home-PC outages.

**Accepted weaknesses (why this is interim):** the PC must be on around sync times, and
there is no remote "sync now" — limitations that motivated the native-refresh fix above.

## 6. Residential proxy — considered, built, then SUPERSEDED & REMOVED (2026-07-08)

Before §4.7, the investigation had reduced the solution space to one shape — *the exchange
must leave from residential IP space* — and the variant without home hardware was a
commercial residential proxy carrying Render's Garmin traffic. Plumbing was built
(`GARMIN_PROXY_URL`: when set, all Garmin traffic exits via the proxy) and a probe-workflow
validation rig was wired up. Economics were favourable (~$1/GB pay-as-you-go, a daily sync
is a few MB, one ~$5 top-up covers years).

**It was never needed.** §4.7 showed the exchange doesn't have to run from a datacenter at
all — the OAuth2 refresh grant on `diauth.garmin.com` isn't IP-blocked. So the premise above
turned out to be avoidable. Once the diauth fix was live, the `GARMIN_PROXY_URL` plumbing
became dead code and **was removed** (2026-07-08), along with the CI probe workflows and the
`GARTH_TOKEN` repo secret. Recorded here as the path not taken.

**Residual risks (of the diauth solution now in place):** an unofficial API is inherently
cat-and-mouse — Garmin could invalidate the refresh token, move/guard the `diauth` token
endpoint, or garth's flow could break. Mitigations in place: the rolling refresh token
(self-sustaining as long as sync runs at least monthly); the staleness watchdog (silent
failure is impossible — a >30h gap pings Telegram); the local diagnostic scripts
(`probe.py`, `diag.py`, `refresh_probe.py`, `refresh_e2e.py`, runnable residentially to
re-classify any future break); and `scripts/home_sync.ps1` — the residential re-bootstrap
that re-mints tokens from a trusted IP if the refresh chain ever fully lapses.

## 7. Lessons learned

1. **Identify the failing layer before iterating on a fix.** Every dead end (TLS
   impersonation aside, which productively eliminated a layer) came from reasoning at
   the application layer about a network-layer verdict. The decisive experiments all had
   the same form: *hold the request constant, vary one thing*.
2. **429 ≠ "slow down."** From a bot-protection front door, 429 can mean "your origin
   is categorically unwelcome" — issued before authentication is ever attempted. A
   401/403-vs-429 distinction carries real diagnostic weight.
3. **Test hypotheses even when you expect refutation.** The stale-token theory was
   cheap to test and its refutation (byte-identical token, live success residentially)
   is what made the IP conclusion airtight rather than assumed.
4. **Empirical probes beat anecdotes.** "Other projects run this on GitHub Actions"
   dissolved under a 10-minute experiment. The probe is now a permanent tool.
5. **Degrade honestly.** When a capability moves (or dies), every surface that exposed
   it should say so — the silent no-op button and the misleading 429 reply were both
   fixed to state the reality. Unattended systems additionally need a watchdog whose
   silence is meaningful.
6. **Unofficial APIs are a structural liability.** This entire class of problem
   disappears with an official push-based API — which Garmin reserves for businesses.
   Accepted as the cost of the product working at all; contained by keeping every
   Garmin touchpoint behind one client module and one chokepoint.
