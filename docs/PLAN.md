# trace-api — build plan

Backend for TRACE (Python / FastAPI). Companion repo: `trace-web` (Next.js).
Authority order (unchanged from the spec package in `Plan's And Artifacts/Plans/TRACE/`):
user decisions > PRD > ontology > architecture > tasks. This file records the **user decisions that
amend the package** and the reverse-engineered process the backend must serve.

## 1. Decisions that amend the spec (2026-10-16)

| # | Decision | Impact |
|---|---|---|
| A1 | Two repos: `trace-api` (Python) and `trace-web` (Next.js). Spec's names were `trace-backend`/`trace-frontend`. | Layout in A3 is kept; only repo names change. |
| A2 | **Settings is dropped for now** (rules, approvers, members, roles, audit UI, credentials page, preferences, notifications, org admin). | No `/settings` endpoints in this build. Backend keeps *internal* defaults (see §4). Roles remain as a backend concept; there is no UI to edit them. |
| A3 | **Connectors: only Vercel (hosting/runtime) and GitHub (code).** Azure, AWS, GCP, Railway, Azure DevOps, Jira, ServiceNow, Azure Boards, Supabase add-on are out. "Other" (generic webhook/upload) stays because P2 telemetry ingestion depends on it. | R-016 table shrinks to two rows. T-021 a–c, e–j are cancelled, not deferred. `Vercel` replaces the cloud step. |
| A4 | No ticket provider ⇒ tickets enter via manual form, authenticated API and webhook only. The wizard's *Tickets* step is removed. Change-ticket requirement (R-012) is satisfied only by a **manually linked external ticket URL/ID**. The Jira writer (T-018) is cancelled. | Wizard: Basics → Hosting (Vercel) → Log scope → Code (GitHub) → Review. |
| A5 | Everything else in the PRD is retained: Home, Tickets, Investigate, Changes & PRs, Observability (+Analytics), Projects (wizard, Code, Log scope, Architecture, Logs, Team), TRACE Bot, Since-you-were-away, usage meters. | See §6 task map. |
| A6 | Local toolchain has Python **3.11** (not 3.12). `requires-python >=3.11`; no 3.12-only syntax. Docker daemon is not running; a native Postgres is on :5432. Neo4j/Supabase/OpenAI are external and **not yet provisioned**. | Everything touching them sits behind an interface with a real adapter and an in-memory/test adapter (test adapter never reachable in production config). |

Unchanged hard rules from the spec worth restating because they drive design: Investigate is a click, never
automatic (D-01); Neo4j is the only retrieval source for the model; recovery follows *deployment* not merge
(D-03); approvals bind to patch hash; TRACE never merges/deploys; no fake timers/metrics/evidence.

## 2. Reverse-engineered process (what the prototype actually does, minus its simulation)

Prototype = `Artifacts/index.html`, final router = NAV7 + V12–V17 overrides. Mock timers/arrays are replaced by jobs.

```
Landing/Login ──► Home (Needs-you queue, active runs, project health, recent changes)
                    │
 Projects ──────────┤  wizard: Basics → Hosting(Vercel) → Log scope → Code(GitHub) → Review
   │  connector verify (staged real checks) → discover resources → pick scope per environment
   │  Code tab: pick repos+branch → resolve SHA → import → Graphify build → Neo4j publish
   │  Architecture tab: system map (service/resource graph + lens by run) · code graph (d3) · (DB ERD = P4)
   │  Logs tab: stored search | explicit "Fetch from source" → ingest → show
   ▼
 Tickets ── intake (form/API/webhook) ── assign project (no run!) ── [Investigate click] ──► Run
                                                                                         │
 Run = persisted state machine: scope → retrieve → hypotheses → test → impact → resolution → report
   each step = tool execution (query_logs, list_deploys, read_diff, read_file, map_impact, assess,
   propose_change, ask_user, manual_steps, coverage) → SSE events, durable seq
   side panels: Impact map · Timeline · Hypotheses · Evidence · coverage checklist · current finding
   human pause: ask_user / manual_steps → response resumes exactly that request
   replay = read-only event playback; rerun = new linked run
                                                                                         │
 Changes & PRs ◄── propose_change ── PatchVersion(hash) ── approvals (not the investigator) ──► draft PR on trace/*
   modify = new version, approvals cleared · blast radius · freeze window / emergency · merge happens in GitHub
                                                                                         ▼
 Observability: merge → awaiting_deployment → (Vercel deployment of merged SHA + traffic) → watching →
   recovered | regressed | inconclusive | unable_to_verify ── Reopen = explicit new run
 Analytics (P4): reviewed labels → accuracy / abstention / time-to-cause / PR acceptance
 TRACE Bot (panel everywhere): scoped to run/project, cites evidence, can propose patch edits via PatchVersion
```

