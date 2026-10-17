# TRACE implementation backlog and validation record

Version 1.0 · 12 October 2026 (backend progress updated 2026-10-16). Task statuses below are authoritative; tasks without a progress-log entry are NOT STARTED. Documentation drafting/audit is distinct from implemented/tested software. Coding agent owns status updates.

## Workflow

Statuses: TODO, IN_PROGRESS, BLOCKED, DONE. Select a task only when dependencies pass. On each update record date, commit(s), changed requirements, exact checks/results, limitations and next task. A screenshot or green UI toast is not completion evidence. Preserve IDs when splitting tasks (e.g. T-012a). If scope changes, update PRD/architecture/ontology and affected tests together. Do not remove later-phase requirements to make current release appear complete.

## P1 — Foundation and code knowledge

### T-001 · Compatibility and deployment spike · TODO
Dependencies: none. Requirements: all architecture foundations.
Deliver: lock tested Python/Node/Next.js/FastAPI/Neo4j/OpenAI SDK/Graphify versions; verify chosen model availability; validate Graphify CLI/output schema on Python+TypeScript fixtures; check hosting memory/process/SSE support, provisioned database capabilities and backup limits. Record exact official provider docs/version/date for active integrations. Validate Graphify redistribution notices for shipped dependencies. No token-saving/performance claim adopted without local measurement.
Acceptance: reproducible local build; real Graphify sample yields expected identities/edges/source locators; tested version matrix; incompatible capability fails visibly. Blocks enabling providers until their own spike passes.

### T-002 · Backend/web repositories, CI and contracts · IN_PROGRESS (backend half)
Dependencies: T-001. Requirements: R-002, architecture A3/A5.
Deliver: two repo layouts, config validation, secret-free env examples, lint/typecheck/test commands, migrations, OpenAPI→TS generation and CI drift check. Add correlation IDs, safe error envelope, live/ready health.
Acceptance: clean checkout installs locked versions; invalid config fails with field name only; frontend compiles generated types; CI runs meaningful checks.

### T-003 · Auth, membership, role policy and audit · IN_PROGRESS
Dependencies: T-002. Requirements: R-001,R-019.
Deliver: sign-in/signup/reset/session handling, invitation/membership flow, project grants, JWT verification, server authz, RLS, audit and own-preferences endpoints. Owner/Admin/Approver/Investigator/Viewer permissions tested independently. Audit export restricted.
Acceptance: unauthorized/cross-project/revoked tests; login deep link return; secret-free logs; Admin privilege not confused with independent patch approval.

### T-004 · Responsive shell and landing · TODO
Dependencies: T-003. Requirements: R-001,R-002,R-020.
Deliver: final prototype navigation, breadcrumbs/scope/command palette, theme, mobile drawer, stable routes, query cache, local table scroll and empty/error components. Landing truthful sample labels.
Acceptance: keyboard/reduced-motion and 1440/1024/768/390/320px browser checks; no whole-page overflow; scope change clears unauthorized cached data.

### T-005 · Projects, wizard and domain scope · IN_PROGRESS (backend largely done; web wizard pending)
Dependencies: T-003,T-004. Requirements: R-015,R-019.
Deliver: project list/detail/team, draft wizard in prototype order, unique names, owner/envs, repository-service mappings, ready-by-capability checklist, review/edit/resume and future-provider disabled states. Mirror non-secret scope via outbox.
Acceptance: skip/resume persists; project name race conflict; one-repo-many-services support; membership enforcement.

### T-006 · Durable jobs, outbox and GitHub credentials · IN_PROGRESS (outbox applier needs Neo4j)
Dependencies: T-005. Requirements: R-016,R-019.
Deliver: queue leases/heartbeat/cancel/retry/dead-letter, cross-store receipts/reconciliation, encrypted versioned credentials, GitHub cloud read installation, R-016 provider capability gates and separately provisioned future remediation identity, real verify/discover, rotate/disconnect, inventory and audit.
Acceptance: process kill during graph commit reconciles once; credential replacement failure preserves working version; disconnect stops queued work; webhook signature/deduplication; no credentials in browser/model/events.

