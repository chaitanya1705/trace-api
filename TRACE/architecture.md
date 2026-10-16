# TRACE architecture

Version 1.0 · 12 October 2026 · Implementation baseline. Normative behavior is in PRD.md; graph semantics in ontology.md. Provider-specific credentials, pricing and SDK support must be reverified at implementation; this specification does not assert every provider feature is available on a free plan.

## A1. Decisions and component boundaries

| Layer | Choice | Responsibility |
|---|---|---|
| Web | Next.js, TypeScript, Tailwind, shadcn/ui, TanStack Query | Authenticated routes, forms, cached API reads, responsive prototype experience |
| Graph UI | D3 managed through React wrappers | Bounded node/edge visualization and accessible parallel table |
| API | Python 3.12, FastAPI, Pydantic v2 | Auth/scope, typed commands, queries, SSE |
| Worker | Python process sharing backend code | Durable indexing, ingestion, investigation, external-write and verification jobs |
| Model | OpenAI API, gpt-4o-mini | Structured hypothesis/answer/patch drafting within fixed tools and budgets |
| Extraction | Pinned graphifyy behind GraphifyAdapter | Code-only extraction into validated intermediary JSON |
| Evidence | Neo4j | All investigator-readable evidence/source/relationships and assessment provenance |
| Control | Supabase Postgres | Membership, project config, connectors, tickets, queue, statuses, approvals, audit, chat/event delivery |
| Auth | Supabase Auth | Identity; backend adds authorization |
| Artifacts | Private Supabase Storage | Raw permitted imports, extraction diagnostics, patches/reports with scoped expiry |
| Deploy | Vercel web; Railway supervised API+worker | Initial one-backend-deployment topology |

Pin tested dependency versions in lockfiles at T-001; do not guess current Next.js/Neo4j/SDK major versions from this planning document. Neo4j must support the required uniqueness, full-text and parameterized query capabilities; baseline avoids mandatory APOC/GDS/vector services. Community metadata comes from extraction. React Bits Pro can be used only when the existing licensed dependency is available; no requirement to install it to reproduce the supplied HTML.

No Redis/Kafka/service-per-skill architecture initially. Long-lived work never runs in Vercel browser/frontend request handlers. Supervisor runs API and worker as separate processes in one backend container with independent limits; failure of a child is detected and restarted. Both depend on durable DB state. Splitting worker into a separate deployment later preserves contracts.

## A2. Evidence boundary and synchronization

Neo4j is the sole retrieval backend for the model. Source adapters may fetch externally, but normalize/store before model tools return their evidence. Supabase application state can be read by orchestration for authorization/job progress; never substitute application JSON blobs for graph evidence silently.

Postgres command transaction writes domain state plus an outbox row. Worker claims outbox/job, applies idempotent Neo4j writes, marks graph receipt/checkpoint, then completes job. Crash after graph commit is safe because node/edge keys and operation IDs are deterministic. Neo4j and Postgres do not share a transaction. A reconciler finds incomplete outbox rows and replays them. UI shows evidence_sync_pending until required ticket/source writes are readable. Model-produced assessments pass schema/citation validation, write Neo4j first, then update control projection via retryable receipt. A delayed projection is labeled stale rather than inventing completion.

## A3. Repository layout

Two repositories; use these logical paths (create matching modules, not empty scaffolding alone):

```text
trace-frontend/
  app/(public)/page.tsx
  app/(auth)/login/  signup/  reset-password/
  app/(workspace)/home/  tickets/  investigate/  changes/
  app/(workspace)/observability/  projects/  settings/
  components/shell/  graph/  evidence/  diff/  forms/
  features/projects/  investigations/  tickets/  changes/  chat/
  lib/api/  auth/  scope/  events/  generated/
  tests/e2e/  tests/components/
  docs/TRACE/  package.json  lockfile  .env.example

trace-backend/
  app/main.py
  app/api/v1/  auth/  config/  contracts/
  app/domain/projects/  tickets/  investigations/  changes/  verification/
  app/connectors/base.py  github/  generic/  providers/
  app/graphify/adapter.py  normalization.py
  app/graph/schema/  queries/  repositories/  migrations/
  app/orchestration/state_machine.py  policy.py  budgets.py
  app/skills/scope/  retrieve/  hypotheses/  impact/  resolution/  verify/
  app/jobs/queue.py  worker.py  leases.py  handlers/
  app/storage/postgres/  neo4j/  artifacts/  secrets/
  app/events/  audit/  metering/  observability/
  migrations/postgres/  tests/unit/  integration/  contracts/  evaluation/
  docs/TRACE/  pyproject.toml  uv.lock  Dockerfile  supervisor.conf  .env.example
```

