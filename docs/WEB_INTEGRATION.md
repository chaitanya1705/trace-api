# trace-web: replacing mock data with the real API

Audience: the session working in `trace-web`. Types come from `docs/openapi.json` in this repo (regenerate with
`python -m app.openapi_export`). Base URL `NEXT_PUBLIC_API_URL` (e.g. `http://localhost:8000`) + `/api/v1`.

## Rules (user decisions)
- **No login at all.** Remove the demo-session cookie, `proxy.ts` redirect and login/signup/reset pages from the flow.
  Landing "Open workspace" goes straight to `/home`. Call the API with no token. (`GET /config` returns
  `{auth_mode:"off"}`; `X-Dev-User: someone@x.com` switches user, only for testing two people.) First `GET /me`
  creates the "Dev Org".
- **Delete the mock data**, not just stop using it: everything in `src/data/*.ts` (projects, tickets, incidents,
  fixes, steps, org, repos, connectors, graph, codegraph, changes) plus the usage/away/on-call sample numbers in
  `SideExtra`. Screens whose API does not exist yet show an honest empty/"not available yet" state - never sample rows.
- Connectors: **GitHub and Vercel only** (plus generic). Settings pages are out of scope (remove from nav).
- Responses are `{data, meta:{request_id, as_of, next_cursor?, coverage?}}`; errors `{error:{code,message,retryable,details},request_id}`.
  Show `request_id` in error states. Times are UTC ISO strings.

## Screen -> endpoint (ready now)
| Screen | Endpoints |
|---|---|
| Projects list | `GET /projects?q=&sort=name|updated&cursor=` (readiness per capability in each row; `meta.coverage.total_authorized` distinguishes empty dataset from no matches) |
| Wizard (Basics, Hosting, Log scope, Code, Review) | `POST /project-drafts`, `PATCH /project-drafts/{id}` (send `version`; `stages:{hosting:"skipped"}`), `GET /project-drafts/{id}/review`, `POST /project-drafts/{id}/activate` |
| Connect GitHub (no password) | `POST /connect/github/start {draft_id|project_id}` -> `{url}`; navigate the browser to it. GitHub returns to the API callback, which redirects to `<web>/projects/new?draft=<id>&connected=github`. Then poll `GET /project-drafts/{id}/connections` until `status` leaves `draft/verifying` |
| Connect Vercel | `POST /project-drafts/{id}/connections {provider:"vercel", auth_method:"vercel_token", secret, config:{team_id?}}` -> 202 `{job_id}`; poll `GET /jobs/{job_id}` or `GET /connections/{id}`; stage results: `GET /connections/{id}/checks` |
| Provider capability gating | `GET /providers` (each auth mode has `available` + `reason`; show unavailable modes disabled with the reason) |
| Pick repo / Vercel project | `GET /connections/{id}/resources?q=&include_archived=` then save via draft `data.repositories` / `data.log_scope` |
| Project detail: Connectors | `GET /projects/{id}/connections`, `POST /connections/{id}/verify|rotate|disconnect` |
| Project detail: Code | `GET/PUT /projects/{id}/repositories`, `GET/PUT /projects/{id}/repositories/{binding}/mappings` (services + stack-trace path mappings) |
| Project detail: Log scope | `GET/PUT /projects/{id}/log-scope` (availability is `unknown` until a log drain exists - show as a gap, never green) |
| Project detail: Team | `GET/PUT /projects/{id}/members` |
| Environments / services | `GET/PUT /projects/{id}/environments|services` |
| Tickets | `GET /tickets?view=need_project|open|resolved|all&q=&severity=&environment=&project_id=&sort=opened|severity|title|status&cursor=` (`meta.coverage.counts` gives the tab counts), `POST /tickets`, `PATCH /tickets/{id}` (assign: `{version, project_id}`; assigning starts nothing), `GET /tickets/{id}`, `GET /tickets/{id}/payload`, `POST /tickets/import/pulselab` (body = PulseLab export JSON) |
| Investigate button | Ticket field `investigate:{available, reason}`. Render disabled with the reason until the orchestrator exists |
| Code tab: Generate graph | `POST /projects/{id}/graph/index {}` -> 202, one run per bound repository (a second click returns the active run); poll `GET /projects/{id}/graph/index` until `status` leaves `queued/running` (`stage`: resolving, downloading, extracting, indexing; `failed` carries `error.code/message`). Needs the worker (`python -m app.worker_main`) |
| Architecture: Code graph | `GET /projects/{id}/graph/code?limit=&community=` (published build only; nodes, edges, communities, `truncated`), `GET /projects/{id}/graph/symbols/{symbol_id}` (callers, callees, pinned source chunks with hash). `503 dependency_unavailable` when Neo4j is not configured; `status:"none"` when nothing is indexed |
| Demo links | `GET /integrations/demo` -> PulseLab/ShopFlow URLs (`configured:false` when blank) |
| Health | `GET /health/ready` |

## Not available yet (show empty / unavailable, no fake numbers)
Home counters and Needs-you queue, Investigate runs/steps/impact/timeline/hypotheses/evidence, Changes & PRs, Observability,
Analytics, TRACE Bot, Since-you-were-away, usage meters, the system map and database diagram (the code graph itself is available, see above).
Nav badges must come from real counts or be hidden (the "2" badges in the current UI are sample numbers).

## Specific things visible in the current Projects screen that must go
Payments / Fulfilment / Storefront / Accounts rows, "Owner Priya S.", "3 workloads without logs" badges, the Jira/ServiceNow-style
"Tickets" column ("GitHub Issues"/"None": there is no ticket-provider connector), "Since you were away" items, "Today's usage 1.8 of 5 GB /
212 of 500". With a fresh database the page should show the empty state ("No projects yet - New project").
