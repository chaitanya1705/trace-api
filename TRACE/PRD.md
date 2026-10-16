# TRACE product requirements

Version 1.0 · 12 October 2026 · Scope: TRACE only. Normative words MUST/SHOULD define requirements. Phase assignment means planned delivery, not removal. Read `Context.md` for precedence.

## 1. Product outcome and users

TRACE enables an investigator to explain a production symptom with inspectable evidence, distinguish possible from observed impact, and prepare a controlled resolution. Administrators connect/scoped sources; investigators analyze; approvers authorize eligible changes; viewers inspect results. Owners manage organization access. A person can hold multiple scoped grants, subject to separation-of-duty policy.

Success is demonstrated through reproducible evidence retrieval, correct source/revision citations, explicit uncertainty, consistent restart/retry behavior, and complete user workflows. Marketing numbers in the prototype are not acceptance targets.

## 2. Scope, phases and prototype authority

The reference is the user-supplied `index.html` (4,172 logical lines in the supplied file; line anchors are approximate). Use final NAV7/router and V12–V17 overrides. Earlier standalone Overview/Knowledge/Organization/Logs/Topology pages are superseded as top-level routes; useful current capabilities are located inside final pages. Source anchors below locate behavior; do not copy mock backend logic.

| Phase | Required working outcome |
|---|---|
| P1 | Authenticated responsive workspace; project configuration; read-only GitHub indexing; published source/code graph; isolation; credentials and operational basics |
| P2 | Ticket intake; explicit Investigate; real normalized telemetry; full run/human-input/bot/evidence experience |
| P3 | Versioned patches; governance; approved external writes; deployment-aware verification and regression reopening |
| P4 | Remaining connector providers; broader schema support; reviewed analytics; notifications and advanced settings |

## 3. Shared behavior contract

All protected routes enforce project permissions on the backend. Lists use server-side search/filter/sort and bounded pagination, preserve URL filters, and distinguish empty dataset from no matches. Responses carry data timestamp and coverage when relevant. Timestamps are stored UTC and rendered in the user's selected timezone; durations are timezone-independent.

Every asynchronous surface supports loading, empty, partial, success, permission-denied, expired-auth, rate-limited, timeout, cancelled and failed states as applicable. Show an actionable error with a correlation ID without secrets. Retry repeats an idempotent command or creates an explicit new run. Refresh does not enqueue investigations. Disable double submission until the command result is known; reconcile uncertain results server-side.

Global project/environment selectors constrain every scoped page, query, bot context and export. An All-projects view returns only authorized projects. Switching scope must not leave data or conversations from an inaccessible project visible. Cross-project search results display their scope.

WCAG-oriented keyboard operation: named controls, visible focus, focus trapping/restoration for dialogs, Escape closes non-destructive overlays, graph alternatives as lists/tables, status text beyond color, reduced-motion support. Graph canvas remains horizontally pannable; page itself must not overflow.

## 4. Requirement catalog

### R-001 — Landing and authentication [P1]

Reproduce prototype landing structure, feature previews and light/dark appearance with truthful labels; sample images/data must be labeled. Login/signup/sign-out use Supabase Auth, email/password and verified-email handling; password reset and expired session paths included. Invitation-only organization membership is baseline: signing up does not grant project access. Unauthenticated deep links return to their intended authorized page after login. Preserve prototype social/SSO affordances only as disabled until a supported provider is implemented in P4. Never simulate authentication with localStorage.

Acceptance: unauthorized deep link shows login; successful login returns safely; revoked member cannot reload project data; recovery flow does not disclose whether another user's account exists.

### R-002 — App shell and scope [P1]

Collapsible rail, mobile drawer, breadcrumbs, account/sign-out, theme switch, project/environment selectors, global command/search, toolbar Settings and contextual bot entry. Persist personal rail/theme preferences. Search covers authorized tickets/projects/runs/changes; each result opens a stable route. Keyboard shortcut opens the command palette; users can navigate without a mouse. Nav counts derive from real records and display loading/unknown instead of fictional numbers.

Acceptance: selector changes API scope, not merely title; browser back restores filters; drawer closes after navigation; selected project is visible at narrow widths.

