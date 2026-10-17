# Deploying TRACE

Topology (decided with the owner): **web on Vercel**, **API + worker together in one container on an always-on host** (Railway, Render or Fly), **Postgres on Supabase**, **graph on Neo4j Aura**.
The API and the worker share one image and are supervised by `python -m app.run_all`. The worker must stay running (it polls the job queue every second), which is why it cannot live on Vercel Hobby (functions are request-bound; cron is once a day).

> **Open access.** TRACE currently has no login. A hosted instance lets *anyone who can reach the URL* use it, including creating projects, importing tickets, reading logs and running investigations that spend your OpenAI quota. Do not publish the URLs. Turning on real login is a separate piece of work (see "Authentication" below).

## 0. Order of work

1. Create the data stores (Supabase Postgres, Neo4j Aura).
2. Deploy the API container with its environment (section 2) and get its public URL.
3. Deploy the web app on Vercel with `NEXT_PUBLIC_API_URL` set to that URL (section 3).
4. Set `TRACE_ALLOWED_ORIGINS` / `TRACE_WEB_ORIGIN` on the API to the web URL and redeploy the API.
5. Re-point the external connections (section 4).
6. Verify (section 5).

## 1. Accounts and services

| Service | What for | Notes |
|---|---|---|
| Supabase Postgres | control plane (projects, tickets, jobs, stored logs) | use the **session pooler** connection string (port 5432), or the direct one if your host has IPv6. Not the transaction pooler (6543): migrations use a session advisory lock |
| Neo4j Aura | evidence graph | `neo4j+s://...` URI, username, password |
| OpenAI | investigator (`gpt-4o-mini`) | an API key with billing |
| GitHub App `traceai-app` | read repository code | already created (App ID `5348735`) |
| Always-on container host | API + worker | **must not sleep** (free tiers that sleep stop the worker and the log intake) |
| Vercel | the web app | any plan |

## 2. API container environment

Build: the `Dockerfile` in this repo (`python:3.11-slim`, `CMD python -m app.run_all`). The host injects `PORT`. Health checks: `GET /health/live` (cheap) or `GET /health/ready` (database, graph, vault).

Helpers (run locally, they print the exact value to paste):

```
.venv\Scripts\python -m app.cli gen-secret-key                      # -> TRACE_SECRET_KEY (new key for a new database)
.venv\Scripts\python -m app.cli pem-oneline secrets\traceai-app.pem  # -> GITHUB_APP_PRIVATE_KEY (single line, \n escapes)
```

| Variable | Value | Required |
|---|---|---|
| `TRACE_ENV` | `production` | yes |
| `AUTH_MODE` | `off` | yes (no login) |
| `ALLOW_OPEN_ACCESS` | `true` | yes: production refuses `AUTH_MODE=off` without it |
| `DATABASE_URL` | Supabase connection string | yes |
| `AUTO_MIGRATE` | `true` | yes (applies SQL and graph migrations at startup) |
| `NEO4J_URI` / `NEO4J_USERNAME` / `NEO4J_PASSWORD` | from Aura | yes |
| `NEO4J_DATABASE` | `neo4j` | yes |
| `OPENAI_API_KEY` | your key | yes |
| `TRACE_MODEL` | `gpt-4o-mini` | yes |
| `TRACE_SECRET_KEY` | output of `gen-secret-key` | yes. Keep it: losing it makes stored provider credentials unreadable |
| `TRACE_SECRET_KEY_VERSION` | `1` | yes |
| `TRACE_PUBLIC_ORIGIN` | the API's own public URL, e.g. `https://trace-api.up.railway.app` | yes. Used to build the log-source and webhook addresses shown in the UI |
| `TRACE_WEB_ORIGIN` | the web app's URL, e.g. `https://trace-web.vercel.app` | yes. Where the browser returns after the GitHub install |
| `TRACE_ALLOWED_ORIGINS` | the web app's URL (comma-separate more) | yes. CORS; PulseLab's origin is added automatically from `PULSELAB_URL` |
| `GITHUB_APP_ID` | `5348735` | yes |
| `GITHUB_APP_PRIVATE_KEY` | output of `pem-oneline` | yes (a hosted container has no key file) |
| `GITHUB_WEBHOOK_SECRET` | a new random string; the same value goes in the GitHub App settings | recommended |
| `PULSELAB_URL` / `SHOPFLOW_WEB_URL` / `SHOPFLOW_API_URL` | the demo apps' URLs | optional (links and CORS only) |

