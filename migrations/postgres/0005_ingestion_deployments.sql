-- Runtime evidence intake (T-009). Neo4j is the investigator's only retrieval source (architecture A2); this table is the
-- DURABLE RECEIPT of accepted events plus the sync state toward the graph. Rows are immutable, redacted and deduplicated on
-- (connection, provider record id). graph_status says honestly whether an event is in the graph yet.
CREATE TABLE evidence_inbox (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  organization_id uuid NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
  project_id uuid NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  connection_id uuid NOT NULL REFERENCES connections(id) ON DELETE CASCADE,
  environment_id uuid REFERENCES environments(id) ON DELETE SET NULL,      -- NULL = could not be mapped (unknown, not guessed)
  provider text NOT NULL,
  kind text NOT NULL DEFAULT 'log',
  source_record_key text NOT NULL,                                          -- provider's own record id (dedupe key)
  event_time timestamptz,                                                    -- NULL stays unknown; ingest time is never used instead
  ingested_at timestamptz NOT NULL DEFAULT now(),
  level text,
  source text,                                                               -- vercel: build|edge|lambda|static|external|firewall|redirect
  provider_project_id text,
  deployment_id text,
  request_id text,
  status_code integer,
  service_hint text,
  message text,                                                              -- redacted, capped
  content_hash text NOT NULL,
  extra jsonb NOT NULL DEFAULT '{}'::jsonb,
  graph_status text NOT NULL DEFAULT 'pending' CHECK (graph_status IN ('pending','synced','failed')),
  UNIQUE (connection_id, source_record_key)
);
CREATE INDEX evidence_inbox_time ON evidence_inbox(project_id, environment_id, event_time DESC);
CREATE INDEX evidence_inbox_request ON evidence_inbox(project_id, request_id) WHERE request_id IS NOT NULL;
CREATE INDEX evidence_inbox_pending ON evidence_inbox(ingested_at) WHERE graph_status = 'pending';

CREATE TABLE ingestion_batches (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  organization_id uuid NOT NULL,
  project_id uuid NOT NULL,
  connection_id uuid NOT NULL REFERENCES connections(id) ON DELETE CASCADE,
  received integer NOT NULL,
  accepted integer NOT NULL,
  duplicates integer NOT NULL,
  out_of_scope integer NOT NULL,
  rejected integer NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ingestion_batches_conn ON ingestion_batches(connection_id, created_at DESC);

-- Per-connection secrets that are not the provider credential (e.g. the log-drain signing secret). Sealed like all secrets.
CREATE TABLE connection_secrets (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  organization_id uuid NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
  connection_id uuid NOT NULL REFERENCES connections(id) ON DELETE CASCADE,
  kind text NOT NULL,
  version integer NOT NULL DEFAULT 1,
  key_version integer NOT NULL,
  nonce bytea NOT NULL,
  ciphertext bytea NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (connection_id, kind)
);

-- Deployments observed through the hosting connection. commit_sha is taken from provider metadata only; when absent the
-- revision mapping is 'unknown' (ambiguous_revision gap), never inferred from a branch name.
CREATE TABLE deployments (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  organization_id uuid NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
  project_id uuid NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  connection_id uuid NOT NULL REFERENCES connections(id) ON DELETE CASCADE,
  environment_id uuid REFERENCES environments(id) ON DELETE SET NULL,
  provider_deployment_id text NOT NULL,
  provider_project_id text NOT NULL,
  project_name text,
  target text,
  state text,
  commit_sha text,
  git_ref text,
  git_repository text,
  revision_mapping text NOT NULL CHECK (revision_mapping IN ('provider_metadata','unknown')),
  url text,
  created_at_provider timestamptz,
  ready_at timestamptz,
  first_seen_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (connection_id, provider_deployment_id)
);
CREATE INDEX deployments_project_time ON deployments(project_id, created_at_provider DESC);

ALTER TABLE evidence_inbox ENABLE ROW LEVEL SECURITY;
ALTER TABLE ingestion_batches ENABLE ROW LEVEL SECURITY;
ALTER TABLE connection_secrets ENABLE ROW LEVEL SECURITY;
ALTER TABLE deployments ENABLE ROW LEVEL SECURITY;