### R-003 — Home and sidebar [P1 shell; P2 investigation; P3 approvals/recovery; P4 feed polish]

Home includes greeting/date, open tickets, waiting requests, pending approvals, recovery counts, Needs-you queue, active run progress, project coverage/health and recent changes. Needs-you links route to the exact answer/manual check/approval/assignment/regression action. Show meaningful empty states.

Sidebar Since-you-were-away supports read/unread per user and mark-all-read. Daily tool-call and provider-scanned-byte meters use separately metered values; unavailable scanned volume is Unknown. Store UTC daily budget periods and display reset in selected timezone. Do not infer bytes from model tokens. On-call display, if present, uses configured membership/rotation metadata; absent integration shows Not configured.

Acceptance: completing a request removes it from Needs-you after refresh; marking feed read is per-user; counters include only authorized scope.

### R-004 — Tickets and manual start [P2]

Intake supports manual form and authenticated API/webhook, then provider intake in P4. Dedupe on connector/external ID and record revisions. Fields: title, description, reported severity, source, source timestamps, project/environment/service candidates, owner and attachments metadata. Validate/redact attachment content before evidence ingestion; initial attachments support text/log files with size caps.

Views: Need a project/Open/Resolved/All; search ID/title/service; filter severity/environment/project; sort owner/opened/status/finding; refresh. Assigning project updates assignment only. Separate explicit Investigate action establishes scope, waits for ticket evidence ingestion, then creates a run. Missing project/environment/time scope prompts a small form. Missing optional telemetry is a coverage gap, not necessarily a blocker. Duplicate clicks return the active run. Finished run has Open result and Run again actions. Closing or resolving a ticket requires a user action and does not erase runs. No automatic restart on connector refresh.

Acceptance: assignment results in zero new jobs; Investigate produces one active run under concurrent clicks; reopening a result does not rerun it.

### R-005 — Investigation lists and lifecycle [P2]

Investigate offers authorized project grouping, per-project active/all lists, title, environment, status, finding, step count, timestamps and owner. Run states: queued/running/waiting_for_input/completed/failed/cancelled. Budget exhaustion completes with an explicit incomplete reason unless user input is necessary. Failed is execution failure; insufficient evidence is a valid completed finding.

Actions: open, cancel with confirmation, resume eligible human-input pause, explicit rerun. Cancellation stops future tool dispatch, preserves completed evidence and acknowledges when in-flight work has stopped. Rerun creates a new run linked to the prior run. Historical replay is read-only event playback; it cannot call tools or alter evidence.

Acceptance: worker restart resumes from durable checkpoint without duplicate writes; failed runs show retry route; cancel is reflected across browser sessions.

### R-006 — Execution trace and contextual panels [P2]

Main workspace shows progressive action cards with tool name, purpose, scope, time window, duration, result count/cap, provider query or graph-query summary, scanned volume if supplied, result summary/table, evidence links and warnings. Expand/collapse individual cards. Open in Logs preserves query scope; View code opens exact revision. Expose safe high-level action explanations, never private model reasoning or credentials.

Context tabs: Impact, Timeline, Hypotheses, Evidence. Selecting an old execution step shows the context known at that step, visibly labeled historical; Return to latest restores current state. The final report summarizes symptoms, observations, leading explanation, alternatives, gaps, impact, resolution and next checks.

Acceptance: reconnect resumes persisted event sequence without duplicates; historical selection does not accidentally show later evidence as previously known.

### R-007 — Human questions and diagnostics [P2]

Agent can pause for a scoped question or manual check, explaining why it matters. Questions have typed options/free text; manual checks include instructions, expected output, scope and whether commands could change state. TRACE does not run user commands through its read-only tools. Authorized user submits redacted results, records claimed observation time and confirms environment. Preserve actor/time/raw-redacted output and independent verification state. Answers resume only the matching pending request; stale/duplicate submission returns current status.

Acceptance: Viewer cannot submit; two concurrent submissions cannot resume twice; fabricated/contradictory submissions remain labeled human-supplied and do not bypass verification.