UI state ownership: URL for filters/selection; TanStack Query for server data; component state for transient overlays; personal preferences in Postgres. Never store credentials or authorization claims in custom localStorage. Persist auth using supported Supabase session handling and protected cookie/session patterns; API validates bearer identity.

## A4. Postgres control schema

All organization-owned rows have `organization_id`; project-owned rows also have `project_id`. UUID primary keys, UTC timestamptz, created/updated timestamps, optimistic `version` for mutable resources. Enable RLS where exposed through Supabase; backend authorization remains mandatory even with service credentials.

| Table | Principal fields and constraints |
|---|---|
| organizations, memberships | user_id, role, status; unique organization/user; revoked_at |
| projects, project_memberships | name normalized unique per org; project role/environment grants |
| environments, services, repository_bindings | stable IDs; scope mappings; no name-only join |
| connections, credential_versions | provider/auth method/scopes/status; encrypted secret ref/version; no plaintext |
| ingestion_cursors, ingestion_batches | connection/resource cursor; counts/error; advance only after acknowledged writes |
| tickets, ticket_revisions | source/external ID unique per connection; reported fields and assignment version |
| investigation_runs | ticket_id, status, cutoff, budget snapshot, checkpoint, result_graph_id; partial unique active ticket run |
| human_requests, submissions | run/step/type/schema; single accepted response per pending request; actor |
| jobs, outbox | kind, payload schema version, idempotency key, lease owner/expiry, attempt, next_attempt, result ref |
| run_events | run_id, monotonically increasing seq, type, redacted payload, graph refs; unique run/seq |
| conversations, messages | project/run, parent/version IDs, status, citation IDs; final evidence answer mirrored to graph |
| resolution_plans, patch_versions | graph refs, immutable patch hash/base manifest/test state |
| policies, approvals | policy version, scoped eligible actor, patch hash, decision/revocation; unique actor/version |
| external_operations | operation key, desired payload hash, provider IDs, uncertain/reconciled status |
| verification_runs, checks | deployment/window/criteria/coverage/result graph refs |
| audit_events, usage_ledger | immutable actor/action/result; source-specific bytes and model usage independently |
| reviews, notifications, user_preferences | authenticated review labels, dedupe delivery key/read state, personal settings |

Large graph/source content is not duplicated into Postgres as an alternative retrieval index. Private artifacts require graph-ingestion before use as evidence. Export jobs read authorized retained evidence and create expiring downloads. Deletion first revokes active scope and stops jobs, then performs policy-controlled cleanup with audit; preserve legally/per-policy retained citations without granting former users access.

## A5. API conventions and endpoint inventory

Base `/api/v1`. JSON envelopes: `{data, meta:{request_id, as_of, next_cursor?, coverage?}}`; errors `{error:{code,message,retryable,details},request_id}`. Never return stack traces/secrets. Typical codes 401 unauthenticated, 403 forbidden, 404 absent-or-inaccessible, 409 state/version conflict, 422 validation, 429 budget/rate limit, 503 dependency unavailable. Command endpoints accept Idempotency-Key and optimistic version where relevant; async command returns 202 `{job_id, resource_id, status, events_url}`. GET never changes domain state.

