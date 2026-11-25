-- Candidate manifest v2 (ADR 026 D2, D8, D9). Two insert-only tables and the approval binding triggers; nothing in
-- runs, tasks, task_attempts or approval_requests changes shape, and a run without run_repo_bindings rows (every
-- single-repository run) keeps the legacy approval path.
--
-- candidate_manifests: the index of v2 manifests. The bytes live in the immutable artifacts table; a row may only
-- point at a v2 candidate_manifest artifact of the same run whose content address IS the manifest hash, for a run
-- that has repository bindings with the same bindings_sha256. seq numbers a run's manifests 1, 2, 3, ...
CREATE TABLE candidate_manifests (
  manifest_sha256 TEXT PRIMARY KEY CHECK (length(manifest_sha256) = 64 AND manifest_sha256 NOT GLOB '*[^0-9a-f]*'),
  run_id TEXT NOT NULL REFERENCES runs(id),
  artifact_id TEXT NOT NULL UNIQUE REFERENCES artifacts(id),
  bindings_sha256 TEXT NOT NULL CHECK (length(bindings_sha256) = 64 AND bindings_sha256 NOT GLOB '*[^0-9a-f]*'),
  version INTEGER NOT NULL CHECK (version = 2),
  seq INTEGER NOT NULL CHECK (seq >= 1),
  created_at TEXT NOT NULL,
  UNIQUE (run_id, seq)
);
CREATE TRIGGER candidate_manifests_no_update BEFORE UPDATE ON candidate_manifests
BEGIN SELECT RAISE(ABORT, 'candidate manifests are insert-only'); END;
CREATE TRIGGER candidate_manifests_no_delete BEFORE DELETE ON candidate_manifests
BEGIN SELECT RAISE(ABORT, 'candidate manifests are insert-only'); END;
-- REPLACE would bypass the delete trigger when recursive_triggers is off.
CREATE TRIGGER candidate_manifests_no_replace BEFORE INSERT ON candidate_manifests
WHEN EXISTS (SELECT 1 FROM candidate_manifests WHERE manifest_sha256 = NEW.manifest_sha256
             OR artifact_id = NEW.artifact_id OR (run_id = NEW.run_id AND seq = NEW.seq))
BEGIN SELECT RAISE(ABORT, 'candidate manifests are insert-only'); END;
CREATE TRIGGER candidate_manifests_valid BEFORE INSERT ON candidate_manifests
WHEN NOT EXISTS (SELECT 1 FROM artifacts a WHERE a.id = NEW.artifact_id AND a.sha256 = NEW.manifest_sha256
                 AND json_valid(a.provenance_json)
                 AND json_extract(a.provenance_json, '$.kind') = 'candidate_manifest'
                 AND json_extract(a.provenance_json, '$.v') = 2
                 AND json_extract(a.provenance_json, '$.run_id') = NEW.run_id
                 AND json_extract(a.provenance_json, '$.manifest_sha256') = NEW.manifest_sha256)
  OR NOT EXISTS (SELECT 1 FROM run_repo_bindings b WHERE b.run_id = NEW.run_id)
  OR EXISTS (SELECT 1 FROM run_repo_bindings b WHERE b.run_id = NEW.run_id AND b.bindings_sha256 <> NEW.bindings_sha256)
  OR NEW.seq <> 1 + COALESCE((SELECT max(seq) FROM candidate_manifests WHERE run_id = NEW.run_id), 0)
BEGIN SELECT RAISE(ABORT, 'invalid candidate manifest'); END;
-- Baseline moved => the run is done producing manifests (owner decision): nothing is rebased, a new run starts.
-- Exactly: refused once any manifest of the run has a 'baseline_moved' row. A run with no manifest yet has nothing
-- to record; the builder's D2 check refuses it while the ref differs from the pinned SHA.
CREATE TRIGGER candidate_manifests_baseline_moved BEFORE INSERT ON candidate_manifests
WHEN EXISTS (SELECT 1 FROM candidate_manifest_invalidations i JOIN candidate_manifests m
             ON m.manifest_sha256 = i.manifest_sha256 WHERE m.run_id = NEW.run_id AND i.reason = 'baseline_moved')
BEGIN SELECT RAISE(ABORT, 'baseline_moved: the run''s baselines moved; start a new run'); END;

