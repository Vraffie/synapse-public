-- Multi-repository runs, first slice (ADR 026 D1, D2, D4, D6). Four insert-only/append-only tables; nothing in runs, tasks or
-- task_attempts changes, and a run without rows here (every single-repository run) keeps the legacy path.
--
-- run_repo_bindings: the registry entries a run uses, snapshotted with the baseline ref pinned to a commit SHA in the
-- transaction that starts the run. Every attempt cuts its worktrees from baseline_sha, never from the ref again.
CREATE TABLE run_repo_bindings (
  run_id TEXT NOT NULL REFERENCES runs(id),
  repo_id TEXT NOT NULL CHECK (length(repo_id) BETWEEN 1 AND 128),
  path TEXT NOT NULL CHECK (substr(path, 1, 1) = '/'),
  baseline_ref TEXT NOT NULL CHECK (substr(baseline_ref, 1, 11) = 'refs/heads/'),
  baseline_sha TEXT NOT NULL CHECK (length(baseline_sha) IN (40, 64) AND baseline_sha NOT GLOB '*[^0-9a-f]*'),
  writer_task_id TEXT REFERENCES tasks(id),
  protected_paths_json TEXT NOT NULL CHECK (json_valid(protected_paths_json) AND json_type(protected_paths_json) = 'array'),
  entry_sha256 TEXT NOT NULL CHECK (length(entry_sha256) = 64),
  bindings_sha256 TEXT NOT NULL CHECK (length(bindings_sha256) = 64),
  bound_at TEXT NOT NULL,
  PRIMARY KEY (run_id, repo_id)
);
CREATE TRIGGER run_repo_bindings_no_update BEFORE UPDATE ON run_repo_bindings
BEGIN SELECT RAISE(ABORT, 'run repo bindings are insert-only'); END;
CREATE TRIGGER run_repo_bindings_no_delete BEFORE DELETE ON run_repo_bindings
BEGIN SELECT RAISE(ABORT, 'run repo bindings are insert-only'); END;
-- REPLACE would bypass the delete trigger when recursive_triggers is off.
CREATE TRIGGER run_repo_bindings_no_replace BEFORE INSERT ON run_repo_bindings
WHEN EXISTS (SELECT 1 FROM run_repo_bindings WHERE run_id = NEW.run_id AND repo_id = NEW.repo_id)
BEGIN SELECT RAISE(ABORT, 'run repo bindings are insert-only'); END;

-- attempt_worktrees: one row per (attempt, repository) worktree, written after all of the attempt's worktrees exist
-- and before the attempt's single spawn. A read-access worktree is detached (branch is NULL).
CREATE TABLE attempt_worktrees (
  attempt_id TEXT NOT NULL REFERENCES task_attempts(id),
  repo_id TEXT NOT NULL CHECK (length(repo_id) BETWEEN 1 AND 128),
  access TEXT NOT NULL CHECK (access IN ('write', 'read')),
  path TEXT NOT NULL CHECK (substr(path, 1, 1) = '/'),
  branch TEXT CHECK ((access = 'write' AND substr(branch, 1, 8) = 'synapse/') OR (access = 'read' AND branch IS NULL)),
  baseline_sha TEXT NOT NULL CHECK (length(baseline_sha) IN (40, 64) AND baseline_sha NOT GLOB '*[^0-9a-f]*'),
  created_at TEXT NOT NULL,
  PRIMARY KEY (attempt_id, repo_id)
);
CREATE TRIGGER attempt_worktrees_no_update BEFORE UPDATE ON attempt_worktrees
BEGIN SELECT RAISE(ABORT, 'attempt worktrees are insert-only'); END;
CREATE TRIGGER attempt_worktrees_no_delete BEFORE DELETE ON attempt_worktrees
BEGIN SELECT RAISE(ABORT, 'attempt worktrees are insert-only'); END;
CREATE TRIGGER attempt_worktrees_no_replace BEFORE INSERT ON attempt_worktrees
WHEN EXISTS (SELECT 1 FROM attempt_worktrees WHERE attempt_id = NEW.attempt_id AND repo_id = NEW.repo_id)
BEGIN SELECT RAISE(ABORT, 'attempt worktrees are insert-only'); END;

