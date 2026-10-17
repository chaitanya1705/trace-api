-- Evidence can also arrive inside an incident export attached to a ticket (PulseLab's backend-generated diagnostics), with no hosting
-- connection involved. Such rows keep their origin (ticket + provider 'pulselab_export'). Deduplication moves from (connection, key)
-- to (project, key) so the same logical event arriving through two routes (log drain and export) is stored once.
ALTER TABLE evidence_inbox ALTER COLUMN connection_id DROP NOT NULL;
ALTER TABLE evidence_inbox ADD COLUMN ticket_id uuid REFERENCES tickets(id) ON DELETE SET NULL;
ALTER TABLE evidence_inbox ADD CONSTRAINT evidence_inbox_has_origin CHECK (connection_id IS NOT NULL OR ticket_id IS NOT NULL);
ALTER TABLE evidence_inbox DROP CONSTRAINT evidence_inbox_connection_id_source_record_key_key;
CREATE UNIQUE INDEX evidence_inbox_project_key ON evidence_inbox(project_id, source_record_key);
CREATE INDEX evidence_inbox_ticket ON evidence_inbox(ticket_id) WHERE ticket_id IS NOT NULL;