| Endpoint | Purpose / authorization / execution |
|---|---|
| GET /me; GET/PATCH /me/preferences | own profile/preferences; synchronous |
| GET/POST /projects; GET/PATCH /projects/{p} | scoped list; Admin create/edit; outbox scope projection |
| GET/PUT /projects/{p}/members | project Admin; audit and authorization-version bump |
| GET/POST /projects/{p}/connections | Admin; encrypted write; connection verification job |
| POST /connections/{c}/verify, /rotate, /disconnect | Admin; lifecycle/version/audit; stop stale jobs |
| GET /connections/{c}/resources, /repositories | scoped discovery; paginated; credentials remain server-side |
| POST /projects/{p}/builds | revision selection; Admin/Investigator with indexing grant; graph_build job |
| GET /projects/{p}/builds; GET /builds/{b} | status/history/coverage |
| POST /jobs/{j}/cancel | actor with original operation scope; cancellation requested |
| GET /projects/{p}/graph | validated filters/root/depth/revision manifest; graph_repository |
| GET /projects/{p}/source | repository/revision/path/line bound; exact source chunks |
| POST /ingest/events; POST /webhooks/{provider} | scoped integration credentials/signatures; durable receipt |
| GET/POST /tickets; PATCH /tickets/{t} | authorized intake/assignment; never starts run |
| POST /tickets/{t}/investigations | explicit start/rerun; Investigator; returns existing active run on duplicate |
| GET /investigations; GET /investigations/{r} | scoped runs and latest projection |
| GET /investigations/{r}/events | SSE with cursor; reauthorize session and project |
| POST /investigations/{r}/cancel | Investigator; retains history |
| POST /human-requests/{h}/responses | eligible Investigator; atomic pending→answered |
| GET /investigations/{r}/context?step_id= | historical graph assessment/evidence snapshot |
| GET /projects/{p}/evidence; POST /projects/{p}/source-fetches | graph search vs explicit connector ingestion |
| GET/POST /conversations; POST /conversations/{c}/messages | context authorized; follow-up job |
| POST /messages/{m}/regenerate; POST /generations/{g}/cancel | versioned generation |
| GET /changes; GET/POST /plans/{p}/patches | scoped change and immutable patch versions |
| POST /patches/{p}/validate | isolated test job when configured |
| POST /patches/{p}/approvals; POST /approvals/{a}/revoke | eligible actor only; policy check |
| POST /patches/{p}/draft-pr; POST /plans/{p}/change-ticket | separately authorized external write jobs |
| GET /verifications; GET /verifications/{v} | deployment-aware verification records |
| POST /verifications/{v}/reopen | explicit new investigation |
| GET/PUT /projects/{p}/policies/{env} | scoped Admin; versioned rules |
| GET /audit; POST /audit/exports; GET /usage | role-scoped, redacted, pagination |
| POST /findings/{f}/reviews; GET /analytics | authorized reviewed metrics |
| GET /notifications; POST /notifications/read | per-user visibility/read state |

Additional required endpoint families (same auth, envelope and idempotency conventions):

| Endpoint | Contract |
|---|---|
| GET/POST /organizations/{o}/invitations; GET/PATCH /organizations/{o}/members/{u} | Owner/Admin; membership lifecycle and invitation status; never public self-grant |
| GET/POST /project-drafts; GET/PATCH /project-drafts/{d}; POST /project-drafts/{d}/activate | creator/Admin; versioned wizard data including completed/skipped stages; activate returns project and indexing job IDs |
| GET/PUT /projects/{p}/environments, /services, /repository-bindings, /path-mappings | scoped Admin; versioned arrays of stable IDs/mappings; invalidates relevant readiness, not historical manifests |
| GET /search; GET /home | authorized scope/q/cursor; typed result links; dashboard counters, source timestamps and actionable needs-you items |
| GET /jobs/{j} | authorized original scope; state/stage/progress/warnings/cancel status, no secrets |
| GET /evidence/{e}; GET /citations/{c} | scoped immutable locator/hash/content/coverage; missing retained source is explicit error |
| GET /conversations/{c}/messages; GET /generations/{g}/events | cursor-paginated versioned history and authorized resumable SSE; stop endpoint defined above |
| GET /plans/{p}; GET /patches/{p}; GET /patches/{p}/diff | scoped rationale, immutable base/hash, policy/approval/test projections and file/line diff |
| POST /projects/{p}/exports; GET /exports/{x} | Investigator/Admin with export grant; selected authorized graph/evidence/report snapshot, redaction, job state and expiring artifact URL |
| GET /credentials; GET/PUT /me/notification-settings; POST /notifications/read-all | credential metadata scoped by Admin role; own preferences/read flags |

Draft resources reference connection IDs, never embed secrets. Mapping updates are audited and effective from a recorded version/time; old runs preserve original manifests. Export generation reads the graph under requestor scope and rechecks access at download authorization. No raw cross-project dump endpoint.

Generate OpenAPI and frontend types in CI; no independently handwritten divergent contracts. Provider webhook responses acknowledge durable receipt, not completed analysis. OAuth callbacks validate state and bind to initiating admin/project; sanitize redirects.

## A6. Typed tool and model contracts

Pydantic models are the authority; JSON is serialization. Representative request:

```json
{"tool":"expand_dependencies","arguments":{"project_id":"p","environment_id":"e","root_entity_ids":["sym1"],"revision_manifest_id":"m","direction":"incoming","relations":["CALLS"],"max_hops":2,"limit":100},"run_id":"r","step_id":"s"}
```

