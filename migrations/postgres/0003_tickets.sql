-- Tickets are reports, not proven observations (ontology). Ticket status is separate from investigation runs and
-- findings: a ticket is open/resolved/closed; "investigating" etc. are derived from runs later.
CREATE TABLE tickets (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  number bigint GENERATED ALWAYS AS IDENTITY,             -- human id: TKT-<number>
  organization_id uuid NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
  project_id uuid REFERENCES projects(id) ON DELETE SET NULL,   -- NULL = "Need a project"
  source text NOT NULL CHECK (source IN ('manual','api','pulselab','webhook')),
  external_id text,                                        -- dedupe key within (organization, source)
  title text NOT NULL CHECK (length(btrim(title)) BETWEEN 1 AND 300),
  description text NOT NULL DEFAULT '',
  severity text CHECK (severity IN ('SEV-1','SEV-2','SEV-3','SEV-4')),   -- reported; NULL = unrated
  environment_key text,                                    -- as reported; validated against the project at Investigate
  service_hint text,
  owner_user_id uuid,
  status text NOT NULL DEFAULT 'open' CHECK (status IN ('open','resolved','closed')),
  source_created_at timestamptz,
  window_start timestamptz,
  window_end timestamptz,
  content_hash text NOT NULL,
  revision_number integer NOT NULL DEFAULT 1,
  assignment_version integer NOT NULL DEFAULT 0,
  evidence_summary jsonb NOT NULL DEFAULT '{}'::jsonb,    -- counts/gaps from the export; raw evidence lives in payload
  payload jsonb,                                           -- redacted, size-capped original export
  evidence_sync text NOT NULL DEFAULT 'not_required'
    CHECK (evidence_sync IN ('not_required','pending','synced','failed')),   -- ticket evidence -> Neo4j (T-009/T-010)
  created_by uuid,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  version integer NOT NULL DEFAULT 1
);
CREATE UNIQUE INDEX tickets_dedupe ON tickets(organization_id, source, external_id) WHERE external_id IS NOT NULL;
CREATE INDEX tickets_org_status ON tickets(organization_id, status, created_at DESC);
CREATE INDEX tickets_project ON tickets(project_id) WHERE project_id IS NOT NULL;

CREATE TABLE ticket_revisions (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  ticket_id uuid NOT NULL REFERENCES tickets(id) ON DELETE CASCADE,
  revision_number integer NOT NULL,
  content_hash text NOT NULL,
  fields jsonb NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (ticket_id, revision_number)
);

CREATE TRIGGER tickets_touch BEFORE UPDATE ON tickets FOR EACH ROW EXECUTE FUNCTION touch_updated_at();
ALTER TABLE tickets ENABLE ROW LEVEL SECURITY;
ALTER TABLE ticket_revisions ENABLE ROW LEVEL SECURITY;
