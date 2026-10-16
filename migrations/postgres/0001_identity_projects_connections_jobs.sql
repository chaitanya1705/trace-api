-- P1 control-plane schema: identity, projects, connections, durable jobs, outbox, audit.
-- Every organization-owned row has organization_id; project-owned rows also project_id (A4).
-- RLS is enabled with NO policies: Supabase's public API sees nothing; only the backend role reads/writes.
-- Backend authorization remains mandatory (A4/A11).

CREATE OR REPLACE FUNCTION touch_updated_at() RETURNS trigger AS $$
BEGIN
  NEW.updated_at = now();
  NEW.version = COALESCE(OLD.version, 0) + 1;
  RETURN NEW;
END $$ LANGUAGE plpgsql;

CREATE TABLE organizations (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  name text NOT NULL CHECK (length(btrim(name)) BETWEEN 1 AND 120),
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  version integer NOT NULL DEFAULT 1
);

CREATE TABLE memberships (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  organization_id uuid NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
  user_id uuid NOT NULL,                       -- Supabase auth.users.id
  email text NOT NULL,
  display_name text,
  role text NOT NULL CHECK (role IN ('owner','admin','approver','investigator','viewer')),
  status text NOT NULL DEFAULT 'active' CHECK (status IN ('active','disabled','revoked')),
  revoked_at timestamptz,
  authz_version integer NOT NULL DEFAULT 1,    -- bumped on any grant change (A5)
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  version integer NOT NULL DEFAULT 1,
  UNIQUE (organization_id, user_id)
);
CREATE INDEX memberships_user ON memberships(user_id) WHERE status = 'active';

-- Invitation-only baseline: signing up grants nothing (R-001).
CREATE TABLE invitations (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  organization_id uuid NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
  email text NOT NULL,
  role text NOT NULL CHECK (role IN ('owner','admin','approver','investigator','viewer')),
  token_hash text NOT NULL UNIQUE,             -- sha256 of the emailed token; token itself never stored
  status text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','accepted','revoked','expired')),
  invited_by uuid NOT NULL,
  expires_at timestamptz NOT NULL,
  accepted_by uuid,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  version integer NOT NULL DEFAULT 1
);
CREATE UNIQUE INDEX invitations_one_pending ON invitations(organization_id, lower(email)) WHERE status = 'pending';

CREATE TABLE projects (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  organization_id uuid NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
  name text NOT NULL CHECK (length(btrim(name)) BETWEEN 1 AND 80),
  name_key text NOT NULL,                      -- lower(btrim(name)); unique per organization
  description text,
  owner_user_id uuid NOT NULL,
  status text NOT NULL DEFAULT 'active' CHECK (status IN ('active','archived')),
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  version integer NOT NULL DEFAULT 1,
  UNIQUE (organization_id, name_key)
);

-- Organization membership alone never grants project access (R-019). environments NULL = all.
CREATE TABLE project_memberships (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  organization_id uuid NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
  project_id uuid NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  user_id uuid NOT NULL,
  role text NOT NULL CHECK (role IN ('admin','approver','investigator','viewer')),
  environment_ids uuid[],
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  version integer NOT NULL DEFAULT 1,
  UNIQUE (project_id, user_id, role)
);
CREATE INDEX project_memberships_user ON project_memberships(user_id);

CREATE TABLE environments (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  organization_id uuid NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
  project_id uuid NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  name text NOT NULL,
  environment_key text NOT NULL CHECK (environment_key ~ '^[a-z0-9][a-z0-9_-]{0,31}$'),
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  version integer NOT NULL DEFAULT 1,
  UNIQUE (project_id, environment_key)
);

-- Logical runtime component. One repository is NOT automatically one service (R-015).
CREATE TABLE services (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  organization_id uuid NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
  project_id uuid NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  name text NOT NULL,
  service_key text NOT NULL CHECK (service_key ~ '^[a-z0-9][a-z0-9_.-]{0,63}$'),
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  version integer NOT NULL DEFAULT 1,
  UNIQUE (project_id, service_key)
);

-- Connections: draft -> verifying -> connected | connected_with_warnings | failed; later expired/revoked/disconnected.
-- Only github (code) and vercel (hosting) are provider-backed in this build; 'generic' = authenticated import/webhook.
CREATE TABLE connections (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  organization_id uuid NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
  project_id uuid NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  provider text NOT NULL CHECK (provider IN ('github','vercel','generic')),
  kind text NOT NULL CHECK (kind IN ('code','hosting')),
  auth_method text NOT NULL,                   -- e.g. github_app, github_pat, vercel_integration, vercel_token, signed_webhook
  status text NOT NULL DEFAULT 'draft' CHECK (status IN
    ('draft','verifying','connected','connected_with_warnings','failed','expired','revoked','disconnected')),
  config jsonb NOT NULL DEFAULT '{}'::jsonb,   -- non-secret provider ids (installation id, team id, host...)
  capabilities jsonb NOT NULL DEFAULT '{}'::jsonb,
  effective_scopes jsonb,                      -- what the credential can actually do, discovered not assumed
  last_verified_at timestamptz,
  last_error jsonb,
  credential_expires_at timestamptz,           -- NULL means Unknown, never 'Never' (R-019)
  active_credential_version integer,
  created_by uuid NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  version integer NOT NULL DEFAULT 1,
  UNIQUE (project_id, provider, kind)
);