### T-007 · Graphify/source indexing and graph migrations · IN_PROGRESS (ontology layer done; Graphify adapter/normaliser/checkout remain)
Dependencies: T-006. Requirements: R-017; ontology O1–O7.
Deliver: safe SHA checkout, extraction adapter/limits, normalizer, deterministic IDs, graph constraints/indexes, chunks, build versioning/staging/publication, manifest, history and partial coverage. No customer script execution.
Acceptance: Python/TS fixtures; identical symbol names/revisions isolation; idempotent rebuild; invalid endpoints fail; failed rebuild leaves previous graph; cleanup after cancellation; cited source exact hash.

### T-008 · Code/source/architecture graph experience · TODO
Dependencies: T-007,T-004. Requirements: R-009,R-017,R-020.
Deliver: repository browser/import states, archived filters/unsupported TFVC, source viewer/path/revision links, code settings and diagnostics, D3 graph controls, communities/most-connected, uses/used-by, bounded reverse blast and accessible table.
Acceptance: source selected from correct manifest; full graph not loaded by default; relation filters/pan/zoom/fit/keyboard work; static reachability never shown as runtime degradation.

## P2 — Real investigations

### T-009 · Generic telemetry ingestion and log console · TODO
Dependencies: T-006,T-007. Requirements: R-018,R-016.
Deliver: signed/scoped generic events API, redaction/dedupe/time mapping/gaps, durable normalization, generic deployment events and service/environment/revision mapping required for P2 code correlation, stored logs search, explicit bounded source-fetch contract, cursor and freshness/coverage UI.
Acceptance: malformed/oversize/duplicate/out-of-order events; cursor doesn't advance before graph acknowledgement; zero rows vs no source; no cross-project query results.

### T-010 · Tickets and explicit Investigate · IN_PROGRESS (tickets done; Investigate command waits for T-012)
Dependencies: T-009,T-005. Requirements: R-004.
Deliver: manual/API intake/revisions/dedupe, attachments text cap, assignment/filters/owner/status, graph ticket sync, Investigate/rerun command and active-run uniqueness.
Acceptance: assignment/refresh/page open enqueue no investigation; two simultaneous clicks one run; missing scope prompt; required evidence sync pending; ticket close independent of run.

### T-011 · Neo4j retrieval tools and GraphRAG evaluation · TODO
Dependencies: T-007,T-009. Requirements: R-008,R-009; ontology O8–O12.
Deliver: exact/full-text retrieval, source/deploy/diff lookups, typed graph traversal, rankings/contradictions, manifests/citations/gaps/limits, parameterized templates; no arbitrary model query.
Acceptance: corpus tests for missing logs, wrong revision, misleading names, cycles, late evidence, ambiguous dependencies, graph isolation and budget truncation; record retrieval recall and cost rather than assumed gains.

### T-012 · State-machine orchestrator and investigation workspace · TODO
Dependencies: T-010,T-011. Requirements: R-005,R-006,R-008,R-009.
Deliver: parent/skills, GPT-4o mini structured outputs, retry/repair/budgets, durable steps/assessments/findings, SSE resume, lists/context panels/timeline, historical step snapshots, read-only replay/cancel/rerun.
Acceptance: model invalid citation rejected; provider outage/worker restart/stream drop handled; historical context excludes later evidence; competing hypotheses and insufficient-evidence completion; no hidden reasoning exposed.

### T-013 · Human questions and manual diagnostics · TODO
Dependencies: T-012. Requirements: R-007.
Deliver: pending requests/schema options/manual instructions, eligible actor responses, raw-redacted observation provenance, pause/resume/expiry.
Acceptance: duplicate/stale/unauthorized submission denied; one resume; human result never becomes provider telemetry; user instructions cannot grant tools authority.

### T-014 · TRACE Bot, Home and user work queues · TODO
Dependencies: T-012,T-013. Requirements: R-003,R-010.
Deliver: scoped chat/history/versioned regenerate/copy/stop/citations/follow-ups, bounded follow-up jobs, real Home counters/Needs-you/progress/project gaps and unread events.
Acceptance: interrupted answer marked partial; citations resolvable; scope switch safe; completing question removes matching queue item; no mock percentages or timers.

### T-015 · P2 end-to-end gate · TODO
Dependencies: T-008 through T-014. Requirements: R-001–R-010,R-015–R-020 as phase-applicable.
Deliver: documented authorized fixture project/run walkthrough, scope-isolation/restore/reconnect/cancellation/evaluation results, source retention check, screenshots at target widths.
Acceptance: actual ticket→click→evidence→hypotheses→impact→finding→question flow; independent reviewer checks citations and uncertainty; all enabled buttons have real behavior; baseline usage measured.