Prototype behaviours that are **simulation and must not be ported**: assignment auto-starting a run; `setTimeout`
verification; "Approve as <name>"; `simWatch`; `mergeFix` demo button; random log rows; hardcoded GUID/ARN regexes
as truth; typed-word streaming delay; fake `USAGE` ticker.

## 3. Vercel + GitHub specifics (these replace the multi-cloud design)

**GitHub (code, P1).** Two separately provisioned GitHub Apps: *TRACE* (Metadata/Contents/PRs **read**) and *TRACE
Remediation* (Contents/PRs write, P3 only, never given to indexing jobs). Per-job installation tokens (1 h) minted
from app JWT; webhook signature (HMAC-SHA256) + delivery-id dedupe. Fine-grained PAT = labeled trial mode.
Fetch tarball at exact SHA (no clone hooks/LFS/submodules). `trace/*` branch rule: backend-enforced; GitHub ruleset
snippet shown to the user (token scopes cannot enforce branch patterns).

**Vercel (hosting + runtime, P1 connect / P2 logs / P3 deploys).**
- Auth modes: *integration* (read-only, preferred) or *access token* (always warned: can write).
- Discovery: team → projects → environments (`production`, custom envs, previews opt-in).
- **Deployments carry the Git commit SHA** (`meta.githubCommitSha`) — this gives the Deployment→Revision mapping
  that the ontology otherwise treats as a hard problem. Mapping method recorded as `provider_metadata`; absent
  SHA ⇒ `ambiguous_revision` gap.
- Logs: runtime logs are only retained briefly (plan-dependent), so the supported path is a **Log Drain to
  `POST /v1/ingest/vercel/{connection_id}`** (signature header verified with a per-connection secret generated by
  TRACE). "Fetch from source" for the Logs console = bounded read of Vercel runtime-logs API → ingest → graph.
- Plan limits (Hobby: no drains) surface as `EvidenceGap(permission_missing|stale)`, never as green.
- Exact endpoints/permissions/limits are verified at implementation time (T-001 spike) against current Vercel and
  GitHub docs; the spec forbids trusting the prototype's claims.

## 4. Internal defaults replacing the dropped Settings

- **Policy** (R-012) is a versioned row per `(project, environment)` created automatically with spec defaults:
  production = 2 independent approvers + linked change ticket + freeze-window support; others = 1 non-author
  approver. No edit UI; an Admin-only `PUT /projects/{p}/policies/{env}` exists but is not exposed in the web app
  yet. Single-user deployments can investigate/propose but cannot satisfy independent approval (spec).
- **Roles** Owner/Admin/Approver/Investigator/Viewer exist in the schema; project Team tab edits project
  membership only. Invitations are API-only for now.
- **Audit** is written for every permission change, connection change and external write; no read UI (an
  Admin-only `GET /audit` is kept for operators).
- **Credentials**: encrypted storage + rotate/disconnect live inside the connector endpoints. No inventory page.
- **Notifications / preferences / on-call**: not built. "Since you were away" is derived from run/change events
  with per-user read state (it is a sidebar feature, not Settings). Theme/rail persistence is a `trace-web`
  concern (cookie/localStorage is fine for UI-only prefs; timezone stays a header sent by the client).