Backend derives allowed project/environment from run context and rejects mismatch; user/model arguments never widen it. Tool result: `{execution_id,status,entities,evidence,paths,coverage:{gaps,truncated,as_of},usage}`. Every evidence item has stable ID, content hash, source, event/ingest times and locator. Unknown source values stay null with reason.

Tools: search_evidence, get_source_excerpt, get_deployment_revision, get_change_diff, expand_dependencies, get_evidence_path, compare_time_windows, request_source_refresh, request_human_input. All except refresh/human request read Neo4j. Refresh returns job status until ingestion completes. Model cannot run Cypher, shell, arbitrary HTTP or repository hooks.

Model output schemas: HypothesisProposal (statement, alternatives, next test), Assessment (status, supported_by, contradicted_by, gaps, summary), Finding (claims each with citation IDs, limitations, impact refs), ResolutionProposal (steps, preconditions, patch draft if relevant, tests, rollback advice), Answer (segments/citations/followups). Validate cited IDs belong to retrieval manifest and scope; validate source line range. Invalid output gets at most one repair attempt within budget, then records structured failure/partial finding. Do not manufacture evidence to satisfy schema.

Only authorized retrieved excerpts go to the model; redact secrets and cap content. Repo/ticket/log instructions are untrusted data. Store high-level tool purposes/results, not private chain-of-thought. Optional docs semantic ingestion is disabled by default and has a separate consent and cost boundary.

## A7. Orchestrator and job semantics

State-machine steps: scope → retrieve → propose hypotheses → test → impact → resolution → report. Steps can revisit retrieval/testing when justified. Parent owns checkpoint, dispatch and budget. Skills/subskills are Python modules sharing typed state; no independent unrestricted agents.

Queue claim uses Postgres row locking with SKIP LOCKED; lease 60s, heartbeat 15s initially. Never hold a DB transaction open over provider/model calls. Recover expired lease and reconcile previous side effects before retry. Max three transient attempts with exponential jitter honoring Retry-After; validation/auth failures pause/fail without endless retries. One running investigation and one indexing job initially, with a shared CPU/memory semaphore so indexing cannot exhaust API resources. Queue priority favors human resume/cancel and light ingestion over bulk rebuild.

Each step stores inputs/evidence manifest, policy/budget versions, output references and event. Deterministic tools can retry with execution keys. External writes require `external_operations` reconciliation. Cancellation checks between calls; kill extraction subprocess group when needed; completed evidence remains. Active run uniqueness includes queued/running/waiting states. Repeated wait/input has a time-limited configurable expiry, visibly expired rather than silently completed.

Initial configurable budgets: 20 model calls, 60 tool calls, 10 minutes active compute per run (human wait excluded), 2→4 graph hops, 100 nodes/200 edges per expansion, 20k evidence characters per model call; actual token limit additionally enforced. These are starting caps requiring evaluation, not performance promises. Agent returns partial/insufficient result with exact budget reason when exhausted.

## A8. Streaming and frontend consistency

SSE event types: run.queued, step.started, tool.completed, evidence.added, assessment.updated, human_input.required, finding.updated, run.completed, run.failed, run.cancelled. Sequence IDs are durable; authorize on connection and periodically on long streams. Last-Event-ID resumes; expired cursor returns a snapshot plus new cursor. Heartbeats do not advance domain sequence. Final message/evidence objects persisted before completion event.

No secret bearer tokens in URLs. Use fetch streaming with Authorization header or secure same-origin session proxy. Poll run snapshot every few seconds if streaming unavailable; exponential backoff on outage. Token deltas may be transient; finalized text persists, interrupted generations marked partial. Choosing historical step supplies cutoff/assessment version, not latest global state.

## A9. Graphify integration and publication

Use subprocess argument arrays, fixed executable and absolute allowed checkout path. Pin tested Graphify package/lockfile and record version/output schema. Extract code only, preserve JSON intermediary. Do not install its coding-assistant hooks in customer repositories. Adapter capability spike must verify exact release CLI flags and output keys with Python/TypeScript fixtures; source review alone does not establish compatibility.

Fetch allowlisted GitHub repositories at exact SHA with bounded size, safe paths, no git hooks/LFS execution/submodule recursion by default. Do not run package install/build/tests during extraction. Temporary directory per job; least privilege; resource timeout and cleanup. Initial repo cap 100MB fetched source/10k candidate files/2MB single parsed file, configurable and visible as skipped coverage. Network needed for fetch is separate from extraction; extraction subprocess gets no provider credentials/model keys and restricted filesystem/network where host permits.