## P3 — Controlled remediation and verification

### T-016 · Changes, immutable patch proposals and diffs · TODO
Dependencies: T-015. Requirements: R-011,R-010.
Deliver: observed changes ingestion/diff at base manifest, proposal and patch versions/hash, rationale/risk/impact, diff annotations/maximize/edit, bot patch cards, stale-base detection.
Acceptance: edit creates new version; source paths/repo scope validated; no patch applied on display; proposed test never presented as executed.

### T-017 · Environment governance and approval controls · TODO
Dependencies: T-016. Requirements: R-012,R-019.
Deliver: policy versions, scoped approvers/CODEOWNERS/author exclusion, count, target branch, freeze timezone, emergency reason/review, required ticket, approval/revoke, settings UI.
Acceptance: self-approval/impersonation prevented; edit invalidates approval; delayed dispatch rechecks policy; DST freeze tests; missing independent reviewers blocks rather than fakes approval.

### T-018 · Isolated validation and authorized external writers · TODO
Dependencies: T-017,T-006. Requirements: R-011,R-012.
Deliver: bounded secret-free sandbox adapter (explicit unavailable state if host not provisioned), test artifacts, GitHub draft PR create/update, separately authorized minimal Jira change-ticket writer (P3), operation reconciliation/audit. Broad ticket-provider intake/discovery remains P4.
Acceptance: timeout doesn't duplicate PR/ticket; unauthorized branch/writes denied; base movement requires re-review; sandbox no provider/DB keys; TRACE cannot merge/deploy/rollback.

### T-019 · Deployment correlation and recovery · TODO
Dependencies: T-018,T-009. Requirements: R-013.
Deliver: deployment ingestion/API, revision overlap/exposure mapping, criteria/baseline/window checks, observations detail/graphs, watching/recovered/regressed/inconclusive states, explicit reopen.
Acceptance: merge alone insufficient; low traffic/absent latency data cannot pass; regression reopening creates new run; ticket not auto-closed; confounding changes disclosed.

### T-020 · P3 governance and recovery gate · TODO
Dependencies: T-016–T-019.
Deliver: authorized sandbox-repo end-to-end test, approval race/revocation/stale hash/uncertain external result/recovery coverage evidence.
Acceptance: actual approved draft PR and separately scoped change ticket tested with configuration; no production execution; checkpoint replay cannot issue repeated writes.

## P4 — Catalog and advanced product completeness

### T-021 · Remaining source connectors · TODO
Dependencies: T-015,T-006; P3 permissions before writes. Requirements: R-016,R-018.
Subtasks: T-021a Azure; b AWS; c GCP; d Vercel; e Railway; f Azure DevOps repositories; g Jira; h ServiceNow; i Azure Boards; j Supabase log/history connector. Each preserves the R-016 provider setup fields/modes with honest capability gating and gets official-doc capability review, auth/scopes, discovery, pagination/cursors, rate limits, error states, scoped UI form, real verify and account smoke test. Ticket adapters explicitly separate intake from change-ticket output. Generic Other remains supported.
Acceptance per adapter: actual bounded authorized read ingests to graph; unsupported plan/capability disabled honestly; token refresh/expiry/revoke handled; customer database rows never queried; write enabled only with policy and explicit action.

### T-022 · Schema/config extraction and extended architecture · TODO
Dependencies: T-007,T-021. Requirements: R-009,R-016,R-017.
Deliver: supported source schema/migration parsing, schema viewer/locators/relations, confidence/drift display, system graph cross-repo evidence linking. Optional docs semantic extraction requires explicit project consent, data scope and model/cost choice; disabled until tested.
Acceptance: schema intent not claimed deployed fact; community not treated as service; model-derived links marked; invalid schema/unsupported file yields coverage gap.

### T-023 · Reviews, analytics, notifications and advanced settings · TODO
Dependencies: T-015,T-019. Requirements: R-003,R-014,R-019.
Deliver: human review workflow and formulas/cohorts/sample sizes, audit exports, per-user preferences, in-app and configured email questions/fixes/PR/digest, deduped delivery/retries, unread sidebar, separate usage byte/tool/token meters, configured on-call metadata if used.
Acceptance: N/A for no data; no model self-grading; timezone/daily reset consistent; opt-out honored; notification links authorized; email service selection/configuration required before email toggle claims delivery.

