-- Changes and patch proposals (T-016/T-017). A change is a fix that an investigation proposes; each edit is a new immutable
-- patch version identified by the hash of its diff and the exact base commit. Approvals bind to a hash, so editing a change
-- invalidates earlier approvals. Nothing here applies a patch anywhere.
CREATE TABLE change_proposals (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  number bigint GENERATED ALWAYS AS IDENTITY,                                  -- human id: CHG-<number>
  organization_id uuid NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
  project_id uuid NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  ticket_id uuid REFERENCES tickets(id) ON DELETE SET NULL,
  run_id uuid NOT NULL,                                                         -- investigation run (control id; the run lives in the graph)
  title text NOT NULL CHECK (length(btrim(title)) BETWEEN 1 AND 300),
  rationale text NOT NULL DEFAULT '',
  risk text NOT NULL CHECK (risk IN ('low','medium','high')),
  environment_key text,
  status text NOT NULL DEFAULT 'proposed' CHECK (status IN ('proposed','approved','rejected','withdrawn','draft_pr_opened','merged')),
  current_version integer NOT NULL DEFAULT 1,
  created_by uuid,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  version integer NOT NULL DEFAULT 1
);
CREATE INDEX change_proposals_project ON change_proposals(project_id, updated_at DESC);
CREATE INDEX change_proposals_org_status ON change_proposals(organization_id, status);

CREATE TABLE patch_versions (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  change_id uuid NOT NULL REFERENCES change_proposals(id) ON DELETE CASCADE,
  version integer NOT NULL,
  base_commit text NOT NULL,                                                    -- exact commit the diff applies to
  base_revision_id text NOT NULL,                                               -- graph Revision node
  files jsonb NOT NULL,                                                         -- [{path, additions, deletions, diff, before_hash}]
  diff text NOT NULL,
  content_hash text NOT NULL,                                                   -- sha256(base_commit + diff)
  summary text NOT NULL DEFAULT '',
  risk_reason text NOT NULL DEFAULT '',
  tests jsonb NOT NULL DEFAULT '[]'::jsonb,                                     -- suggested tests: PROPOSED, never executed here
  rollback text NOT NULL DEFAULT '',
  checks jsonb NOT NULL DEFAULT '[]'::jsonb,                                    -- static checks that actually ran (e.g. Python syntax)
  test_status text NOT NULL DEFAULT 'not_run' CHECK (test_status IN ('not_run','passed','failed')),
  instruction text,                                                             -- the revision request that produced this version
  graph_patch_id text,
  created_by uuid,
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (change_id, version)
);

CREATE TABLE change_approvals (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  change_id uuid NOT NULL REFERENCES change_proposals(id) ON DELETE CASCADE,
  patch_version integer NOT NULL,
  patch_hash text NOT NULL,
  actor_user_id uuid NOT NULL,
  decision text NOT NULL CHECK (decision IN ('approve','reject')),
  reason text,
  created_at timestamptz NOT NULL DEFAULT now(),
  revoked_at timestamptz,
  UNIQUE (change_id, patch_hash, actor_user_id)
);

CREATE TABLE change_policies (
  project_id uuid PRIMARY KEY REFERENCES projects(id) ON DELETE CASCADE,
  required_approvals integer NOT NULL DEFAULT 1 CHECK (required_approvals BETWEEN 1 AND 5),
  author_can_approve boolean NOT NULL DEFAULT false,                            -- solo projects only; off means an independent approver is required
  updated_by uuid,
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TRIGGER change_proposals_touch BEFORE UPDATE ON change_proposals FOR EACH ROW EXECUTE FUNCTION touch_updated_at();
ALTER TABLE change_proposals ENABLE ROW LEVEL SECURITY;
ALTER TABLE patch_versions ENABLE ROW LEVEL SECURITY;
ALTER TABLE change_approvals ENABLE ROW LEVEL SECURITY;
ALTER TABLE change_policies ENABLE ROW LEVEL SECURITY;
