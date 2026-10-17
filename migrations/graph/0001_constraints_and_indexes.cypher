// Evidence graph schema v1 (ontology 1.0). Every statement is idempotent. Indexes are performance aids, not access
// control: authorization is enforced by the backend, and every query template filters on project_id.

CREATE CONSTRAINT entity_id_unique IF NOT EXISTS FOR (n:Entity) REQUIRE n.id IS UNIQUE;
CREATE CONSTRAINT entity_project_present IF NOT EXISTS FOR (n:Entity) REQUIRE n.project_id IS NOT NULL;

CREATE INDEX entity_project IF NOT EXISTS FOR (n:Entity) ON (n.project_id);

CREATE INDEX evidence_scope_time IF NOT EXISTS FOR (n:EvidenceRecord) ON (n.project_id, n.environment_id, n.event_time);
CREATE INDEX evidence_source_record IF NOT EXISTS FOR (n:EvidenceRecord) ON (n.project_id, n.source_connection_id, n.source_record_key);
CREATE INDEX evidence_request IF NOT EXISTS FOR (n:LogEvent) ON (n.project_id, n.request_id);

CREATE INDEX repository_provider IF NOT EXISTS FOR (n:Repository) ON (n.project_id, n.provider, n.provider_repository_id);
CREATE INDEX revision_commit IF NOT EXISTS FOR (n:Revision) ON (n.project_id, n.repository_id, n.commit_sha);
CREATE INDEX build_revision IF NOT EXISTS FOR (n:GraphBuild) ON (n.project_id, n.revision_id);
CREATE INDEX file_revision_path IF NOT EXISTS FOR (n:SourceFile) ON (n.project_id, n.revision_id, n.path);
CREATE INDEX chunk_file_lines IF NOT EXISTS FOR (n:SourceChunk) ON (n.project_id, n.file_id, n.start_line);
CREATE INDEX symbol_file_name IF NOT EXISTS FOR (n:CodeSymbol) ON (n.project_id, n.file_id, n.qualified_name);
CREATE INDEX symbol_build IF NOT EXISTS FOR (n:CodeSymbol) ON (n.project_id, n.build_id);

CREATE INDEX deployment_scope_time IF NOT EXISTS FOR (n:Deployment) ON (n.project_id, n.service_id, n.environment_id, n.event_time);
CREATE INDEX change_time IF NOT EXISTS FOR (n:Change) ON (n.project_id, n.event_time);
CREATE INDEX service_key IF NOT EXISTS FOR (n:Service) ON (n.project_id, n.service_key);
CREATE INDEX environment_key IF NOT EXISTS FOR (n:Environment) ON (n.project_id, n.environment_key);

CREATE INDEX ticket_control IF NOT EXISTS FOR (n:Ticket) ON (n.project_id, n.control_ticket_id);
CREATE INDEX run_project IF NOT EXISTS FOR (n:InvestigationRun) ON (n.project_id, n.control_run_id);
CREATE INDEX gap_status IF NOT EXISTS FOR (n:EvidenceGap) ON (n.project_id, n.kind, n.status);

CREATE FULLTEXT INDEX source_text IF NOT EXISTS FOR (n:SourceChunk) ON EACH [n.content];
CREATE FULLTEXT INDEX log_text IF NOT EXISTS FOR (n:LogEvent) ON EACH [n.message];