**Not needed on the host** (leave unset): `VERCEL_TOKEN`, `VERCEL_TEAM_ID` (only used by local smoke scripts; the Vercel token is entered in the project setup page and stored encrypted), `SUPABASE_*`, `LOCAL_AUTH_SECRET`, `GITHUB_APP_PRIVATE_KEY_PATH`, `GITHUB_REMEDIATION_*`.
Optional tuning variables exist (job leases, graph limits, run budgets); defaults are fine.

Host notes:
- **Railway**: New project > Deploy from GitHub repo (`trace-api`) > it detects the Dockerfile > add the variables > Settings > Networking > Generate domain. Health check path `/health/live`.
- **Render**: New Web Service > Docker > an always-on (paid) instance type > add the variables. Free instances sleep and will stop the worker.
- **Fly**: `fly launch` with the Dockerfile, set `auto_stop_machines = false` and `min_machines_running = 1`, then `fly secrets set ...`.

## 3. Web app (Vercel)

| Setting | Value |
|---|---|
| Repository | `trace-web` |
| Framework | Next.js (auto-detected); build `next build`, no output directory override |
| Environment variable `NEXT_PUBLIC_API_URL` | the API's public URL (no trailing slash). **Build-time**: change it and you must redeploy |
| Environments | set it for Production (and Preview if you use previews) |

The web app needs no other variable and holds no secrets. `trace-web` builds with `next build` (verified, see below).

## 4. External connections to re-point after deploying

1. **GitHub App** (GitHub > Settings > Developer settings > GitHub Apps > traceai-app):
   - Setup URL: `<API URL>/api/v1/connect/github/callback`, and tick **Redirect on update**.
   - Webhook: tick Active, URL `<API URL>/api/v1/webhooks/github`, secret = `GITHUB_WEBHOOK_SECRET`, subscribe to *Installation*.
2. **ShopFlow's log forwarder** (Vercel > shopflow-backend > Settings > Environment Variables): in the deployed TRACE open the project's Logs tab > "Send logs from your app" > set it up, then set `TRACE_INGEST_URL` and `TRACE_INGEST_SECRET` on ShopFlow to the values shown, and redeploy ShopFlow (with `docs/SHOPFLOW_LOG_FORWARDER.md` applied). The address and secret are new for every TRACE database, so the local ngrok values stop working.
3. **Vercel connection** (inside TRACE): Projects > the project > Connectors > Connect Vercel with the access token (and team id). Stored encrypted in the new database.
4. **GitHub connection**: Connectors > Connect GitHub (or "Use this" for the existing installation).
5. Stop ngrok; it is no longer in the path.

A fresh database starts empty. Projects, connections, tickets and stored logs from the local database are **not** carried over; recreate the project through the setup page (about two minutes) and re-import tickets. Copying the local data is possible with `pg_dump` plus the same `TRACE_SECRET_KEY`, but is rarely worth it for a POC.

## 5. Verify

```
curl https://<API>/health/ready
```
Expect `"status":"ready"`, `database: ok`, `graph: ok`, `vault: ok`, `model: configured`, and `access: open (no login ...)`.

Then in the web app: create a project, connect GitHub and Vercel, pick the repository and the Vercel project, open Logs and set up "Send logs from your app", send one request through ShopFlow and watch the events arrive.

## Authentication (not built)

Supabase JWT verification exists in the API (`AUTH_MODE=supabase`, with `SUPABASE_URL` / `SUPABASE_JWKS_URL`), but the web app has no login or sign-up pages and the verifier has not been run against a real Supabase issuer. Moving off open access needs: a Supabase client in the web app, login and sign-up pages, sending the access token with every API call, an invitation screen, and `ALLOW_OPEN_ACCESS` removed.

## What was verified for this guide (2026-10-17)

- `next build` succeeded for `trace-web` (Next.js 16.4.2, 10 routes) after a clean `npm ci` from the lockfile.
- The supervisor restarts a crashed child and stops cleanly (`tests/test_run_all.py`); the API started and answered health checks under it on this machine.
- The one-line `GITHUB_APP_PRIVATE_KEY` form is accepted by GitHub (real call).
- Production-mode configuration rules (open-access opt-in, Supabase settings only when real auth is used) are covered by `tests/test_auth_off.py`.
- **Not verified**: building the Docker image (Docker is not running on the development machine) and an actual deployment to a host, Supabase or Aura.
