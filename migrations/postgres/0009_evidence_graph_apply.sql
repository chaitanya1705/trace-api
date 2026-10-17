-- Evidence -> graph application (T-009, T-006; architecture A2 and A10).
-- evidence_inbox is the durable receipt. A row is only 'applied' after the Neo4j write that carries it has committed:
--   pending -> claimed (lease) -> applied            normal path
--   claimed with an expired lease is picked up again (a crash between the graph commit and the acknowledgement is safe,
--   because graph ids are deterministic and evidence nodes are immutable)
--   pending (retry with backoff) ... -> failed       after repeated failure or a row the ontology refuses; kept, visible, retryable by hand
ALTER TABLE evidence_inbox DROP CONSTRAINT IF EXISTS evidence_inbox_graph_status_check;
UPDATE evidence_inbox SET graph_status = 'applied' WHERE graph_status = 'synced';
ALTER TABLE evidence_inbox ADD CONSTRAINT evidence_inbox_graph_status_check CHECK (graph_status IN ('pending','claimed','applied','failed'));
ALTER TABLE evidence_inbox
  ADD COLUMN graph_attempts integer NOT NULL DEFAULT 0,
  ADD COLUMN graph_claim_owner text,
  ADD COLUMN graph_lease_expires_at timestamptz,
  ADD COLUMN graph_next_attempt_at timestamptz NOT NULL DEFAULT now(),
  ADD COLUMN graph_applied_at timestamptz,
  ADD COLUMN graph_error text,                       -- error class only, never a value
  ADD COLUMN graph_scope jsonb,                      -- mapping decided once at first claim (service, deployment) so a retry writes identical content
  ADD COLUMN batch_id uuid;                          -- the ingestion batch that delivered the row

DROP INDEX IF EXISTS evidence_inbox_pending;
CREATE INDEX evidence_inbox_graph_work ON evidence_inbox(project_id, graph_next_attempt_at) WHERE graph_status IN ('pending','claimed');
CREATE INDEX evidence_inbox_batch ON evidence_inbox(batch_id) WHERE batch_id IS NOT NULL;
CREATE INDEX evidence_inbox_graph_ack ON evidence_inbox(project_id, ingested_at) WHERE graph_status <> 'applied';

-- Intake accounting per request. 'reason' is set when the whole request was refused after authentication (oversize body,
-- malformed payload, too many events); those, and per-record rejections, truncation and unmapped scope, become EvidenceGap
-- nodes once the applier has written them (gaps_applied_at).
ALTER TABLE ingestion_batches
  ADD COLUMN via text,                               -- drain | app_push | capture
  ADD COLUMN reason text,
  ADD COLUMN truncated integer NOT NULL DEFAULT 0,
  ADD COLUMN unmapped integer NOT NULL DEFAULT 0,    -- stored, but environment could not be mapped (left unknown)
  ADD COLUMN body_bytes integer,
  ADD COLUMN gaps_applied_at timestamptz;
CREATE INDEX ingestion_batches_gaps ON ingestion_batches(project_id, created_at) WHERE gaps_applied_at IS NULL;

-- Inbox rows are written before their batch row inside one transaction.
ALTER TABLE evidence_inbox ADD CONSTRAINT evidence_inbox_batch_fk FOREIGN KEY (batch_id) REFERENCES ingestion_batches(id) ON DELETE SET NULL DEFERRABLE INITIALLY DEFERRED;

-- The outbox gets worker leases so a crashed applier's rows are claimable again (the reconciler).
ALTER TABLE outbox
  ADD COLUMN lease_owner text,
  ADD COLUMN lease_expires_at timestamptz;
CREATE INDEX outbox_due ON outbox(next_attempt_at) WHERE status = 'pending';
