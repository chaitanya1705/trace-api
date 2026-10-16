-- The wizard connects Vercel/GitHub before the project exists, so a connection may belong to a draft first and is
-- attached to the project on activation. Exactly one owner (project XOR draft) at any time.
ALTER TABLE connections ALTER COLUMN project_id DROP NOT NULL;
ALTER TABLE connections ADD COLUMN draft_id uuid REFERENCES project_drafts(id) ON DELETE CASCADE;
ALTER TABLE connections ADD CONSTRAINT connections_one_owner CHECK ((project_id IS NULL) <> (draft_id IS NULL));
CREATE UNIQUE INDEX connections_draft_provider_kind ON connections(draft_id, provider, kind) WHERE draft_id IS NOT NULL;

-- Pending credential versions (rotation) must be verifiable before activation; only one active per connection.
CREATE UNIQUE INDEX credential_one_active ON credential_versions(connection_id) WHERE status = 'active';

-- Per-connection secret for verifying inbound provider callbacks (Vercel drain signatures). Stored sealed like any
-- credential; this column only records which credential version holds it.
ALTER TABLE connections ADD COLUMN webhook_credential_version integer;