### R-008 — Timeline, hypotheses and evidence [P2]

Timeline has changes/failures/runtime/investigation lanes, onset marker and event detail with event/ingestion time; mobile uses chronological vertical list. Hypotheses show supported/plausible/contradicted/insufficient labels, supporting and opposing citations, missing evidence and next useful check. Assessments are versioned. Labels are not numeric probabilities. Evidence browser filters by kind/service/time; includes source excerpt, graph path, code/diff/measurement viewer, provenance and coverage. Show missing sources as gaps, not fabricated records.

Acceptance: citation opens matching content/hash/revision; deleting a source connection does not silently invalidate retained citations; unknown time remains unknown.

### R-009 — Impact and graph interaction [P1 code; P2 runtime; P3 proposed patches]

Display direct exposure, potential propagation, observed impact and unknown as distinct non-exclusive categories. Provide path explanation and evidence for each component. Graph supports search, relation filters, community filters, most-connected list, node selection, file/line/revision, uses/used-by, expand bounded hops, reverse dependency impact, zoom/pan/drag/fit and keyboard-accessible node list. Display total and shown counts, truncation and selected revision manifest. Default two-hop code expansion is configurable to max four after scope checks.

Acceptance: reachable-only node is never colored as observed failure; telemetry gaps never become healthy states; graph/table convey equivalent classification.

### R-010 — TRACE Bot [P2 answers; P3 patch actions]

Contextual conversation belongs to project/run; header shows scope and as-of time. Support new conversation, history, current conversation, streaming, stop generation, copy, regenerate, follow-up prompts and citation navigation. Regenerate creates a new answer version preserving prior answer/evidence references. Questions requiring work enqueue a bounded follow-up job, not unrestricted synchronous execution. Structured patch/diff cards link to immutable PatchVersion records; wording in chat cannot bypass approval.

Acceptance: previous conversation remains accessible; model interruption leaves a marked partial answer; stale scope change requires explicit conversation context selection; invalid citation is rejected before finalizing answer.

### R-011 — Changes and proposed fixes [P3]

Views Need approval/In progress/Merged/All with project/environment/type/risk/status filters. Change/fix detail includes rationale and citations, originating ticket/run, base revision, predicted exposure, test status, files/line annotations, expandable context, maximize diff, edit proposal, blast-radius modal/table, branch/PR links and approval progress. Risk is an explainable assessment with limits, not a magic percentage.

Editing creates a new patch version and invalidates prior approvals. A stale base blocks PR update until reconciliation and re-review. Suggested tests are not executed tests. Failure to validate appears explicitly. External PR state is reconciled from provider evidence; retries must not duplicate branches/PRs.

Acceptance: approved patch cannot change under its approval hash; patch with failed checks cannot claim validated; merge occurs in provider, not a TRACE merge button.

### R-012 — Governance and external writes [P3]

Per-project/environment rules: eligible approvers, count, author exclusion, CODEOWNERS matching, allowed target branch, `trace/*` branch naming, required change-ticket type/destination, timezone-aware freeze windows, emergency override permissions/reason/post-review, redaction policy. Defaults: production needs two independent eligible reviewers and a linked change ticket; other environments need one eligible non-author reviewer. Single-user deployments can investigate/propose but cannot pretend to satisfy independent approval; admin policy changes are explicit and audited.

An authenticated approver acts only as themselves. Missing required ticket connector blocks the write with configuration action; manual external ticket link can satisfy policy only after scoped validation. Creating a change ticket is a separate authorized command. A minimal Jira writer is delivered in P3 for this workflow; the broader Jira connector/intake experience remains P4. Draft PR creation/update rechecks patch hash, approvals, policy version, credentials, scope, branch base and freeze at dispatch time. TRACE never auto-approves its own proposal. Emergency override does not grant merge/deploy capability.

Acceptance: revoked approval blocks pending dispatch; edited policy is re-evaluated; uncertain write response is reconciled by operation key before retry.

### R-013 — Observability and recovery [P3]

