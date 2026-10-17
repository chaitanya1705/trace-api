// Evidence graph schema v2 (ontology 1.1). Full-text index behind the graph explorer's search box: one index over the
// human-readable caption fields of the labels a person looks for. Like every index it is a performance aid; the explorer
// still filters every result on project_id.

CREATE FULLTEXT INDEX entity_caption IF NOT EXISTS
FOR (n:Project|Environment|Service|Resource|SourceConnection|Repository|Revision|SourceFile|CodeSymbol|CodeCommunity|SchemaEntity|Deployment|Change|Ticket|InvestigationRun|Hypothesis|Finding|Claim|EvidenceGap|PullRequest|LogEvent|RequestSpan)
ON EACH [n.name, n.title, n.qualified_name, n.path, n.commit_sha, n.provider_repository_id, n.provider_id, n.display_id, n.statement, n.summary, n.text, n.message, n.operation, n.description, n.label];

CREATE INDEX source_connection IF NOT EXISTS FOR (n:SourceConnection) ON (n.project_id, n.connection_id);