### T-024 · Full prototype parity and operations gate · TODO
Dependencies: all prior phase tasks.
Deliver: independent validation of every PRD coverage row, browser route/action checks, all widths/keyboard/reduced-motion, load/retention/restore drills, evaluated model costs and provider capability docs, final gap register.
Acceptance: no unexplained missing item; deferred/unsupported features visible; all enabled actions verified; no fabricated provider/graph/metric/recovery success. Update this file with reviewer/date/test evidence and known limitations.

## Validation checklist for every phase

- [ ] User-visible behavior mapped to requirement IDs and current prototype overrides.
- [ ] Backend authority, idempotency, permissions and failure path implemented.
- [ ] Graph identity/project/environment/revision/time boundaries tested.
- [ ] Citation and evidence source checks pass; no fallback to untracked live content.
- [ ] Loading/empty/partial/auth/rate-limit/retry/cancel states checked where applicable.
- [ ] Metrics have definition/source/window/denominator/freshness.
- [ ] UI keyboard/responsive and streaming reconnect checks pass.
- [ ] Status record contains actual tests/results, not claims of future verification.

## Documentation audit and decision register

Independent static prototype audit completed against final V12–V17 overrides. This is source inspection, not interactive browser testing. All active feature families map to PRD coverage matrix. Current decisions supersede autostart on assignment, merge-based recovery, impersonated approvals, broad database access and simulated production apply actions.

Implementation gates still requiring actual environment verification: pinned dependency compatibility, provider-specific permission/plan availability, licensed UI dependency availability if chosen, SMTP delivery service, sandbox capacity, hosting resource limits, retention/backup capacity. These are explicit tasks rather than hidden omissions. Product defaults are specified; no remaining question blocks beginning P1.

Final five-document review completed 12 October 2026 by an independent validation agent. Static source inspection covered final navigation and active V3–V17 functions/overrides, compared 28 coverage families with R-001–R-020 and T-001–T-024, and reviewed architecture/ontology boundaries. Corrections added provider-specific setup parity and capability gating, separate GitHub remediation credentials, safe provider URL handling, precise abstention denominators, and canonical bot entity names. Explicit Investigate, Neo4j-only retrieval, immutable revisions, approval invalidation and deployment-based recovery remain consistent. Limitations: no browser interaction, provider account/API validation, package compatibility execution, Cypher integration execution or working-application tests were performed in this document audit; these remain implementation gates. The earlier T-4 text match was within GPT-4o, not an undefined task reference.

## Amendments by user decision (2026-10-16)

Recorded in full in `docs/PLAN.md` section 1. Summary and task impact:
- Repos are `trace-api` (Python) and `trace-web` (Next.js).
- Settings is dropped for now (rules/approvers/members/roles/audit UI/credentials page/preferences/notifications). Backend keeps internal defaults only. Affects T-003 (no preferences endpoints), T-017 (no settings UI; default policies, no edit UI), T-023 (preferences, notification delivery, on-call removed; reviews/analytics/usage meters kept).
- Connectors limited to **Vercel (hosting) and GitHub (code)** plus generic webhook/upload. T-021 subtasks a, b, c, e, f, g, h, i, j are **CANCELLED**; d (Vercel) is promoted into P1/P2/P3 work and T-021 becomes hardening of the two adapters. The wizard has no Tickets step; tickets enter by manual form/API/webhook; the required change ticket is a manually linked external reference. The minimal Jira writer in T-018 is **CANCELLED**.
- Local toolchain is Python 3.11 (spec said 3.12): `requires-python >=3.11`.

## Agent progress log

