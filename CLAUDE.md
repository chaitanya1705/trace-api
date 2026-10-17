# CLAUDE.md — TRACE (trace-api)

Applies to this project only. Repos: trace-web (https://github.com/chaitanya1705/trace-web.git), trace-api (https://github.com/chaitanya1705/trace-api).

## Docs (read in this order)
1. `docs/PLAN.md` - **user decisions that override `TRACE/`**. Read first. Where it conflicts with `TRACE/`, `docs/PLAN.md` wins.
2. `TRACE/` - the original plans (Context, PRD, architecture, ontology). They still mention dropped scope (Settings, Azure/AWS/GCP/Railway/Azure DevOps/Jira/ServiceNow/Azure Boards/Supabase connector); ignore those parts.
- `task.md` (repo root) is the task backlog and progress log. It is the source of truth when running in the cloud.

## Git
- Work only on branch `feature/trace-implementation`; keep pushing to it. A PR to `main` exists; if not, create it.
- Never mention Claude/AI in commits or PRs: no Co-Authored-By, no "Generated with" lines.
- Never commit secrets, `.env`, `node_modules`, `.venv`. No force-push, no `--no-verify`.

## After every task
1. Update `task.md`: set the task status (DONE / IN_PROGRESS / BLOCKED), and add an "Agent progress log" entry with date, commits, exact checks and results, limitations, **what remains** and the next task. Follow the Workflow section in the file.
2. If the task touched the other repo, update its `task.md` the same way so both stay in sync.
3. Commit all changes (including `task.md`), push to `feature/trace-implementation`.
4. Tell the user what is done, what remains, and the PR link.

## Decisions that override TRACE/ (summary; details in docs/PLAN.md section 1)
- Repos: `trace-api` (this, Python/FastAPI) and `trace-web` (Next.js).
- **Settings is out of scope** (no settings endpoints/UI). **Real auth is deferred**: use `AUTH_MODE=off` (no login, fixed dev user, `X-Dev-User` header switches user). Do not spend time on Supabase/login work.
- **Only GitHub (code) and Vercel (hosting) connectors**, plus generic webhook/upload. No ticket provider: tickets come from manual form/API/webhook; the change ticket is a manually linked reference. No Jira writer.
- Local dev uses plain Postgres (`DATABASE_URL`), Python 3.11. Tests use an embedded throwaway Postgres (`pgserver`); no Docker needed.
- Investigate is always an explicit click; never auto-start a run. Never fake logs/metrics/verification; unknown stays unknown.

## Demo apps (built separately by a collaborator - do NOT build them here)
- **ShopFlow** (FastAPI backend + React frontend on Vercel, deliberate defects) is the system TRACE investigates. **PulseLab** (browser-only React app) sends traffic to ShopFlow and exports symptom tickets as JSON.
- TRACE reaches them only through placeholders in env: `PULSELAB_URL`, `SHOPFLOW_WEB_URL`, `SHOPFLOW_API_URL` (all optional; shown by `GET /api/v1/integrations/demo` as `configured:false` when blank). Never hardcode URLs, never mock their data. ShopFlow's logs reach TRACE via the Vercel log drain, not a ShopFlow endpoint.
- PulseLab's JSON export is the expected input of ticket intake (T-010). Never expose evaluator data/expected defects to the investigator.

## How to run
```
python -m venv .venv && .venv/Scripts/pip install -r requirements.txt   # or .venv/bin on Linux; always run via the venv python
.venv/Scripts/python -m pytest -q              # DB tests start their own Postgres
.venv/Scripts/python -m ruff check app tests
.venv/Scripts/python -m app.openapi_export     # regenerate docs/openapi.json (CI: add --check) after changing any API
cp .env.example .env  # AUTH_MODE=off, DATABASE_URL=...
python -m app.serve   # API on Windows dev (Linux/prod: uvicorn app.main:app_factory --factory) (applies pending migrations at startup; AUTO_MIGRATE=false to disable);  python -m app.worker_main  # worker
```
Use the Write/Edit tools rather than shell heredocs for multi-line files.

## State at 2026-10-16 (task.md has the authoritative log)
Done: config/errors/health/migrations, vault, projects/environments/services/members, durable job queue, GitHub+Vercel connections (verify/discover/rotate/disconnect), log scope, repository bindings + mappings, wizard drafts, GitHub webhook. 55 tests pass.
Next (Postgres-only, no external accounts needed): T-009 ingestion + Vercel drain endpoint + stored log search, T-010 tickets + explicit Investigate (accept PulseLab JSON). Blocked on credentials: T-007 (Neo4j), T-012 (OpenAI), real GitHub App/Vercel smoke tests. Provider tests use labeled fixtures (httpx.MockTransport); say so rather than claiming real-account verification.

## POC posture
This is a proof of concept: do not add security hardening beyond what exists. One GitHub App is fine (`Settings.remediation_app` falls back to it). See docs/PLAN.md section 9.

## No mock data (user rule)
The backend has no seeded/mock data and must never add any (test fixtures live only under tests/ and are labeled). When a
screen's API lands here, the matching screen in trace-web must drop its mock data in the same change: Tickets is ready now
(`GET/POST/PATCH /tickets`, `POST /tickets/import/pulselab`); Projects, connections, wizard drafts, log scope, repositories
are ready too. Screens whose API does not exist yet (Investigate, Changes, Observability, Home counters) should show an
honest empty/unavailable state, not invented numbers.