After changes view lists watching/recovered/regressed/inconclusive/unable-to-verify with pre/post error counts/rates, deployment and observation windows. Detail shows criteria, query/scope, baseline, sampling/traffic coverage, checkpoints and evidence. Merge starts awaiting-deployment tracking; deployment/traffic exposure establishes observation start. Require configured minimum request count and duration; zero requests is insufficient. Match environment/service/query definition between baseline and follow-up; disclose scope differences.

Regression presents Reopen investigation; this explicit action creates a new run. Recovery does not automatically close ticket. Pause/failed checks and rate-limited sources show stale state. No simulation timer marks recovery.

Acceptance: merge without deploy never becomes recovered; insufficient traffic yields inconclusive; regressed record links to new run only after user action.

### R-014 — Analytics and review [P4]

Analytics has date/project/environment filters, outcome chart and project breakdown. Require an authenticated reviewer to label diagnosis Correct/Partial/Wrong/Appropriate abstention/Unreviewed, with rationale and revision history. Exclude unreviewed from accuracy denominator. Accuracy = Correct / (Correct + Partial + Wrong); report abstention separately. Abstention reviews additionally record appropriate/inappropriate. Appropriate-abstention rate = appropriately reviewed abstentions / all reviewed abstentions; abstention frequency = abstained completed runs / all completed runs in the selected cohort. Report both with sample size; do not infer appropriateness from the absence of a finding. Unsupported-claim rate = reviewed claims lacking valid supporting evidence / all reviewed claims. Citation validity is a separate automated metric, not truth accuracy.

Time to cause = first supported finding timestamp minus user investigation start; show ticket-to-finding separately to avoid conflation. PR acceptance = merged proposed PRs / decided PRs (merged or closed unmerged) in selected cohort; disclose major-rework labels. Baseline comparisons require recorded comparison data. Display N/A with denominator zero and sample size everywhere.

Acceptance: known fixture yields exact values; filter changes denominator; generated confidence never supplies a review label.

### R-015 — Projects and wizard [P1]

Project list includes search, owner, providers, environments, code status, coverage, last sync and readiness checklist. Wizard order matches final prototype: Basics → Cloud → Log scope → Code → Tickets → Review. Basics includes name uniqueness per organization, description, owner and environment names. Allow verified/skip/pending stages, save/resume and edit review links. Optional Supabase backend-services connector belongs in cloud step. Readiness is capability-specific, not one misleading green badge.

Project detail tabs: Connectors, Code, Log scope, Architecture, Logs, Team. Project membership and service/repository mapping are explicit. One repo is not automatically one runtime service. Code may be indexed before cloud/ticket integrations are ready.

Acceptance: partial project survives reload; duplicate project name conflicts clearly; final review reflects repository selections and scope accurately.

### R-016 — Connector catalog and lifecycle [P1 GitHub/basic lifecycle; P4 remaining]

Hosting: Azure, AWS, GCP, Vercel, Railway, Other. Code: GitHub, Azure DevOps, Other. Tickets: Jira, ServiceNow, Azure Boards, None. Backend services: optional Supabase. Other uses authenticated generic import/webhook contracts, not a fake connection.

Connection lifecycle: draft → verifying → connected / connected_with_warnings / failed; later expired/revoked/disconnected. Validate credentials server-side against actual provider; format checks alone do not establish connection. Scope review, external auth handoff, save/edit/reverify/replace/disconnect and provider-switch confirmation required. Preserve usable credentials during failed replacement; disconnect halts future jobs and revokes local access without erasing retained evidence.

Provider detail tabs: Connection; hosting CI/CD and Databases; code Path mappings; ticket Destination configuration; Supabase Logs/history. Customer databases expose permitted logs/metadata only. No database row viewer or live schema scan.

Acceptance: simulated timers cannot verify; over-broad credentials show effective permissions; provider switch does not leave old jobs silently running; failures retain actionable reason.

#### R-016 provider setup parity and capability gates

These are required prototype affordances and data fields, not verified claims about current provider APIs. Each adapter must verify supported authentication modes, permission scopes and plan limits in T-001/T-021 before enabling them. Unavailable modes stay visibly disabled with an explanation; never invent a TRACE publisher, app ID, OAuth grant, template URL or federation issuer. IDs and setup snippets come from provisioned deployment configuration. Copy/download setup snippets must be safely escaped and must never execute infrastructure changes automatically.