| Date | Task | Status | Evidence / next action |
|---|---|---|---|
| 2026-10-12 | Specification | Reviewed | Five TRACE-only documents; implementation not started; next T-001 after document handoff |
| 2026-10-16 | Plan | Done | `docs/PLAN.md`: prototype reverse-engineered (landing, wizard, tickets, run state machine, bot, changes, observability), scope amendments, task map. |
| 2026-10-16 | T-002 (backend), T-003, T-005 (part) | IN_PROGRESS | Built: FastAPI app factory, validated config (field names only in errors), request-id middleware, safe error envelope, live/ready health, forward-only SQL migrator with checksums, migration 0001 (identity, projects, environments, services, connections, credential_versions, repo bindings, path mappings, log scope, drafts, jobs, outbox, audit append-only trigger, RLS on without policies), AES-256-GCM vault with AAD binding and key rotation, Supabase JWT verifier (RS256/ES256, iss/aud/exp), pure authz rules (roles are not a ladder; env-scoped grants), `/me`, invitation create/revoke/accept (single-use, email-bound, generic failure message), projects list/create/detail/patch (optimistic version, case-insensitive unique name, race-safe), environments/services PUT with stable IDs, project members PUT (>=1 admin, org-member and env checks), outbox scope projection, capability-specific readiness, typed OpenAPI (`docs/openapi.json`, `python -m app.openapi_export --check`). Checks: `pytest` 24 passed (unit + DB tests on an embedded throwaway Postgres via `pgserver`), `ruff check` clean. **Not done / limits**: no real Supabase JWKS verification run (FakeVerifier in tests only; verifier code unexercised against a live issuer); no CI workflow yet; project drafts/wizard, audit read endpoint, project repository bindings and path mappings, JWT-expiry tests; Docker/Neo4j/Supabase/OpenAI not provisioned. Next: T-006 (jobs/leases, GitHub + Vercel connections), then drafts (needs connections to allow draft-scoped rows). |
| 2026-10-16 | T-006 (part), T-003 (dev auth) | IN_PROGRESS | Added durable job queue (`app/jobs/queue.py`, `worker.py`): idempotent enqueue, SKIP LOCKED claim, leases/heartbeat, jittered retry honoring Retry-After, dead-letter, cooperative cancel, resource cancel, lease reaper. Added local-dev auth per user decision (plain Postgres, no Supabase): `AUTH_MODE=local` HS256 tokens via dev-only `POST /api/v1/dev/token` (not mounted unless local and non-production; config rejects local in production), `python -m app.cli bootstrap-org`/`migrate`. Checks: `pytest` 36 passed, `ruff` clean, openapi snapshot current. **Remaining for T-006**: GitHub (App installation token, verify stages, discovery, webhook signature/dedupe) and Vercel (token auth, projects/deployments discovery, drain secret) connectors, connection lifecycle endpoints (create/verify/rotate/disconnect, draft-scoped connections), provider capability descriptors endpoint, graph outbox applier (needs Neo4j). Docs checked 2026-10-16: Vercel `GET /v7/deployments` (git info only in loosely typed `meta`, so commit SHA must be treated as optional); GitHub `GET /installation/repositories` paginated (per_page<=100). Next: GitHub/Vercel connectors. |
| 2026-10-16 | T-003 (scope cut) | DONE for now | Per user: real auth deferred. `AUTH_MODE=off` = no login, fixed dev user (`X-Dev-User` header switches user), auto-joins a 'Dev Org'; rejected in production. Supabase/local-token code kept but not being extended. Checks: pytest 38 passed, ruff clean. Next: GitHub/Vercel connectors (T-006). |
| 2026-10-16 | T-005, T-006 | IN_PROGRESS | Added (migration 0002 draft-scoped connections): GitHub adapter (App installation tokens from READ app only, PAT trial mode, staged verify: installation, permission comparison incl. over-broad, repo listing), Vercel adapter (token, projects, v7 deployments; commit SHA optional; logs stage `skip`, never healthy), connection lifecycle (create/verify/rotate/disconnect; rotation verified before activation and failure keeps the working credential; disconnect wipes ciphertext and cancels queued jobs), vault-backed masked metadata only, provider capability catalog (only github/vercel/generic; unavailable modes carry a reason), discovery with bounded pages, log scope + repository bindings validated against live discovery, service/path mappings (one repo -> many services), wizard drafts (versioned, resume, skip, review, activate adopts connections), GitHub webhook (HMAC-SHA256, delivery dedupe, installation deleted/suspend revokes and cancels jobs), `/jobs/{id}` + cancel, `python -m app.worker_main`. Checks: `pytest` 52 passed (provider responses are labeled FIXTURES via httpx.MockTransport, so real-account behavior is NOT yet verified), `ruff` clean. **Remaining**: real GitHub App/Vercel token smoke test; GitHub App install-callback/state flow; Vercel log-drain endpoint (T-009); outbox->Neo4j applier and reconciler (needs Neo4j); webhook for push/rebuild-on-deploy; CI workflow. Next: T-007 needs Neo4j credentials, so continue with T-009 (ingestion + stored log search) and T-010 (tickets + explicit Investigate), which need only Postgres for now. |
| 2026-10-16 | Handoff / demo placeholders | DONE | Optional env placeholders for PulseLab/ShopFlow + `GET /api/v1/integrations/demo` (unset => configured:false), PulseLab origin added to CORS when set; CLAUDE.md now carries the overriding decisions, demo-app boundary, run commands and state so a cloud session has full context. pytest 55 passed, ruff clean. Next: T-009, T-010. |
| 2026-10-16 | T-010 (tickets part) | IN_PROGRESS | Migration 0003 + `/tickets`: manual form and API intake (idempotent on external_id), `POST /tickets/import/pulselab` (tolerant mapping of PulseLab export schema 1.0; unrecognised fields stay in the redacted payload; severity/environment/time never invented), dedupe with revisions, secret redaction before storage (`app/redact.py`), views need_project/open/resolved/all with per-view counts, server-side search/filter/sort, bounded pagination, empty-dataset vs no-match flag, assignment (needs investigate on the target project; defaults owner to project owner) that enqueues no job (tested), resolve/close separate from runs, optimistic versions, unassigned tickets visible org-wide for triage and assigned ones follow project grants. Investigate is reported `available:false` with a reason until the orchestrator exists. Checks: pytest 66 passed, ruff clean. **Limits**: PulseLab export field names are read from its plan, not from a real export - verify against a real file; ticket evidence (requests/backend_events) is stored in the payload with `evidence_sync=pending` and is not in Neo4j yet (T-009/Neo4j); Investigate/rerun command and active-run uniqueness not built (needs T-012). Next: project setup with real Vercel + GitHub on ShopFlow, then T-009 log ingestion, then T-011/T-012 once Neo4j and OpenAI keys exist. |
| 2026-10-17 | T-007 (ontology layer), T-011 (templates part) | IN_PROGRESS | Neo4j Aura reachable (5.27). Added `app/graph`: machine-readable ontology (38 labels, 56 relationships, vocabularies), deterministic project-scoped identities, validation (field names only in errors), checksum-guarded graph migrations (`migrations/graph/0001`), idempotent writer with immutability/cross-project/scope-agreement/evidence_ids checks, build lifecycle + manifests (published builds only), citation check, 9 fixed parameterized query templates + hop-limited callers. Gap audit against ontology.md in `docs/ONTOLOGY_GAPS.md`. Checks: `pytest` 103 passed, 15 skipped (Neo4j tests opt-in), `TRACE_TEST_NEO4J=1 pytest tests/test_graph_neo4j.py` 15 passed against the real instance (throwaway project ids, cleaned up), ruff clean. Limitations: no Graphify output normaliser, full-text/ranking/RetrievalManifest builder, outbox applier or O12 fixture suite yet. **Next:** T-007 Graphify adapter + normaliser, T-006 outbox applier, T-011 retrieval. |
| 2026-10-17 | T-006 (GitHub connect flow) | IN_PROGRESS | Per user: no TRACE login; the project setup page connects accounts instead. Added `POST /connect/github/start` (returns the github.com install URL with a signed, 15-minute `state` bound to org/user/draft-or-project) and public `GET /connect/github/callback` (verifies state, confirms the installation with GitHub using the app JWT, creates or reuses the draft/project GitHub connection, queues verify, 303-redirects to the web origin). Vercel stays a pasted token. Verified for real against api.github.com: app `traceai-app`, installation 172165419, private fork `chaitanya1705/shopflow-backend` visible, its `main` = `218241b0cd51` = the deployed ShopFlow revision (so exact-SHA indexing matches runtime). Real PulseLab run (Playwright, 10 PL-04 requests) exported and imported as TKT-1. Checks: pytest all passed incl. 3 new, ruff clean. **Needs from user/ops**: set the GitHub App's *Setup URL* to `<TRACE_PUBLIC_ORIGIN>/api/v1/connect/github/callback` and tick 'Redirect on update' (needs a public URL or tunnel; local `http://localhost:8000` works because the redirect happens in the user's own browser); Vercel token. Callback was tested with mocked GitHub only - the browser round trip is not yet exercised live. Next: T-009 log ingestion (Vercel drain). |
| 2026-10-17 | T-007 (indexer), T-011 (templates), graph read API | IN_PROGRESS | Added built-in extractor + indexer (`python -m app.graph.cli index <path> --project <id> --name <repo>`): indexed trace-web (109 files, 336 symbols, 587 CALLS + 360 IMPORTS) into Neo4j under a real project UUID; re-run creates no duplicate nodes, new build supersedes the old. Read API `GET /projects/{id}/graph/code` and `/graph/symbols/{symbol}` verified over HTTP (unknown project 404, missing symbol 404) and in trace-web `/graph` (200 nodes, symbol detail with pinned chunk hash). Closed the remaining ontology gaps (see docs/ONTOLOGY_GAPS.md). Checks: `pytest` default run passes; `TRACE_TEST_NEO4J=1 pytest tests/test_graph_*` 27 passed against Neo4j Aura. Limitations: built-in extractor is a stand-in for Graphify (TS via regex); indexer reads a local directory only; the failed-rebuild test only proves an empty folder is rejected before a build is created; membership checks for the graph routes were exercised for the not-found case only. **Next:** GitHub checkout at SHA + Graphify adapter (T-007), outbox applier (T-006). |
| 2026-10-17 | T-007 (GitHub indexing job), T-008 (code graph API/UI) | IN_PROGRESS | Migration 0004 `graph_index_runs`; job `graph.index` (resolve exact commit, download tarball read-only, safe extract, index, publish); `POST/GET /projects/{id}/graph/index`; latest published build per repository wins; dirty local checkouts get their own `+wip-` revision so a commit is never overwritten; communities group one level deeper when one folder holds most code. trace-web: Code tab "Generate graph" card with polled progress/errors/coverage gaps, Architecture tab (System map / Code graph / Database) with the live graph, blast radius and pinned source. Checks: default `pytest` 115 passed / 31 skipped; with `TRACE_TEST_NEO4J=1` 76 graph tests passed against Neo4j (including connection -> bound repo -> queued run -> worker -> published graph -> read API, with GitHub as a labeled fixture); browser check of the Architecture tab against the real graph. **Not verified:** a run against a real GitHub App installation (needs the App installed on an account and the worker running); the System map and Database views stay "not available yet". **Next:** run the real GitHub flow, Graphify adapter, outbox applier (T-006). |
| 2026-10-17 | Architecture views, deployments (T-009 part, T-022 interim) | IN_PROGRESS | Per user: customer app schema from a Postgres connection for now (graph later); System map from Postgres; Code graph left to the graph session. Added migrations 0005 (evidence_inbox, ingestion_batches, connection_secrets, deployments) and 0006 (postgres/database connections, schema_snapshots); `postgres_db` connector (read-only session, metadata only, defaults redacted; tested against a real Postgres, writes rejected); `schema.capture` and `deployments.sync` jobs; `GET /projects/{id}/schema`, `POST .../schema/refresh`, `GET .../system-map`, `POST .../deployments/sync`, `GET .../deployments`; web Architecture tab and DB connection card. Checks: pytest 121 passed, 31 skipped (graph tests need Neo4j), ruff clean; browser-verified: real trace-db read as 28 tables / 336 columns, real system map. **Spec deviation (user decision)**: PRD D-06 says TRACE never connects to customer databases; this POC reads schema metadata only. **Not done**: Vercel log-drain endpoint and stored log search (0005 tables and `app/domain/ingest/vercel_logs.py` exist, endpoints not), graph writes of evidence/deployments (outbox applier), ERD for >30 tables is a list, service<->database links unknown (shown as a gap). Next: log drain intake endpoint + Logs tab. |
| 2026-10-17 | T-009 (logs) | IN_PROGRESS | Deleted the other session's seeded fake ShopFront incident (demo/, .demo/, app/demo/) per user. Built: `POST /ingest/vercel/{connection}` (public, x-vercel-signature HMAC-SHA1 verified, JSON or NDJSON, only log-scope Vercel projects stored, dedupe on provider record id, batch counts), `POST /connections/{id}/drain/secret` (sealed, shown once) + `GET .../drain`, `POST /projects/{id}/logs/capture` (explicit bounded read-only live tail, 5-60 s) with `logs.capture` job, `GET /projects/{id}/logs` (TRACE filters: environment, level, text, request id, deployment, since/until; keyset paging; empty dataset vs no match), `GET /projects/{id}/logs/status` (per-resource freshness; no events = unknown, never healthy), web Logs tab. Checks: pytest 125 passed + 1 stale ticket test caused by the other session's uncommitted Investigate change (`test_assignment_enqueues_nothing_and_sets_owner` expects the old 'not available' reason; theirs to update), ruff clean; real run (scripts/smoke_logs.py) against the live ShopFlow backend: 3 events stored, structured fields and deployed revision preserved, freshness flipped unknown -> available. **Measured limitation**: Vercel's runtime-log live stream is lossy (3 of ~16 diagnostics events per request, no history), so complete logs require the drain; the drain intake is covered by fixture tests only, **not yet exercised with a real Vercel drain** (needs a public URL/tunnel and TRACE_PUBLIC_ORIGIN). Events are stored with graph_status=pending; the Neo4j writer for evidence is not built (other session owns graph code). Next: evidence -> graph applier, then investigator wiring. |