-- attempt_worktree_removals: every removal of an attempt worktree (never-spawned cleanup, operator removal).
CREATE TABLE attempt_worktree_removals (
  seq INTEGER PRIMARY KEY AUTOINCREMENT,
  attempt_id TEXT NOT NULL REFERENCES task_attempts(id),
  repo_id TEXT NOT NULL CHECK (length(repo_id) BETWEEN 1 AND 128),
  reason TEXT NOT NULL CHECK (length(reason) BETWEEN 1 AND 64),
  forced INTEGER NOT NULL CHECK (forced IN (0, 1)),
  removed_at TEXT NOT NULL
);
CREATE TRIGGER attempt_worktree_removals_no_update BEFORE UPDATE ON attempt_worktree_removals
BEGIN SELECT RAISE(ABORT, 'attempt worktree removals are insert-only'); END;
CREATE TRIGGER attempt_worktree_removals_no_delete BEFORE DELETE ON attempt_worktree_removals
BEGIN SELECT RAISE(ABORT, 'attempt worktree removals are insert-only'); END;
CREATE TRIGGER attempt_worktree_removals_no_replace BEFORE INSERT ON attempt_worktree_removals
WHEN NEW.seq IS NOT NULL AND EXISTS (SELECT 1 FROM attempt_worktree_removals WHERE seq = NEW.seq)
BEGIN SELECT RAISE(ABORT, 'attempt worktree removals are insert-only'); END;

-- attempt_repo_commits: the host-side commit phase, append-only. An 'intent' row (with the branch HEAD before the
-- commit) precedes every git commit; exactly one result row follows it: done (candidate_sha), unchanged (nothing to
-- commit: candidate_sha = baseline = head_before, no git commit), failed (reason), or, from startup recovery only,
-- not_done / unknown. One intent per (attempt, repository): a failed or unresolved
-- commit is never re-run on the same tree; recovery is a new attempt.
CREATE TABLE attempt_repo_commits (
  seq INTEGER PRIMARY KEY AUTOINCREMENT,
  attempt_id TEXT NOT NULL REFERENCES task_attempts(id),
  repo_id TEXT NOT NULL CHECK (length(repo_id) BETWEEN 1 AND 128),
  state TEXT NOT NULL CHECK (state IN ('intent', 'done', 'unchanged', 'failed', 'not_done', 'unknown')),
  head_before TEXT CHECK (head_before IS NULL OR (length(head_before) IN (40, 64) AND head_before NOT GLOB '*[^0-9a-f]*')),
  candidate_sha TEXT CHECK (candidate_sha IS NULL OR (length(candidate_sha) IN (40, 64) AND candidate_sha NOT GLOB '*[^0-9a-f]*')),
  reason TEXT CHECK (reason IS NULL OR length(reason) BETWEEN 1 AND 64),
  paths INTEGER CHECK (paths IS NULL OR paths >= 0),
  at TEXT NOT NULL,
  CHECK ((state = 'intent' AND head_before IS NOT NULL AND candidate_sha IS NULL AND reason IS NULL)
      OR (state = 'done' AND candidate_sha IS NOT NULL AND reason IS NULL)
      OR (state = 'unchanged' AND candidate_sha IS head_before AND reason IS NULL)
      OR (state IN ('failed', 'unknown') AND candidate_sha IS NULL AND reason IS NOT NULL)
      OR (state = 'not_done' AND candidate_sha IS NULL))
);
CREATE TRIGGER attempt_repo_commits_no_update BEFORE UPDATE ON attempt_repo_commits
BEGIN SELECT RAISE(ABORT, 'attempt repo commits are append-only'); END;
CREATE TRIGGER attempt_repo_commits_no_delete BEFORE DELETE ON attempt_repo_commits
BEGIN SELECT RAISE(ABORT, 'attempt repo commits are append-only'); END;
CREATE TRIGGER attempt_repo_commits_one_intent BEFORE INSERT ON attempt_repo_commits
WHEN NEW.state = 'intent'
 AND EXISTS (SELECT 1 FROM attempt_repo_commits WHERE attempt_id = NEW.attempt_id AND repo_id = NEW.repo_id)
BEGIN SELECT RAISE(ABORT, 'attempt repo commit intent already recorded'); END;
CREATE TRIGGER attempt_repo_commits_result_after_intent BEFORE INSERT ON attempt_repo_commits
WHEN NEW.state <> 'intent'
 AND NOT EXISTS (SELECT 1 FROM attempt_repo_commits WHERE attempt_id = NEW.attempt_id AND repo_id = NEW.repo_id
                 AND state = 'intent' AND head_before IS NEW.head_before)
BEGIN SELECT RAISE(ABORT, 'attempt repo commit result needs its intent'); END;
CREATE TRIGGER attempt_repo_commits_one_result BEFORE INSERT ON attempt_repo_commits
WHEN NEW.state <> 'intent'
 AND EXISTS (SELECT 1 FROM attempt_repo_commits WHERE attempt_id = NEW.attempt_id AND repo_id = NEW.repo_id
             AND state <> 'intent')
BEGIN SELECT RAISE(ABORT, 'attempt repo commit result already recorded'); END;
-- Explicit seq values would let a writer slot a row in front of the intent; the sequence is the database's.
CREATE TRIGGER attempt_repo_commits_no_replace BEFORE INSERT ON attempt_repo_commits
WHEN NEW.seq IS NOT NULL AND EXISTS (SELECT 1 FROM attempt_repo_commits WHERE seq = NEW.seq)
BEGIN SELECT RAISE(ABORT, 'attempt repo commits are append-only'); END;