| Provider | Setup fields and actions to preserve | Specific validation and coverage |
|---|---|---|
| Azure | Cloud partition; tenant; TRACE app consent or own registration; client ID and federated/secret/certificate mode; secret expiry/certificate upload; subscription/resource group/workspace; CLI/Bicep/Terraform guidance | Verify identity, allowed resource scope and log query separately; consent alone grants no resource access. Show missing diagnostics, certificate expiry and unsupported federation as gaps. |
| AWS | Account ID, role ARN, generated per-connection External ID, selected regions; CloudFormation/Terraform/manual trust guidance | Verify assumed account and region scope; show policy-inspection limitations. No access-key fallback. Generated trust values belong to the configured TRACE deployment. |
| GCP | Project/folder/organization scope; service-account email; impersonation or federation provider; gcloud/Terraform/console guidance | Validate actual granted scope and log availability; distinguish perimeter restrictions and absent audit logs. No downloaded JSON private keys. |
| GitHub | github.com or enterprise server URL; app installation and organization approval-pending state; selected repositories; fine-grained token trial mode/expiry | P1 implements tested cloud read installation; enterprise/trial alternatives remain visible and gated until tested. P3 uses a separately installed remediation identity. Backend branch restrictions and provider rulesets are separate controls; do not claim token scopes enforce branch patterns. |
| Azure DevOps | Organization URL; Entra identity or organization-scoped PAT/expiry; project and repository discovery; path mappings | Separate code/build read permissions from optional PR writes; unsupported TFVC disabled. Do not embed prototype token-deprecation dates as current provider facts. |
| Jira | OAuth or scoped token; site; account email where required; expiry; destination project/type and summary template | Discover destination values rather than hardcode examples; separate intake and write grants. P3 minimal writer validates required fields; P4 supplies broad discovery/intake. |
| ServiceNow | Instance; supported OAuth identity or restricted trial fallback; change model, assignment group and standard template | Release/auth compatibility checked before enablement. Validate destination mandatory fields and preserve external approval state without treating it as a TRACE patch approval. |
| Azure Boards | Reuse authorized code connection or separate organization identity; project/type/area/iteration; approved/rejected state mappings | Discover process-specific fields and states. Reuse never silently adds write privilege. Sanitize outbound HTML; link the created item to its PR. |
| Vercel | Integration or scoped token; team/project selection; plan/capability; log-drain setup; environment and preview selection | Warn if actual credential can write despite TRACE read-only policy. Verify drain delivery and expose missing historical coverage; previews excluded until explicitly selected. |
| Railway | Supported OAuth or workspace/project token; workspace/project/environment selection; plan/capability | Enforce query-only connector operations; show actual credential powers. Display unavailable/expired logs and short-lived preview scope explicitly. |
| Supabase add-on | Supported OAuth/scoped token; organization; per-environment project mapping; selected log sources; plan/capability | Include edge, Postgres, auth, function, storage, realtime and pooler sources only when supported. Preserve function versions observed since connection and migration metadata; missing historical versions/times remain unknown. No row queries, database password or service-role credentials requested from customer projects. |

Credential-expiry reminders are configurable and use known expiry timestamps; unknown expiry cannot generate a fabricated countdown. Connection checks show actual stage outcomes, timestamps, warnings and retry actions. Logs/history views show source-specific retention and last successful read. Preview/branch associations need verified project/environment/revision mappings; matching branch names alone is an uncertain candidate. All provider-supplied HTML and setup output is escaped or sanitized.

Acceptance: each enabled provider passes its real identity/discovery/bounded-read checks; every prototype setup mode is implemented or visibly gated; separate remediation identity cannot be used by read-only indexing; source history never predates TRACE's available observations without provider evidence.

### R-017 — Repository import and Graphify [P1]

Repository browser search/filter, visibility and archived toggle; branch/explicit revision selection; unsupported TFVC disabled. Resolve commit before indexing. States not_indexed/queued/running/ready/ready_with_gaps/failed/cancelled. Display real processed/excluded/failed counts, progress stage, exact revision, versions and warnings. Actions import, build, rebuild, retry, cancel, build history. Reuse existing matching successful build unless forced.