| 2026-10-17 | T-010 (explicit Investigate), T-011 (retrieval), T-012 (orchestrator, partial) | IN_PROGRESS | Built the GraphRAG investigation over the evidence graph. `app/graph/sync.py` + `POST /projects/{id}/graph/sync` project Postgres deployments and PulseLab ticket evidence (log events, request spans, run counts) into Neo4j: deployments carry the revision only when the commit is an indexed revision, an operation name locates code only when exactly one symbol carries it (`name_match`), the environment of evidence is its deployment's environment. `app/investigate/runner.py` is a 9-step run (scope, deployments, logs and observations, code by evidence and full-text, changes, blast radius, hypotheses, impact, report) using only fixed parameterized templates (new: log_events_in_window, evidence_by_operation, request_observations, code_located_in_window, changes_into_revision); each ToolExecution records a RetrievalManifest; the model (gpt-4o-mini, strict JSON schema) sees short references, every citation is checked against the run manifest, factual claims without a verified citation are dropped; no model key or provider failure falls back to graph rules and records a gap. Run, steps, hypotheses, assessments, findings, claims, impact and answers are written to the graph. `POST /tickets/{id}/investigate` (explicit, one active run per ticket), `GET /investigations`, `GET /projects/{id}/investigations/{run}`, `POST .../ask` (answers cite only the run's retrieved evidence), `GET /projects/{id}/evidence`. Checks: pytest tests/test_investigation.py 7 passed against real Neo4j (labeled fixtures); full suite 153 passed before the ticket assertion update, tickets + graph api re-run 10 passed; ruff clean on new code. Real run on ShopFlow ticket TKT-5 (100 real PulseLab requests, no failures): finding `no_failure_observed`, all citations verified. **Limits**: only runs the ShopFlow evidence that exists: TKT-1 (receipt mismatch, the real incident) is assigned to another project so it has not been investigated; only 218241b is an indexed revision (6 of 8 deployments have no indexed code); no commit history is ingested (Change nodes need GitHub compare), so the changes step reports none; runner runs on an API thread (no durable worker/SSE resume yet), human questions (T-013) not built, no outbox applier (sync is on demand). Next: assign TKT-1 to ShopFlow and investigate it; index the other deployed revisions; ingest commit history. |
| 2026-10-17 | T-009 (app log push) | IN_PROGRESS | User is on Vercel Hobby (drains need Pro; runtime logs kept 1 h; no historical API; live tail is lossy) and TRACE must read ShopFlow only (PulseLab is just their traffic generator), so an earlier attempt to read logs from PulseLab's export was REMOVED. Built option 2: per-project **app log source**: `GET/POST /projects/{id}/log-source` (creates telemetry connection + HMAC secret, shown once, rotation invalidates old), public `POST /ingest/events/{connection}` (x-trace-signature sha256 HMAC, JSON/NDJSON/array, accepts ShopFlow's native diagnostics field names, project-wide dedupe on event_id, unknown time stays unknown, explicit default environment, secret wiped on disconnect), migrations 0007 (project-level dedupe) and 0008 (telemetry kind), web card with setup, endpoint, secret-once, forwarder snippet; `docs/SHOPFLOW_LOG_FORWARDER.md` for the ShopFlow owner. Checks: pytest 129 passed / 38 skipped (graph tests need Neo4j), ruff clean; real run: set up through the UI, then real checkouts against the live ShopFlow, events taken from its responses and pushed signed through ngrok: 47 accepted incl. 3 real error events from the failing request, searchable by level/request id. **Limits**: ShopFlow itself does not forward yet (needs the owner to add forward.py + 3 lines + 2 env vars); until then only my stand-in forwarder (same code path/signature) has sent events; events are stored with graph_status=pending (graph writer for evidence not built here); **deployed ShopFlow revision is now fdedfd9, newer than the fork's 218241b - sync the fork before code-to-runtime mapping**. Next: sync fork, evidence -> graph applier. |