-- Ciphertext only. Key lives in backend env; key_version selects it (A11).
CREATE TABLE credential_versions (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  organization_id uuid NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
  connection_id uuid NOT NULL REFERENCES connections(id) ON DELETE CASCADE,
  version integer NOT NULL,
  key_version integer NOT NULL,
  nonce bytea NOT NULL,
  ciphertext bytea NOT NULL,
  secret_kind text NOT NULL,
  status text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','active','revoked')),
  expires_at timestamptz,
  created_by uuid NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (connection_id, version)
);

CREATE TABLE connection_checks (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  organization_id uuid NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
  connection_id uuid NOT NULL REFERENCES connections(id) ON DELETE CASCADE,
  outcome text NOT NULL CHECK (outcome IN ('running','ok','warnings','failed')),
  stages jsonb NOT NULL DEFAULT '[]'::jsonb,   -- [{t, api, status: ok|warn|fail|run, message, at}]
  started_at timestamptz NOT NULL DEFAULT now(),
  finished_at timestamptz
);
CREATE INDEX connection_checks_conn ON connection_checks(connection_id, started_at DESC);

-- Stable repository identity; never name-only joins (A4). One repo may map to many services (and vice versa).
CREATE TABLE repository_bindings (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  organization_id uuid NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
  project_id uuid NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  connection_id uuid NOT NULL REFERENCES connections(id) ON DELETE CASCADE,
  provider_repository_id text NOT NULL,
  full_name text NOT NULL,
  default_branch text NOT NULL,
  selected_ref text NOT NULL,                  -- branch name or explicit revision requested
  visibility text,
  archived boolean NOT NULL DEFAULT false,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  version integer NOT NULL DEFAULT 1,
  UNIQUE (project_id, connection_id, provider_repository_id)
);

CREATE TABLE repository_service_mappings (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  organization_id uuid NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
  project_id uuid NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  repository_binding_id uuid NOT NULL REFERENCES repository_bindings(id) ON DELETE CASCADE,
  service_id uuid NOT NULL REFERENCES services(id) ON DELETE CASCADE,
  path_prefix text NOT NULL DEFAULT '',
  valid_from timestamptz NOT NULL DEFAULT now(),
  valid_to timestamptz,
  UNIQUE (repository_binding_id, service_id, path_prefix)
);

-- Stack-trace path -> repository path (prototype 'Path mappings' tab).
CREATE TABLE path_mappings (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  organization_id uuid NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
  project_id uuid NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  repository_binding_id uuid NOT NULL REFERENCES repository_bindings(id) ON DELETE CASCADE,
  runtime_prefix text NOT NULL,
  repo_prefix text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (repository_binding_id, runtime_prefix)
);

-- Which hosting resources belong to which environment ("Log scope" step/tab). Includes explicit no-logs state.
CREATE TABLE log_scope_entries (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  organization_id uuid NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
  project_id uuid NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  environment_id uuid NOT NULL REFERENCES environments(id) ON DELETE CASCADE,
  connection_id uuid NOT NULL REFERENCES connections(id) ON DELETE CASCADE,
  provider_resource_id text NOT NULL,
  resource_type text NOT NULL,
  name text NOT NULL,
  service_id uuid REFERENCES services(id) ON DELETE SET NULL,
  log_availability text NOT NULL DEFAULT 'unknown'
    CHECK (log_availability IN ('available','none','unknown','plan_limited','stale')),
  availability_note text,
  last_event_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  version integer NOT NULL DEFAULT 1,
  UNIQUE (environment_id, connection_id, provider_resource_id)
);

-- Wizard drafts: versioned, resumable, skip-able stages (R-015).
CREATE TABLE project_drafts (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  organization_id uuid NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
  created_by uuid NOT NULL,
  data jsonb NOT NULL DEFAULT '{}'::jsonb,     -- references connection ids, never secrets
  stages jsonb NOT NULL DEFAULT '{}'::jsonb,   -- {basics:'done', hosting:'skipped', ...}
  status text NOT NULL DEFAULT 'open' CHECK (status IN ('open','activated','discarded')),
  project_id uuid REFERENCES projects(id) ON DELETE SET NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  version integer NOT NULL DEFAULT 1
);