Graph settings: code-only enabled initially; rebuild-on-deployment preference; docs/README semantic extraction disabled with P4 explanation until separately consented model processing is implemented. Multiple repo graphs expose a revision manifest, never a single misleading project SHA. Failed rebuild preserves prior published build. Source viewer opens exact file/revision/line and expandable context.

Acceptance: cross-repo same names do not collide; partial parsing reports gaps; deployment mapping unknown is separate from graph ready; extraction does not execute source scripts.

### R-018 — Log scope and console [P2 generic; P4 providers]

Per-environment workload/resource selection with log availability and freshness. Log console selects environment/workload/window, supports query editor for connector-supported provider syntax, row caps, scanned bytes if known, results/export and investigation prefill. Default mode searches already ingested evidence with TRACE filters. Explicit Fetch from source executes a bounded read-only connector job, normalizes results into Neo4j, then displays those evidence records. Provider query strings are never sent as Cypher.

Acceptance: console mode clearly states stored versus source refresh; source-result ingestion failures do not become investigator evidence; resource unavailable/stale differs from zero matching rows.

### R-019 — Settings, access, credentials and audit [P1 essentials; P3 policy; P4 expanded]

Settings: environment rules, approvers, members, roles, audit, credentials, notifications, preferences. Roles Owner/Admin/Approver/Investigator/Viewer with explicit project/environment grants. Team tab reflects project membership; organization membership alone does not grant every project. Approver does not inherently administer connectors. Audit search/filter/export includes actor, action, target, project, time, result and correlation ID.

Credentials inventory shows provider/project, credential method, stored-secret versus delegated identity, verification, expiry, needs-attention, open/verify/rotate/disconnect. No secret readback. Rotation requires privileged actor and audit. Token expiry unknown is Unknown, not Never.

Preferences: light/dark/system, density, page size, timezone, starting project/environment, account. Apply immediately where safe; starting scope must still be authorized on next login. Notification settings questions/fixes/PR updates/daily digest support in-app first, email in P4; delivery honors preferences and dedupes.

Acceptance: disabled account loses protected API access; viewer cannot export forbidden evidence; rotation verifies replacement before activation; signed download links are scoped and expire.

### R-020 — Responsive behavior [all phases]

Preserve layout intent at prototype thresholds: around 1260px collapse changes panes; 1180px reduce grids; 1100px collapse home/investigation side panels; 900px drawer/single-column auth/settings/wizard; 640px condensed toolbar and vertical timeline. Test 1440, 1024, 768, 390 and 320px. Exact breakpoint implementation may adapt to content without removing controls. Tables can scroll locally; dialogs/diffs have accessible close actions; touch graph controls and table alternative remain usable.

Acceptance: no whole-page horizontal overflow, clipped primary actions or keyboard traps at test widths; reduced motion disables nonessential animation.

## 5. Prototype coverage matrix

Each family is accounted for below. Covered means specified, not implemented. Source references are function names and approximate lines in supplied HTML.

