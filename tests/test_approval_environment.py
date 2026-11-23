from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3

import pytest

from synapse.domain.approvals import ApprovalChange, ApprovalState
from synapse.domain.contracts import ApprovalId
from synapse.persistence.approval_binding import ApprovalNotGranted, DenialReason, verify_approval_for_execution
from synapse.persistence.approval_environment import (
    bind_approval_environment, ensure_schema, environment_bound_hash, validate_environment,
    verify_approval_for_environment as verify)
from synapse.persistence.approval_store import write_approval_request, write_approval_transition
from synapse.persistence.migrations import apply_migrations, open_database
from synapse.persistence.side_effect_ledger import (
    Entry, StepKey, ensure_schema as ledger_schema, get_state, record_intent, recover_side_effects, resolve_unknown)
from synapse.persistence.state_store import StateConflict
import ast

ROOT = Path(__file__).resolve().parents[1]
D = "a" * 64
ARGS = dict(approval_id="ap", run_id="run", operation="deploy", target="repo", candidate_sha256=D, policy_version="1")
TWO = frozenset({"STAGING", "STAGING3"})


@pytest.fixture
def db(tmp_path):
    with closing(open_database(tmp_path / "db")) as c:
        apply_migrations(c, ROOT / "migrations")
        ensure_schema(c)
        c.execute("INSERT INTO workflow_versions VALUES('wf','1','{}','hash','now')")
        c.execute("INSERT INTO runs VALUES('run','wf','1','created',0,'now','now')")
        yield c