-- candidate_manifest_invalidations: a manifest with any row here can never again be approved or deployed.
CREATE TABLE candidate_manifest_invalidations (
  seq INTEGER PRIMARY KEY AUTOINCREMENT,
  manifest_sha256 TEXT NOT NULL REFERENCES candidate_manifests(manifest_sha256),
  reason TEXT NOT NULL CHECK (reason IN ('baseline_moved', 'candidate_invalid', 'operator')),
  actor TEXT NOT NULL CHECK (length(actor) BETWEEN 1 AND 256),
  at TEXT NOT NULL,
  UNIQUE (manifest_sha256, reason)
);
CREATE TRIGGER candidate_manifest_invalidations_no_update BEFORE UPDATE ON candidate_manifest_invalidations
BEGIN SELECT RAISE(ABORT, 'candidate manifest invalidations are insert-only'); END;
CREATE TRIGGER candidate_manifest_invalidations_no_delete BEFORE DELETE ON candidate_manifest_invalidations
BEGIN SELECT RAISE(ABORT, 'candidate manifest invalidations are insert-only'); END;
CREATE TRIGGER candidate_manifest_invalidations_no_replace BEFORE INSERT ON candidate_manifest_invalidations
WHEN EXISTS (SELECT 1 FROM candidate_manifest_invalidations WHERE seq = NEW.seq
             OR (manifest_sha256 = NEW.manifest_sha256 AND reason = NEW.reason))
BEGIN SELECT RAISE(ABORT, 'candidate manifest invalidations are insert-only'); END;

-- Approval binding for multi-repository runs (ADR 026 D9). In a run with repository bindings every approval request
-- must bind one of exactly two things:
--  * a live candidate manifest of the same run (a candidate_manifests row with no invalidation row) - the only form
--    a 'deploy' approval may take; or
--  * the agent_run_multi spawn gate: the approval_step (operation, target, policy_version, candidate_sha256 = the
--    full spec hash, which covers bindings_sha256) recorded for a multi-repository task in the run's
--    agent.tasks_configured event. Allowlisted: only operation 'agent.run' (MULTI_REPO_SPAWN_OPERATION in
--    synapse.persistence.agent_tasks, which also refuses any other operation in a multi-repository approval_step;
--    a test keeps the two in step), so no deploy-like name ('DEPLOY', 'deploy:DEV', ...) can use this branch.
CREATE TRIGGER approval_multi_repo_binding BEFORE INSERT ON approval_requests
WHEN EXISTS (SELECT 1 FROM run_repo_bindings WHERE run_id = NEW.run_id)
 AND NOT (
   (EXISTS (SELECT 1 FROM candidate_manifests m WHERE m.manifest_sha256 = NEW.candidate_manifest_sha256
            AND m.run_id = NEW.run_id)
    AND NOT EXISTS (SELECT 1 FROM candidate_manifest_invalidations i
                    WHERE i.manifest_sha256 = NEW.candidate_manifest_sha256))
   OR (NEW.operation = 'agent.run'
       AND EXISTS (SELECT 1 FROM events e, json_each(e.payload_json, '$.tasks') t
                   WHERE e.run_id = NEW.run_id AND e.type = 'agent.tasks_configured' AND json_valid(e.payload_json)
                   AND json_type(t.value, '$.repos') = 'array'
                   AND json_extract(t.value, '$.approval_step.candidate_sha256') = NEW.candidate_manifest_sha256
                   AND json_extract(t.value, '$.approval_step.operation') = NEW.operation
                   AND json_extract(t.value, '$.approval_step.target') = NEW.target
                   AND json_extract(t.value, '$.approval_step.policy_version') = NEW.policy_version)))
BEGIN SELECT RAISE(ABORT, 'multi-repository approval must bind a live candidate manifest of the run'); END;
-- Decision backstop: an approval bound to an invalidated manifest can never become approved. Matched by manifest hash
-- only (not joined on the approval's run), so no run_id on the approval row can hide an invalidation.
CREATE TRIGGER approval_manifest_invalidated BEFORE UPDATE OF state ON approval_requests
WHEN NEW.state = 'approved'
 AND EXISTS (SELECT 1 FROM candidate_manifest_invalidations i WHERE i.manifest_sha256 = OLD.candidate_manifest_sha256)
BEGIN SELECT RAISE(ABORT, 'manifest_invalidated'); END;