| Audit family | Prototype anchor | Requirements | Architecture owner | Ontology | Task | Phase |
|---|---|---|---|---|---|---|
| AUTH-LANDING | landing/login 1340–1475 | R-001 | auth/web | membership outside evidence | T-003 | P1 |
| UI-SHELL | NAV7/drawSwitch 3078,3449 | R-002 | web/scope API | project/environment | T-004 | P1 |
| UI-HOME | vHome 3422 | R-003 | dashboard | run/request/plan | T-014 | P2/P3 |
| UI-SIDEBAR | drawSideExtra 3401 | R-003,R-019 | events/metering | run refs | T-023 | P4 |
| TICKET | vTickets/assignTicket 3111 | R-004 | tickets | Ticket/Run | T-010 | P2 |
| INV-LIST | vInvProject 3129 | R-005 | investigations | Run | T-012 | P2 |
| INV-RUN | stepHTML/vInvestigate 3185 | R-006 | orchestrator/events | Step/ToolExecution | T-012 | P2 |
| INV-HUMAN | answerStep/submitSteps | R-007 | human-input | HumanCheck/Observation | T-013 | P2 |
| INV-CONTEXT | drawInvSide 3172 | R-008,R-009 | retrieval/graph | Assessment/Impact | T-012 | P2 |
| INV-TIMELINE | tTimeline 4038 | R-008 | event projections | temporal evidence | T-012 | P2 |
| BOT | drawBot/prBuild 3332 | R-010 | chat/patch | ConversationAnswer/CITES | T-014,T-016 | P2/P3 |
| CHANGE | vChangesPR/vFix 2947 | R-011 | changes | Change/PatchVersion | T-016 | P3 |
| GOVERNANCE | ruleDetail/advanceFix | R-012 | policy/writes | plan refs | T-017 | P3 |
| OBS | vObsDetail/obsChanges 3145 | R-013 | verification | Deployment/Verification | T-019 | P3 |
| ANALYTICS | obsAnalytics 3149 | R-014 | review/metrics | Finding/Review refs | T-023 | P4 |
| PROJECT | vProjectDetail 2995 | R-015 | projects | structural graph | T-005 | P1 |
| WIZARD | NP_STEPS/wizBody 3883 | R-015 | setup | project scope | T-005 | P1 |
| CONNECTOR | connSheet 4118 | R-016 | connector adapters | source provenance | T-006,T-021 | P1/P4 |
| CONN-LIFECYCLE | connForm/saveConn | R-016,R-019 | credentials/jobs | SourceConnection ref | T-006,T-021 | P1/P4 |
| CONN-PROVIDER-SETUP | CDEF 3633–3777; V17 4062–4113 | R-016,R-019 | provider capability registry | SourceConnection/EvidenceGap | T-006,T-018,T-021 | P1/P3/P4 |
| CONN-DETAIL | vConnector 4126 | R-016 | connectors | deployment/schema refs | T-021,T-022 | P4 |
| CODE-IMPORT | codeBody/repoBrowser 3473 | R-017 | indexing | GraphBuild/Revision | T-007 | P1 |
| GRAPH | archTab/cgPanel 3541 | R-009,R-017 | graph API | code/runtime graph | T-008 | P1/P2 |
| GRAPH-CONTROLS | cgInit/cgStyle 3984 | R-009,R-020 | graph UI | bounded paths | T-008 | P1 |
| LOGS | logsTab/runLogQuery 3024 | R-018 | ingestion/retrieval | Evidence/Gap | T-009,T-021 | P2/P4 |
| SETTINGS | SETNAV/vSettings3 3236 | R-019 | policy/membership | audit refs | T-003,T-017,T-023 | P1/P3/P4 |
| CREDENTIALS | credsPage/credRows 3946 | R-019 | secret manager | no secret nodes | T-006 | P1 |
| RESPONSIVE | CSS 498–1279 | R-020 | web | not applicable | T-004,T-024 | all |

## 6. Explicit prototype corrections

D-01: assignment never starts a run. D-02: provider query UI uses ingestion before graph evidence. D-03: recovery follows deployment, not merge. D-04: external change-ticket write and draft-PR write are separate privileges. D-05: approvals attach to patch hash; no impersonated approve-as controls. D-06: customer database logs only; source-derived schemas disclose drift. D-07: actual job outcomes replace timers/random arrays. D-08: historical replay never reruns tools. D-09: reviewed metrics replace mock percentages. D-10: legacy routes are superseded, not extra mandatory top-level screens. D-11: production apply/rollback simulation becomes a recommendation/manual-action record, not autonomous deployment. D-12: Graphify extraction tags never substitute for observed runtime proof.

## 7. Release acceptance

Every requirement has a task, API/job owner and acceptance test. No unexplained missing prototype family. Every metric identifies source/window/denominator/freshness. Every write has authorization/idempotency/reconciliation. Validate responsive widths, keyboard, cross-project/environment/revision isolation, real ingestion, restart and streaming reconnect. Do not certify provider capabilities until tested against official current API documentation and a configured account. Final document review is not a claim of executed application tests.