-- Durable queue (Postgres, SKIP LOCKED). Leases, retries, dead-letter (A7).
CREATE TABLE jobs (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  organization_id uuid NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
  project_id uuid REFERENCES projects(id) ON DELETE CASCADE,
  kind text NOT NULL,
  payload jsonb NOT NULL DEFAULT '{}'::jsonb,
  payload_version integer NOT NULL DEFAULT 1,
  idempotency_key text,
  status text NOT NULL DEFAULT 'queued' CHECK (status IN
    ('queued','running','succeeded','failed','cancel_requested','cancelled','dead')),
  priority integer NOT NULL DEFAULT 100,       -- lower runs first; human resume/cancel < light ingest < bulk rebuild
  attempt integer NOT NULL DEFAULT 0,
  max_attempts integer NOT NULL DEFAULT 3,
  next_attempt_at timestamptz NOT NULL DEFAULT now(),
  lease_owner text,
  lease_expires_at timestamptz,
  stage text,
  progress jsonb NOT NULL DEFAULT '{}'::jsonb,
  warnings jsonb NOT NULL DEFAULT '[]'::jsonb,
  result_ref jsonb,
  error jsonb,                                 -- {code, message, retryable}; no secrets
  requested_by uuid,
  resource_type text,
  resource_id uuid,
  created_at timestamptz NOT NULL DEFAULT now(),
  started_at timestamptz,
  finished_at timestamptz,
  updated_at timestamptz NOT NULL DEFAULT now(),
  version integer NOT NULL DEFAULT 1
);
CREATE UNIQUE INDEX jobs_idempotency ON jobs(organization_id, kind, idempotency_key) WHERE idempotency_key IS NOT NULL;
CREATE INDEX jobs_claim ON jobs(priority, next_attempt_at) WHERE status = 'queued';
CREATE INDEX jobs_lease ON jobs(lease_expires_at) WHERE status IN ('running','cancel_requested');
CREATE INDEX jobs_resource ON jobs(resource_type, resource_id);

-- Same-transaction domain write + outbox row; worker replays idempotently into Neo4j (A2).
CREATE TABLE outbox (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  organization_id uuid NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
  project_id uuid REFERENCES projects(id) ON DELETE CASCADE,
  kind text NOT NULL,
  operation_key text NOT NULL UNIQUE,          -- deterministic; replay-safe
  payload jsonb NOT NULL,
  status text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','applied','failed')),
  attempts integer NOT NULL DEFAULT 0,
  last_error text,
  next_attempt_at timestamptz NOT NULL DEFAULT now(),
  created_at timestamptz NOT NULL DEFAULT now(),
  applied_at timestamptz
);
CREATE INDEX outbox_pending ON outbox(next_attempt_at) WHERE status = 'pending';

CREATE TABLE webhook_deliveries (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  provider text NOT NULL,
  delivery_id text NOT NULL,
  connection_id uuid,
  received_at timestamptz NOT NULL DEFAULT now(),
  outcome text NOT NULL DEFAULT 'received',
  UNIQUE (provider, delivery_id)
);

-- Immutable audit trail (A4). No UPDATE/DELETE.
CREATE TABLE audit_events (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  organization_id uuid NOT NULL,
  project_id uuid,
  actor_user_id uuid,
  action text NOT NULL,
  target_type text,
  target_id text,
  result text NOT NULL CHECK (result IN ('success','denied','failure')),
  correlation_id text,
  details jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX audit_org_time ON audit_events(organization_id, created_at DESC);
CREATE OR REPLACE FUNCTION audit_immutable() RETURNS trigger AS $$
BEGIN RAISE EXCEPTION 'audit_events is append-only'; END $$ LANGUAGE plpgsql;
CREATE TRIGGER audit_no_update BEFORE UPDATE OR DELETE ON audit_events
  FOR EACH ROW EXECUTE FUNCTION audit_immutable();

-- updated_at/version triggers for mutable resources
DO $$
DECLARE t text;
BEGIN
  FOREACH t IN ARRAY ARRAY['organizations','memberships','invitations','projects','project_memberships',
    'environments','services','connections','repository_bindings','log_scope_entries','project_drafts','jobs']
  LOOP
    EXECUTE format('CREATE TRIGGER %I_touch BEFORE UPDATE ON %I FOR EACH ROW EXECUTE FUNCTION touch_updated_at()', t, t);
  END LOOP;
  -- RLS on, no policies: deny by default for any non-backend role.
  FOREACH t IN ARRAY ARRAY['organizations','memberships','invitations','projects','project_memberships',
    'environments','services','connections','credential_versions','connection_checks','repository_bindings',
    'repository_service_mappings','path_mappings','log_scope_entries','project_drafts','jobs','outbox',
    'webhook_deliveries','audit_events']
  LOOP
    EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
  END LOOP;
END $$;