Validate output nodes/edges, preserve parallel relations/direction and extractor provenance, handle unknown labels explicitly. Normalize to ontology with project/repository/revision identity. Batch parameterized UNWIND writes (start 500 records/batch). Reject dangling edges, compare counts, then publish completed GraphBuild and atomic revision-manifest pointer. Consumers require published build. A failed build leaves prior pointer intact. Cross-repository linking is a distinct evidence-backed pass, not label matching. Rebuild-on-deploy only after actual revision is known; an unknown SHA queues resolution or marks gap.

## A10. Connector implementation contracts

Base adapter methods: validate_connection, discover_resources, fetch_page(cursor,scope,window), normalize, health; optional fetch_repository, list_deployments, create_change_ticket, create_draft_pr with separate capability permissions. Record provider API version/scopes. Verification must prove requested scope with safe reads and report missing privileges.

Pipeline: validate webhook/auth → durable receipt → redact → normalize → dedupe → entity resolution → graph batch → cursor commit. Out-of-order events use event time; allow configured late-arrival window; never discard late records silently. Emit coverage gap for rejected/oversize/truncated input. Exactly-once logical evidence via deterministic keys; transport is at-least-once.

P1 GitHub baseline: scoped GitHub App installation for repository reads; local-development token path optional and explicitly labeled. App write permission added only for P3 authorized operations. Validate webhook signature and delivery ID. P2 generic structured JSON runtime and deployment events, explicit revision mappings, and manual/API tickets are real initial paths. Deployment mappings are required for P2 code attribution; P3 adds recovery orchestration and richer provider deployment tracking. P3 implements a minimal Jira change-ticket writer (or a verified manually linked ticket) to satisfy required-ticket policies; broad Jira intake/discovery follows in P4. P4 adapters Azure/AWS/GCP/Vercel/Railway, Azure DevOps, Jira/ServiceNow/Azure Boards and Supabase must verify current auth, pagination, log availability and rate limits from official docs before enabled. Do not hardcode prototype token regexes/plan claims as truth. Resource discovery with no logs is a supported state, not connected-and-healthy.

Logs console: stored mode reads Neo4j via filters. Fetch-source mode sends allowlisted read query to connector with timeout/row/scan-cost bounds where available. Result ingested before displayed as evidence; show partial/failed ingestion. No arbitrary SQL/customer row queries. Provider query syntax never becomes an executable graph query.

## A11. Security and credential lifecycle

Supabase JWT signature/issuer/audience/expiry verified server-side; check active membership and project grants per command. Roles are scoped; service credential bypass of RLS must not bypass API policy. Graph queries require project prefix filters and bound endpoints. No client-supplied arbitrary Neo4j IDs are trusted without scope lookup.

Initial secrets: authenticated encryption using a maintained cryptography library with random nonce; master key and key version in backend secret environment, encrypted values in Postgres. Never store key beside ciphertext. Document encrypted backups and manual versioned re-encryption procedure. P4 can adopt managed KMS; do not claim KMS/short-lived identities before implemented. Rotation validates new credential then atomically activates version; queued jobs recheck version/status. Revocation/disconnect blocks new fetch/writes immediately; cached credentials expire promptly. UI gets masked metadata only.

Treat provider URLs/repo URLs as allowlisted origins; block SSRF to private/metadata addresses for generic source configuration; constrain redirect targets. Redact token-like values before graph/model/log writes. Enforce payload limits, file extension/content validation and escaping in code/log viewers. Raw HTML from artifacts never executes in workspace. Signed artifact URLs scoped and short lived. Audit all permission changes and external write attempts/results.

## A12. Remediation and verification

Patch generation uses retrieved exact source plus base manifest; validate syntax/diff paths; no secret files or unauthorized repositories. Freeze PatchVersion with hash and test report refs. Dedicated P3 sandbox runner is separate from API/ingestion secrets, network-denied by default, ephemeral, bounded CPU/time/disk. If unavailable, mark untested and block policy requiring executed tests; do not run customer tests in privileged backend container.

Policy engine checks eligible actor, author exclusion, count, CODEOWNERS, branch, linked change ticket, freeze timezone (IANA), override role/reason, patch and policy versions. Write dispatch repeats checks after queue delay. Approvals invalidated on patch edit; credential rotation doesn't grant write scope. Draft PR operation key includes repo/base/patch hash. On timeout, query provider for matching branch/PR marker before retry. Never merge, deploy or rollback; those remain external/manual actions tracked as evidence.

