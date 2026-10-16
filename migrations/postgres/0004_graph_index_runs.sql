-- Control-plane record of code-graph indexing runs (the graph itself lives in Neo4j). One row per request; the job queue
-- executes it. Status here is what the UI shows: a run is never reported "succeeded" until the build is published.
CREATE TABLE graph_index_runs (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  organization_id uuid NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
  project_id uuid NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  repository_binding_id uuid NOT NULL REFERENCES repository_bindings(id) ON DELETE CASCADE,
  job_id uuid,
  status text NOT NULL DEFAULT 'queued' CHECK (status IN ('queued','running','succeeded','failed','cancelled')),
  stage text,                                   -- resolving | downloading | extracting | indexing | publishing
  requested_ref text NOT NULL,                  -- branch or commit the user chose
  commit_sha text,                              -- exact commit actually indexed (resolved, never assumed)
  build_id text,
  manifest_id text,
  counts jsonb,                                 -- files / chunks / symbols / edges as reported by the indexer
  gaps jsonb,                                   -- coverage gaps (unsupported language, partial parse, skipped files)
  extractor text,
  error jsonb,                                  -- {code, message}: never contains a token or file content
  requested_by uuid,
  created_at timestamptz NOT NULL DEFAULT now(),
  started_at timestamptz,
  finished_at timestamptz
);
CREATE INDEX graph_index_runs_project ON graph_index_runs(project_id, created_at DESC);
-- At most one active run per repository binding: a double click returns the active run instead of starting another.
CREATE UNIQUE INDEX graph_index_runs_active ON graph_index_runs(repository_binding_id) WHERE status IN ('queued','running');
ALTER TABLE graph_index_runs ENABLE ROW LEVEL SECURITY;