## 5. Technology and layout

Python 3.11+, FastAPI, Pydantic v2, psycopg 3 (async pool), plain versioned SQL migrations (tiny runner),
`neo4j` official driver, `PyJWT[crypto]` (Supabase JWKS), `cryptography` (AES-256-GCM), `httpx`, `openai`
(`gpt-4o-mini`, pinned in `TRACE_MODEL`), `graphifyy` behind `GraphifyAdapter`, `pytest`, `ruff`, `mypy`.
Layout follows architecture A3 under `trace-api/app/…` (api/v1, domain/*, connectors/{base,github,vercel,generic},
graphify, graph/{schema,queries,repositories,migrations}, orchestration, skills/*, jobs, storage, events, audit).
Auth: Supabase JWT verified server-side; the backend adds authorization (membership + project grants). Control
state in Supabase Postgres; evidence in Neo4j; durable queue = Postgres `FOR UPDATE SKIP LOCKED`; outbox → graph.

Interface rule: every external dependency (Supabase JWKS, Neo4j, OpenAI, GitHub, Vercel, artifact storage) is a
`Protocol` with a production adapter. Test doubles live under `tests/` only and are labeled; production config
cannot select them.

## 6. Task map (spec task IDs → this build)

| Phase | Task | Scope in this build | Status |
|---|---|---|---|
| P1 | T-001 spike | pin versions, Graphify CLI/schema check, GitHub/Vercel doc review, host limits | TODO (needs network + accounts) |
| P1 | T-002 | repo scaffold, config validation, error envelope, correlation IDs, health, SQL migrations, OpenAPI export | **start here** |
| P1 | T-003 | JWT verification, memberships, project grants, audit writer, `/me` | next |
| P1 | T-005 | projects, environments, services, repo bindings, project drafts (wizard), team, readiness | next |
| P1 | T-006 | jobs/outbox/leases, encrypted credentials, GitHub + Vercel connection lifecycle (verify/discover/rotate/disconnect), webhooks | next |
| P1 | T-007 | safe SHA fetch, Graphify adapter, normalizer, Neo4j schema/publish, manifests | needs Neo4j |
| P1 | T-008 | graph/source/architecture APIs | needs Neo4j |
| P2 | T-009 | generic + Vercel-drain ingestion, stored log search, source fetch | |
| P2 | T-010 | tickets + explicit Investigate (active-run uniqueness) | |
| P2 | T-011 | retrieval tools (typed Cypher templates), manifests, citations | |
| P2 | T-012/013 | orchestrator, SSE, human requests, replay | needs OpenAI |
| P2 | T-014 | bot, Home aggregate, Since-you-were-away, usage meters | |
| P3 | T-016/017 | changes, patch versions, approvals, policy | |
| P3 | T-018 | sandbox adapter (unavailable state if unprovisioned), GitHub draft-PR writer; **Jira writer cancelled** | |
| P3 | T-019/020 | Vercel deployment correlation, verification, reopen | |
| P4 | T-022/023/024 | schema/ERD extraction, analytics + review labels, parity gate; **T-021 reduced to Vercel+GitHub hardening** | |

## 7. Open items needing the user (none block T-002/T-003/T-005/T-006)

1. Neo4j instance (Aura Free or local) credentials → needed for T-007.
2. Supabase project (URL, JWKS, service key, DB URL) → needed to run auth/DB for real. Until then the local Postgres
   on :5432 can host the control schema for development.
3. OpenAI key → T-012.
4. GitHub App(s) and a Vercel integration/test token → T-006 real verification.
5. Contract hand-off to `trace-web`: OpenAPI at `/openapi.json`, snapshot committed to `docs/openapi.json`; the web
   repo generates TS types from it (spec A5).
