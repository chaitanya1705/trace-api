# TRACE — coding-agent context

Version 1.0 · 12 October 2026 · Status: implementation specification, not implemented product.

## Read first

TRACE is a standalone incident investigation and change-impact workspace. It helps a user investigate a ticket, connect runtime evidence to deployed code, compare explanations, understand affected systems, propose a correction, and verify recovery. Build only TRACE. No companion simulation applications are in this specification.

Read this file, then the relevant PRD requirement, architecture contract, ontology section, and task. Maintain `tasks.md` after each implementation session. Do not mark a task complete because its UI renders: its acceptance checks must pass. The HTML prototype is a visual/interaction reference containing simulated data and superseded code; it is not a working backend.

## Document authority

1. Subsequent explicit user decisions override this package; update affected documents together.
2. `PRD.md`: externally visible behavior, feature scope, phases, acceptance criteria, prototype coverage.
3. `ontology.md`: authoritative graph semantics, identity, provenance, temporal scope, traversal.
4. `architecture.md`: implementation boundaries, API/job/data contracts and operational controls.
5. `tasks.md`: dependency-ordered delivery status, verification records and known implementation gates.

Do not resolve a material contradiction by silently dropping a requirement. Record the decision and its impact. Examples, provider limits and mock figures are not contractual targets.

## Settled decisions

- A user clicks **Investigate** from Tickets to start a run. Ingestion, assignment, refresh and page opening never start a run automatically.
- Retain all active prototype capabilities in the phased roadmap. Later-phase actions are visibly unavailable with a reason until implemented.
- Final top-level navigation: Home, Tickets, Investigate, Changes & PRs, Observability, Projects. Settings is accessible in the toolbar. Landing and authentication are separate surfaces.
- Neo4j is the investigator's sole evidence retrieval source. Connectors ingest before the investigator analyzes. Supabase stores application/control state, not a parallel investigation search corpus.
- Graphify (`graphifyy`, command `graphify`) is a replaceable code-extraction dependency behind a backend adapter. TRACE does not require the Claude Code skill or hooks to run.
- Python/FastAPI/Pydantic backend; GPT-4o mini (`gpt-4o-mini`) for bounded model tasks; Next.js/TypeScript frontend. Provider availability must be checked at implementation; never silently switch the selected model.
- Parent orchestration is a persisted state machine. Skills/subskills are typed modules, not a fleet of independently deployed agents.
- Graph/source evidence is pinned to exact repository revisions. Unknown deployment mapping is a visible gap.
- Extracted code dependency, potential propagation, and observed runtime impact have distinct semantics.
- Cloud/database connectors collect permitted metadata/logs. They do not query customer database rows. Schema views derive from source/migrations and disclose possible drift.
- Read-only investigation can proceed autonomously after the initial click within budget. External writes require an authenticated authorized action and governance checks.
- Approved draft PR creation/update on `trace/*` branches and separately authorized change-ticket creation are supported in phase 3. TRACE does not merge PRs or deploy/rollback production in this baseline.
- Recovery follows deployment/exposure evidence, not merge alone. No evidence or insufficient traffic cannot establish recovery.
- Tickets, investigation runs, findings and resolution states are separate.
- Provider credentials and arbitrary repository instructions never enter the model's tool authority.

## Deployment and cost posture

Start with one Vercel frontend project and one Railway backend deployment containing supervised API and worker processes. Supabase provides Auth/Postgres/Storage; Neo4j hosts evidence. A durable Postgres queue avoids a Redis requirement. Concurrency, repository size, evidence retention, token use and connector scans are capped. These are low-resource design choices, not guaranteed free-tier capacity.

## Delivery phases

- P1: shell/auth, membership, projects, GitHub, durable jobs, Graphify/source indexing, graph/source views, basic credentials/audit/settings.
- P2: ticket intake, generic runtime ingestion, user-started investigations, GraphRAG, human checks, hypotheses, impact, timeline, citations and bot.
- P3: changes/patches, approvals/environment policy, isolated patch validation, approved PR/change-ticket writes, deployment correlation, recovery tracking.
- P4: remaining provider integrations, advanced schema extraction, analytics/review workflows, notification delivery, expanded organization settings and polish.

Basic isolation, secret handling, actual error states, keyboard access and responsive layout are mandatory in every phase; they are not P4 extras.

## Operating instructions for the implementation agent

Work in two repos, `trace-frontend` and `trace-backend`. Keep this specification under a shared `docs/TRACE/` path in each or designate one authoritative copy and link the other; avoid divergent copies. Generate TypeScript API types from backend OpenAPI. Preserve requirement IDs in tests and task records. Run relevant unit/integration checks and browser tests at phase gates. Never use hardcoded success timers, invented metrics, fake connection checks or generated evidence in production paths. Test fixtures must be labeled and inaccessible as real evidence.

Do not install/run connected repositories during indexing. Do not give the model raw Cypher, shell, database credentials or permission to expand project scope. UI display filters do not authorize access. Keep source excerpts and model/tool logs redacted. Patches require immutable versions and approval invalidation on edit.

## First implementation entry point

Start T-001 through T-005 in `tasks.md`. A deployment/compatibility spike must validate selected package versions and hosting limits before broad feature work. Planning statuses are not implementation completion. The final package includes a static prototype coverage review; it does not certify a working application.
