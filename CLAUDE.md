# CLAUDE.md — TRACE (trace-api)

Applies to this project only. Repos: trace-web (https://github.com/chaitanya1705/trace-web.git), trace-api (https://github.com/chaitanya1705/trace-api).

## Docs
- `TRACE/` holds the plans: Context, PRD, architecture, ontology. Read them before starting a task.
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