Verification waits for relevant deployment + actual exposure evidence. Baseline/post windows use same query definition, service/environment and known sampling. Criteria are versioned per plan: minimum window and request count, max error rate, latency bound when available. At least 10-minute observation and 100 relevant requests are illustrative default settings, must be visible/configurable and recorded, not hidden universal proof. Too few requests → inconclusive. Log-only connector cannot calculate latency without suitable events. Regression offers explicit reopen. Record deployment ambiguity/confounding changes as limitation.

## A13. Operational configuration and recovery

Required backend variables (names are app conventions): SUPABASE_URL, SUPABASE_JWKS_URL, SUPABASE_SERVICE_ROLE_KEY, DATABASE_URL, NEO4J_URI, NEO4J_DATABASE, NEO4J_USERNAME, NEO4J_PASSWORD, OPENAI_API_KEY, TRACE_MODEL=gpt-4o-mini, TRACE_SECRET_KEY, TRACE_SECRET_KEY_VERSION, TRACE_PUBLIC_ORIGIN, TRACE_ALLOWED_ORIGINS. Connector keys only when used; GitHub App ID/private key/webhook secret stored server-side. Read indexing and remediation use separately provisioned app installations and credentials; a write job cannot borrow read-plane credentials, and an indexing job cannot receive remediation credentials. Provider adapters expose versioned capability descriptors (supported auth modes, read/write operations, discovered scope, plan limitations, verification timestamp); the frontend gates unsupported R-016 setup modes using these descriptors. External identity provider URLs and customer-entered enterprise/instance URLs require HTTPS, allowlisted destinations or explicitly provisioned private routing, redirect validation and SSRF protection; no arbitrary backend URL fetching. Certificate/private-key uploads use the encrypted credential store, never evidence artifacts. Frontend receives only NEXT_PUBLIC_SUPABASE_URL, publishable auth key and public API origin; never service-role/model/graph keys.

Queue limits, model budget, provider scan budgets, artifact retention and evidence retention have named validated config. Log startup configuration names/capability status without values. `/health/live` process check; `/health/ready` dependency readiness with redacted detail; worker heartbeat visible to admin. Structured logs carry request/job/run/project IDs and duration, never prompt/source bodies by default.

Track ingestion lag, oldest queued job, lease recovery, extraction coverage, query p95, SSE reconnects, model usage, failed citations, external uncertain operations and verification coverage. Define SLOs after baseline load tests; initial engineering target API p95 under 1s for cached/list reads at one active user, not a contractual guarantee. Long work always async.

Backup Postgres, Neo4j and protected artifacts consistently enough to restore receipts; run restore drill and reconcile cross-store pointers. Define retention with configurable defaults: raw imports 7 days, uncited logs 14 days, completed investigation evidence 90 days; pinned citations retain at least investigation lifetime. Deletion job computes references before removing content. Explicit organization deletion overrides normal pinning through audited policy workflow. Do not promise durability beyond provisioned plans.

## A14. Validation strategy and rollout gates

Unit: scope/policy, identities, normalization, metric formulas, state transitions. Integration: actual test Postgres/Neo4j, graph idempotency/publication, outbox crash points, lease recovery, source version isolation. Contract: adapter fixtures/real authorized sandbox provider smoke, OpenAPI generation, Graphify version fixture. Evaluation: known incident corpus with independent expected evidence; competing explanations, missing evidence, misleading ticket and prompt injection; measure citation correctness, causal accuracy via review, abstention, cost and retrieval recall.

Browser: R-020 widths, keyboard/reduced motion, actual API state not mock timers, stream interruption, expired credentials and manual start. P3 sandbox/PR writes validated only in dedicated test repository with explicit operator configuration. All metrics distinguish tests from live usage.

Sources inspected during planning (12 October 2026; moving branches are not dependency pins): https://github.com/Graphify-Labs/graphify ; https://github.com/Graphify-Labs/graphify/blob/v8/graphify/affected.py ; https://github.com/Graphify-Labs/graphify/blob/v8/graphify/export.py ; https://github.com/Graphify-Labs/graphify/blob/v8/pyproject.toml ; https://graphify.com/integrations/claude-code . Source review supports adapter approach; performance/token-savings claims are not adopted as TRACE guarantees.
