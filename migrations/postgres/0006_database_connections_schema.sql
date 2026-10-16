-- A project's own application database (Projects > Architecture > Database). POC decision (user): TRACE reads schema METADATA
-- (tables, columns, keys, indexes) from the customer's Postgres through a read-only connection. It never reads row data.
-- Later this view is fed from the graph (code/migration-derived SchemaEntity); the snapshot table is the interim source.
ALTER TABLE connections DROP CONSTRAINT connections_provider_check;
ALTER TABLE connections ADD CONSTRAINT connections_provider_check CHECK (provider IN ('github','vercel','generic','postgres'));
ALTER TABLE connections DROP CONSTRAINT connections_kind_check;
ALTER TABLE connections ADD CONSTRAINT connections_kind_check CHECK (kind IN ('code','hosting','database'));

CREATE TABLE schema_snapshots (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  organization_id uuid NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
  project_id uuid NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  connection_id uuid NOT NULL REFERENCES connections(id) ON DELETE CASCADE,
  captured_at timestamptz NOT NULL DEFAULT now(),
  database_name text,
  server_version text,
  table_count integer NOT NULL,
  column_count integer NOT NULL,
  truncated boolean NOT NULL DEFAULT false,         -- table cap hit: the picture is partial, said so in the UI
  schema jsonb NOT NULL                              -- {tables:[{schema,name,kind,approx_rows,columns,primary_key,foreign_keys,indexes}]}
);
CREATE INDEX schema_snapshots_project ON schema_snapshots(project_id, captured_at DESC);
ALTER TABLE schema_snapshots ENABLE ROW LEVEL SECURITY;