def make(db, approval_id="ap", env="STAGING", decide=True):
    db.execute("BEGIN IMMEDIATE")
    write_approval_request(db, approval_id=approval_id, run_id="run", operation="deploy", target="repo",
                           candidate_sha256=D, policy_version="1",
                           expires_at=(datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
                           requester_subject="agent-1", requester_kind="agent")
    if env:
        bind_approval_environment(db, approval_id=approval_id, environment=env)
    db.execute("COMMIT")
    if decide:
        db.execute("BEGIN IMMEDIATE")
        write_approval_transition(db, ApprovalChange(ApprovalId(approval_id), ApprovalState.REQUESTED,
                                                     ApprovalState.APPROVED, 0),
                                  principal="human-1", candidate_sha256=D, policy_version="1", environment=env)
        db.execute("COMMIT")


def denied(db, reason, **over):
    with pytest.raises(ApprovalNotGranted) as e:
        verify(db, **{"environment": "STAGING", **ARGS, **over})
    assert e.value.reason == reason


def test_match_succeeds(db):
    make(db)
    assert verify(db, environment="STAGING", **ARGS).approval_id == "ap"
    assert not db.in_transaction


def test_mismatched_environment_denied(db, monkeypatch):
    monkeypatch.setattr("synapse.persistence.approval_environment.ALLOWED_ENVIRONMENTS", TWO)
    make(db)
    denied(db, DenialReason.BINDING_MISMATCH, environment="STAGING3")


def test_missing_or_unknown_requested_environment_denied(db):
    make(db)
    for env in (None, "", "staging", "PROD", 5):
        denied(db, DenialReason.MALFORMED, environment=env)


def test_approval_without_binding_denied_but_plain_check_unchanged(db):
    make(db, env=None)
    denied(db, DenialReason.BINDING_MISMATCH)
    assert verify_approval_for_execution(db, **ARGS).approval_id == "ap"  # legacy approval behaviour intact


def test_missing_table_fails_closed(tmp_path):
    with closing(open_database(tmp_path / "x")) as c:
        apply_migrations(c, ROOT / "migrations")
        c.execute("INSERT INTO workflow_versions VALUES('wf','1','{}','hash','now')")
        c.execute("INSERT INTO runs VALUES('run','wf','1','created',0,'now','now')")
        make(c, env=None)
        denied(c, DenialReason.BINDING_MISMATCH)


def test_replay_across_environments(db, monkeypatch):
    monkeypatch.setattr("synapse.persistence.approval_environment.ALLOWED_ENVIRONMENTS", TWO)
    make(db, "ap", "STAGING")
    make(db, "ap3", "STAGING3")
    assert verify(db, environment="STAGING", **ARGS)
    denied(db, DenialReason.BINDING_MISMATCH, environment="STAGING3")
    assert verify(db, environment="STAGING3", **{**ARGS, "approval_id": "ap3"})
    denied(db, DenialReason.BINDING_MISMATCH, approval_id="ap3", environment="STAGING")


def test_binding_immutable_and_only_before_decision(db):
    make(db)
    for sql in ("UPDATE approval_environment_binding SET environment='X'", "DELETE FROM approval_environment_binding"):
        with pytest.raises(sqlite3.DatabaseError):
            db.execute(sql)
    make(db, "ap2", env=None, decide=True)
    db.execute("BEGIN IMMEDIATE")
    with pytest.raises(ValueError):
        bind_approval_environment(db, approval_id="ap2", environment="STAGING")
    with pytest.raises(ValueError):
        bind_approval_environment(db, approval_id="ap2", environment="PROD")
    db.execute("ROLLBACK")


def test_tampered_hash_is_malformed(db):
    make(db)
    db.execute("DROP TRIGGER approval_environment_binding_no_update")
    db.execute("UPDATE approval_environment_binding SET bound_sha256=?", ("0" * 64,))
    denied(db, DenialReason.MALFORMED)


def test_hash_stable_and_environment_sensitive():
    assert environment_bound_hash(D, "STAGING") == environment_bound_hash(D, "STAGING")
    assert environment_bound_hash(D, "STAGING") != environment_bound_hash("b" * 64, "STAGING")
    assert environment_bound_hash(D, "STAGING") != D
    with pytest.raises(ValueError):
        environment_bound_hash(D, "PROD")
    assert validate_environment("STAGING") == "STAGING"


def test_ledger_key_includes_environment(tmp_path, monkeypatch):
    monkeypatch.setattr("synapse.persistence.approval_environment.ALLOWED_ENVIRONMENTS", TWO)
    with closing(open_database(tmp_path / "l")) as c:
        ledger_schema(c)
        assert record_intent(c, StepKey("t", 1, D, "deploy", "STAGING")) is Entry.PROCEED
        assert record_intent(c, StepKey("t", 1, D, "deploy", "STAGING")) is Entry.IN_FLIGHT
        assert record_intent(c, StepKey("t", 1, D, "deploy", "STAGING3")) is Entry.PROCEED
        assert record_intent(c, StepKey("t", 1, D, "deploy")) is Entry.PROCEED  # legacy no-environment key
        with pytest.raises(ValueError):
            StepKey("t", 1, D, "deploy", "PROD")


def _undecided(db, approval_id="ap", env=None):
    make(db, approval_id, env=env, decide=False)


def test_plain_sql_insert_rejected_for_decided_approval(db):
    make(db, env=None)  # approved
    with pytest.raises(sqlite3.DatabaseError, match="undecided"):
        db.execute("INSERT INTO approval_environment_binding VALUES('ap','STAGING',?, 'now')", ("0" * 64,))
    with pytest.raises(sqlite3.DatabaseError):  # unknown approval id
        db.execute("INSERT INTO approval_environment_binding VALUES('nope','STAGING',?, 'now')", ("0" * 64,))
    _undecided(db, "ap9")  # an undecided one is accepted at SQL level
    db.execute("INSERT INTO approval_environment_binding VALUES('ap9','STAGING',?, 'now')", ("0" * 64,))


def _decide(db, environment):
    db.execute("BEGIN IMMEDIATE")
    try:
        write_approval_transition(db, ApprovalChange(ApprovalId("ap"), ApprovalState.REQUESTED, ApprovalState.APPROVED, 0),
                                  principal="human-1", candidate_sha256=D, policy_version="1", environment=environment)
        db.execute("COMMIT")
    except BaseException:
        db.execute("ROLLBACK")
        raise


def test_decision_refused_when_environment_differs_or_omitted(db, monkeypatch):
    monkeypatch.setattr("synapse.persistence.approval_environment.ALLOWED_ENVIRONMENTS", TWO)
    make(db, decide=False)  # bound to STAGING after the human "viewed" the request
    for env in ("STAGING3", None, ""):
        with pytest.raises(StateConflict):
            _decide(db, env)
    assert db.execute("SELECT state FROM approval_requests WHERE id='ap'").fetchone()[0] == "requested"
    _decide(db, "STAGING")
    assert verify(db, environment="STAGING", **ARGS)


def test_decision_without_binding_unchanged(db):
    make(db, env=None, decide=False)
    _decide(db, None)
    assert verify_approval_for_execution(db, **ARGS)


OLD_SCHEMA = """
CREATE TABLE side_effect_ledger (
  task_id TEXT NOT NULL, attempt_no INTEGER NOT NULL CHECK (attempt_no >= 1),
  candidate_sha256 TEXT NOT NULL CHECK (length(candidate_sha256) = 64),
  step TEXT NOT NULL,
  state TEXT NOT NULL CHECK (state IN ('intent','done','unknown','not_done')),
  intent_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  resolution_source TEXT CHECK (resolution_source IN ('worker','operator','probe')),
  resolved_by TEXT, evidence TEXT,
  PRIMARY KEY (task_id, attempt_no, candidate_sha256, step)
);
CREATE TABLE side_effect_ledger_transitions (
  seq INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id TEXT NOT NULL, attempt_no INTEGER NOT NULL, candidate_sha256 TEXT NOT NULL, step TEXT NOT NULL,
  from_state TEXT, to_state TEXT NOT NULL, source TEXT NOT NULL, actor TEXT, evidence TEXT, at TEXT NOT NULL
);
CREATE TRIGGER side_effect_transitions_no_update BEFORE UPDATE ON side_effect_ledger_transitions
BEGIN SELECT RAISE(ABORT, 'side-effect transitions are append-only'); END;
CREATE TRIGGER side_effect_transitions_no_delete BEFORE DELETE ON side_effect_ledger_transitions
BEGIN SELECT RAISE(ABORT, 'side-effect transitions are append-only'); END;
"""


def test_ledger_upgrade_from_pr138_shape(tmp_path):
    with closing(open_database(tmp_path / "old")) as c:
        c.executescript(OLD_SCHEMA)
        c.execute("INSERT INTO side_effect_ledger VALUES('t',1,?, 'deploy','unknown','a','b','operator','me','ev')", (D,))
        c.execute("INSERT INTO side_effect_ledger VALUES('t',2,?, 'deploy','done','a','b','worker',NULL,NULL)", (D,))
        c.execute("INSERT INTO side_effect_ledger_transitions(task_id,attempt_no,candidate_sha256,step,from_state,to_state,"
                  "source,actor,evidence,at) VALUES('t',1,?, 'deploy',NULL,'intent','worker',NULL,NULL,'a')", (D,))
        for _ in range(2):  # idempotent
            ledger_schema(c)
        assert not c.in_transaction
        assert "environment" in [r[1] for r in c.execute("PRAGMA table_info(side_effect_ledger)")]
        assert "side_effect_ledger_pre_env" not in [r[0] for r in c.execute("SELECT name FROM sqlite_master")]
        rows = c.execute("SELECT attempt_no,state,environment,resolved_by,evidence FROM side_effect_ledger "
                         "ORDER BY attempt_no").fetchall()
        assert rows == [(1, "unknown", "", "me", "ev"), (2, "done", "", None, None)]
        assert c.execute("SELECT count(*),min(environment) FROM side_effect_ledger_transitions").fetchone() == (1, "")
        with pytest.raises(sqlite3.DatabaseError):  # transitions triggers survived
            c.execute("DELETE FROM side_effect_ledger_transitions")
        assert get_state(c, StepKey("t", 1, D, "deploy")) == "unknown"
        assert record_intent(c, StepKey("t", 1, D, "deploy", "STAGING")) is Entry.PROCEED  # new PK in effect
        assert resolve_unknown(c, StepKey("t", 1, D, "deploy"), outcome="not_done", actor="op") == "not_done"


def test_recovery_and_resolution_after_environment_leaves_allowlist(tmp_path, monkeypatch):
    with closing(open_database(tmp_path / "l")) as c:
        ledger_schema(c)
        key = StepKey("t", 1, D, "deploy", "STAGING")
        assert record_intent(c, key) is Entry.PROCEED
        monkeypatch.setattr("synapse.persistence.approval_environment.ALLOWED_ENVIRONMENTS", frozenset())
        with pytest.raises(ValueError):  # caller-supplied keys are still validated
            StepKey("t", 1, D, "deploy", "STAGING")
        recovered = recover_side_effects(c, no_workers_survive=True)
        assert [k.environment for k in recovered] == ["STAGING"]
        stored = StepKey.from_stored("t", 1, D, "deploy", "STAGING")
        assert get_state(c, stored) == "unknown"
        assert resolve_unknown(c, stored, outcome="not_done", actor="op") == "not_done"
        with pytest.raises(ValueError):  # from_stored still validates shape
            StepKey.from_stored("t", 1, "bad", "deploy", "STAGING")


# Current legitimate users of the plain verifier. Adding a module here is a deliberate, reviewed decision: any
# deploy-class path must call verify_approval_for_environment instead (ADR-022).
PLAIN_VERIFIER_ALLOWLIST = {
    "synapse/persistence/approval_binding.py",      # definition
    "synapse/persistence/approval_environment.py",  # the environment-aware wrapper
    "synapse/orchestration/approval_gates.py",      # non-deploy approval gate resume (legacy, no environment)
}


def test_plain_verifier_not_used_outside_allowlist():
    found = set()
    for path in (ROOT / "src").rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if ((isinstance(node, ast.Name) and node.id == "verify_approval_for_execution")
                    or (isinstance(node, ast.Attribute) and node.attr == "verify_approval_for_execution")
                    or (isinstance(node, ast.alias) and node.name == "verify_approval_for_execution")
                    or (isinstance(node, ast.FunctionDef) and node.name == "verify_approval_for_execution")):
                found.add(path.relative_to(ROOT / "src").as_posix())
    assert found <= PLAIN_VERIFIER_ALLOWLIST, f"new plain-verifier user(s): {sorted(found - PLAIN_VERIFIER_ALLOWLIST)}"
