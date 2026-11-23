"""Audit hash chain (ADR 018): tamper cases, anchors, gate refusal, bounded verification, triggers."""
from contextlib import closing
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3

import pytest

from synapse.domain.approvals import ApprovalChange, ApprovalState
from synapse.domain.contracts import ApprovalId
from synapse.persistence import audit_chain as chain
from synapse.persistence.approval_binding import ApprovalNotGranted, DenialReason, verify_approval_for_execution as verify
from synapse.persistence.approval_store import write_approval_request, write_approval_transition
from synapse.persistence.migrations import apply_migrations, open_database

ROOT = Path(__file__).resolve().parents[1]
D = "a" * 64
ARGS = dict(approval_id="ap", run_id="run", operation="push", target="repo", candidate_sha256=D, policy_version="1")
NOW = datetime.now(timezone.utc).isoformat()


class MemorySink:
    """Stand-in for the journal: records survive any edit of the database."""
    name, strength = "memory", "journal-trusted"

    def __init__(self):
        self.records = []

    def publish(self, record):
        self.records.append(record)

    def read(self):
        return list(self.records)


def audit(db, n=1, action="test.action", resource="r"):
    for i in range(n):
        chain.append_audit(db, "p", action, resource, "allowed", None, "{}", NOW)


def event(db, i):
    db.execute("INSERT INTO events(id,aggregate_type,aggregate_id,type,schema_version,payload_json,occurred_at)"
               " VALUES(?,?,?,?,?,?,?)", (f"e{i}", "run", "x", "t", 1, "{}", NOW))


def fresh(tmp_path, name="synapse.sqlite"):
    db = open_database(tmp_path / name)
    apply_migrations(db, ROOT / "migrations")
    db.execute("INSERT INTO workflow_versions VALUES('wf','1','{}','hash','now')")
    db.execute("INSERT INTO runs VALUES('run','wf','1','created',0,'now','now')")
    return db


@pytest.fixture
def env(tmp_path):
    db = fresh(tmp_path)
    sink = MemorySink()
    yield db, sink, tmp_path
    chain.forget(db)
    db.close()


def drop_guards(db):
    """What a same-user attacker does first."""
    for (name,) in db.execute("SELECT name FROM sqlite_master WHERE type='trigger' AND "
                              "(name LIKE 'audit_no_%' OR name LIKE 'chain_links_%' OR name LIKE 'events_prevent_%')").fetchall():
        db.execute(f"DROP TRIGGER {name}")


def recompute_chain(db, stream="audit"):
    """Attacker recomputes every link after editing rows (no secret is needed)."""
    table, cols = chain._SPEC[stream]
    db.execute("DELETE FROM chain_links WHERE stream=?", (stream,))
    prev = chain.genesis(stream)
    for row in db.execute(f"SELECT {','.join(cols)} FROM {table} ORDER BY sequence").fetchall():
        prev = chain.link_hash(stream, prev, row)
        db.execute("INSERT INTO chain_links VALUES(?,?,?)", (stream, row[0], prev))


def start(db, sink, **kw):
    return chain.startup_check(db, [sink], **kw)


# ------------------------------------------------------------- happy path

def test_clean_chain_verifies_and_anchors(env):
    db, sink, _ = env
    audit(db, 3)
    for i in range(4):
        event(db, i)
    report = start(db, sink)
    assert report.ok and report.strength == "journal-trusted"
    heads = {r["stream"]: r["seq"] for r in sink.records}
    assert heads == {"audit": 3, "events": 4}
    assert chain.verify(db, [sink], window=None).ok
    audit(db)   # a later daemon write stays valid and is anchored by the next flush
    chain.flush_anchors(db)
    assert sink.records[-1]["seq"] == 4 and chain.verify(db, [sink]).ok


def test_legacy_rows_are_adopted_on_first_start(tmp_path):
    db = fresh(tmp_path)
    db.execute("INSERT INTO audit_entries(principal,action,resource,decision,details_json,created_at) VALUES('p','a','r','allowed','{}','t')")
    assert db.execute("SELECT count(*) FROM chain_links").fetchone()[0] == 0
    assert start(db, MemorySink()).ok
    assert db.execute("SELECT count(*) FROM chain_links WHERE stream='audit'").fetchone()[0] == 1
    chain.forget(db)


# ------------------------------------------------------------ tamper cases

def test_edit_row_detected_even_with_chain_recomputed(env):
    db, sink, _ = env
    audit(db, 5)
    assert start(db, sink).ok
    drop_guards(db)
    db.execute("UPDATE audit_entries SET decision='denied' WHERE sequence=2")
    # Naive edit: row no longer matches its link.
    assert any("edited" in p for p in chain.verify(db, [sink]).problems)
    # Competent edit: recompute the whole chain. Only the external anchor still disagrees.
    recompute_chain(db)
    assert chain.verify(db, [], window=None).ok          # a chain with no anchors cannot tell
    problems = chain.verify(db, [sink]).problems
    assert any("differs from its anchor" in p for p in problems)


def test_delete_row_detected(env):
    db, sink, _ = env
    audit(db, 5)
    assert start(db, sink).ok
    drop_guards(db)
    db.execute("DELETE FROM audit_entries WHERE sequence=3")
    assert any("row 3 is missing" in p for p in chain.verify(db, [sink]).problems)
    recompute_chain(db)  # attacker also fixes the links
    assert any("anchor" in p for p in chain.verify(db, [sink]).problems)


def test_truncation_detected(env):
    db, sink, _ = env
    audit(db, 5)
    for i in range(5):
        event(db, i)
    assert start(db, sink).ok
    drop_guards(db)
    db.execute("DELETE FROM audit_entries WHERE sequence>2")
    db.execute("DELETE FROM chain_links WHERE stream='audit' AND sequence>2")
    db.execute("DELETE FROM sqlite_sequence WHERE name='audit_entries'")
    problems = chain.verify(db, [sink]).problems
    assert any("audit: truncated (head 2, anchored 5)" in p for p in problems)
    db.execute("DELETE FROM events WHERE sequence>1")
    db.execute("DELETE FROM chain_links WHERE stream='events' AND sequence>1")
    assert any("events: truncated" in p for p in chain.verify(db, [sink]).problems)


def test_edited_event_detected(env):
    db, sink, _ = env
    for i in range(4):
        event(db, i)
    assert start(db, sink).ok
    drop_guards(db)
    db.execute("UPDATE events SET payload_json='{\"x\":1}' WHERE sequence=2")
    assert any("events: row 2" in p for p in chain.verify(db, [sink]).problems)


def test_unanchored_chain_gives_no_claim_against_a_competent_attacker(env):
    """The honest limit: with no anchor, a recomputed chain verifies."""
    db, _, _ = env
    audit(db, 3)
    drop_guards(db)
    db.execute("UPDATE audit_entries SET principal='mallory' WHERE sequence=1")
    recompute_chain(db)
    report = chain.verify(db, [], window=None)
    assert report.ok and report.strength is None


def test_rows_appended_while_daemon_was_down_are_flagged(env):
    db, sink, _ = env
    audit(db, 2)
    assert start(db, sink).ok
    chain.forget(db)
    drop_guards(db)
    # attacker appends a forged row and (competently) its link
    db.execute("INSERT INTO audit_entries(principal,action,resource,decision,details_json,created_at) VALUES('x','approval.decide','ap','allowed','{}','t')")
    recompute_chain(db)
    assert any("beyond the last anchor" in p for p in chain.verify(db, [sink]).problems)
    assert not start(db, sink).ok


def test_foreign_append_while_running_breaks_chain_and_refuses_writes(env):
    db, sink, _ = env
    audit(db, 2)
    assert start(db, sink).ok
    drop_guards(db)
    with closing(sqlite3.connect(db.execute("PRAGMA database_list").fetchone()[2])) as other:
        other.execute("INSERT INTO audit_entries(principal,action,resource,decision,details_json,created_at) VALUES('x','a','r','allowed','{}','t')")
        other.commit()
    with pytest.raises(chain.ChainBroken):
        chain.require_intact(db)
    with pytest.raises(chain.ChainBroken):
        audit(db)


# ------------------------------------------------------------ gate refusal

def make_approval(db, *, audited=True):
    db.execute("BEGIN IMMEDIATE")
    write_approval_request(db, approval_id="ap", run_id="run", operation="push", target="repo", candidate_sha256=D,
                           policy_version="1", expires_at=(datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
                           requester_subject="agent-1", requester_kind="agent")
    write_approval_transition(db, ApprovalChange(ApprovalId("ap"), ApprovalState.REQUESTED, ApprovalState.APPROVED, 0),
                              principal="human:human-1", candidate_sha256=D, policy_version="1")
    if audited:
        chain.append_audit(db, "human:human-1", "approval.decide", "ap", "allowed", "c",
                           json.dumps({"decision": "approved"}), NOW)
    db.execute("COMMIT")


def test_gated_grant_refused_on_broken_chain(env):
    db, sink, _ = env
    make_approval(db)
    assert start(db, sink).ok
    assert verify(db, **ARGS).approval_id == "ap"      # intact chain: granted
    chain.forget(db)
    drop_guards(db)
    db.execute("UPDATE audit_entries SET resource='other' WHERE sequence=1")
    assert not start(db, sink).ok                      # daemon start finds the edit
    with pytest.raises(ApprovalNotGranted) as denied:
        verify(db, **ARGS)
    assert denied.value.reason == DenialReason.CHAIN_BROKEN


def test_forged_approval_without_audit_record_is_refused(env):
    db, sink, _ = env
    make_approval(db, audited=False)                   # state='approved' but nothing in the chain
    assert start(db, sink).ok
    with pytest.raises(ApprovalNotGranted) as denied:
        verify(db, **ARGS)
    assert denied.value.reason == DenialReason.CHAIN_BROKEN


def test_chain_inactive_without_startup_keeps_existing_behaviour(env):
    db, _, _ = env
    make_approval(db, audited=False)
    assert verify(db, **ARGS).approval_id == "ap"      # library use without the daemon is unchanged


def test_reset_accepts_restore_but_not_edits(env):
    db, sink, tmp = env
    audit(db, 3)
    assert start(db, sink).ok
    chain.forget(db)
    drop_guards(db)
    db.execute("DELETE FROM audit_entries WHERE sequence=3")
    db.execute("DELETE FROM chain_links WHERE stream='audit' AND sequence=3")
    db.execute("DELETE FROM sqlite_sequence WHERE name='audit_entries'")   # looks like restoring an older snapshot
    assert not start(db, sink).ok
    chain.forget(db)
    assert start(db, sink, accept_reset=True).ok
    assert sink.records[-1]["kind"] == "reset"
    chain.forget(db)
    assert start(db, sink).ok                                              # baseline accepted
    chain.forget(db)
    db.execute("UPDATE audit_entries SET action='edited' WHERE sequence=1")
    assert not start(db, sink, accept_reset=True).ok                       # edits are never reset away


# ------------------------------------------------------------- bounded cost

def vm_steps(db, sink, window):
    steps = [0]

    def tick():
        steps[0] += 1
        return 0
    db.set_progress_handler(tick, 1)
    try:
        assert chain.verify(db, [sink], window=window).ok
    finally:
        db.set_progress_handler(None, 1)
    return steps[0]


def test_startup_verification_cost_is_bounded_by_window_not_table_size(tmp_path):
    sizes = {}
    for n in (300, 6000):
        db = fresh(tmp_path, f"db{n}")
        db.execute("BEGIN")
        db.executemany("INSERT INTO events(id,aggregate_type,aggregate_id,type,schema_version,payload_json,occurred_at)"
                       " VALUES(?,?,?,?,?,?,?)", [(f"e{i}", "run", "x", "t", 1, "{}", NOW) for i in range(n)])
        db.execute("COMMIT")
        sink = MemorySink()
        assert start(db, sink).ok
        sizes[n] = vm_steps(db, sink, window=100)
        chain.forget(db)
        db.close()
    assert sizes[6000] < sizes[300] * 1.5     # 20x the rows, about the same work
    # and the window is what the work scales with
    assert vm_steps_window(tmp_path) > 3


def vm_steps_window(tmp_path):
    db = fresh(tmp_path, "dbw")
    db.execute("BEGIN")
    db.executemany("INSERT INTO events(id,aggregate_type,aggregate_id,type,schema_version,payload_json,occurred_at)"
                   " VALUES(?,?,?,?,?,?,?)", [(f"e{i}", "run", "x", "t", 1, "{}", NOW) for i in range(2000)])
    db.execute("COMMIT")
    sink = MemorySink()
    assert start(db, sink).ok
    ratio = vm_steps(db, sink, 1000) / vm_steps(db, sink, 100)
    chain.forget(db)
    db.close()
    return ratio


# ------------------------------------------------------ triggers / schema

def test_audit_is_append_only_and_approval_transitions_are_legal(env):
    db, _, _ = env
    audit(db)
    for sql in ("UPDATE audit_entries SET action='x'", "DELETE FROM audit_entries",
                "UPDATE chain_links SET entry_hash='%s'" % ("0" * 64), "DELETE FROM chain_links"):
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(sql)
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("INSERT INTO audit_entries(principal,action,resource,decision,details_json,created_at) VALUES('p','a','r','maybe','{}','t')")
    make_approval(db)
    for target in ("requested", "rejected"):
        with pytest.raises(sqlite3.IntegrityError, match="illegal approval state"):
            db.execute("UPDATE approval_requests SET state=?", (target,))
    db.execute("UPDATE approval_requests SET state='revoked'")          # approved -> revoked is legal (restore)
    with pytest.raises(sqlite3.IntegrityError, match="illegal approval state"):
        db.execute("UPDATE approval_requests SET state='approved'")


def test_schema_fingerprint_covers_new_triggers(env, tmp_path):
    from synapse.persistence.backup import BackupError, _check_schema, code_schema_version
    db, _, _ = env
    version = code_schema_version(ROOT / "migrations")
    assert version >= 15
    _check_schema(db, ROOT / "migrations", version)
    db.execute("DROP TRIGGER approval_state_transition")
    with pytest.raises(BackupError, match="approval_state_transition"):
        _check_schema(db, ROOT / "migrations", version)


def test_journal_sink_reads_only_its_unit_and_parses_records():
    lines = [json.dumps({"MESSAGE": "synapse-chain-anchor " + json.dumps(
        {"v": 1, "kind": "head", "scope": "s", "stream": "audit", "seq": 7, "hash": "h"})}), "garbage",
        json.dumps({"MESSAGE": "unrelated"})]
    seen = {}

    class Done:
        stdout = "\n".join(lines)

    def run(cmd, **kw):
        seen["cmd"] = cmd
        return Done()

    sink = chain.JournalAnchors(unit="synapse.service", uid=1000, journalctl="/bin/journalctl", run=run)
    assert sink.strength == "journal-trusted"
    assert sink.read() == [{"v": 1, "kind": "head", "scope": "s", "stream": "audit", "seq": 7, "hash": "h"}]
    assert "_SYSTEMD_USER_UNIT=synapse.service" in seen["cmd"] and "_UID=1000" in seen["cmd"]
    assert chain.JournalAnchors(unit=None, journalctl="/x", run=run).strength == "journal"


def test_file_sink_roundtrip(tmp_path):
    sink = chain.FileAnchors(tmp_path / "a.log")
    assert sink.read() == []
    sink.publish({"v": 1, "kind": "head", "scope": "s", "stream": "events", "seq": 1, "hash": "h"})
    assert sink.read()[0]["seq"] == 1


def test_anchors_of_another_database_are_ignored(tmp_path):
    shared = MemorySink()          # one journal, two state dirs of the same user
    first, second = fresh(tmp_path, "one.sqlite"), fresh(tmp_path, "two.sqlite")
    audit(first, 5)
    assert start(first, shared).ok
    assert start(second, shared).ok            # second has no history; first's anchors are not its business
    assert chain.verify(second, [shared]).ok and chain.verify(first, [shared]).ok
    for db in (first, second):
        chain.forget(db)
        db.close()


# ------------------------------------------------- review fixes (PR #119)

def test_rollback_of_caller_transaction_with_two_appends_does_not_break_chain(env):
    db, sink, _ = env
    audit(db, 2)
    assert start(db, sink).ok
    db.execute("BEGIN IMMEDIATE")
    audit(db, 2)
    db.execute("ROLLBACK")
    audit(db)                                   # raised ChainBroken before the fix
    assert chain._STATES[chain._key(db)].broken is None
    chain.require_intact(db)
    assert chain.verify(db, [sink], window=None).ok
    db.execute("BEGIN IMMEDIATE")               # and a committed caller transaction also keeps working
    audit(db, 2)
    db.execute("COMMIT")
    audit(db)
    chain.require_intact(db)
    assert chain.verify(db, [sink], window=None).ok


class FailingSink:
    name, strength = "failing", "journal"

    def publish(self, record):
        raise OSError("socket full")

    def read(self):
        return []


def test_publish_failure_is_logged_and_retried_by_a_later_flush(env, caplog):
    db, _, _ = env
    audit(db, 1)
    flaky = {"fail": False}

    class Flaky(MemorySink):
        def publish(self, record):
            if flaky["fail"]:
                raise OSError("down")
            super().publish(record)

    only = Flaky()
    assert chain.startup_check(db, [only]).ok
    flaky["fail"] = True
    with caplog.at_level("WARNING", logger=chain.log.name):
        audit(db)
    assert "failed to publish" in caplog.text and "OSError" in caplog.text
    assert only.records[-1]["seq"] == 1          # the new head did not reach any sink
    flaky["fail"] = False
    chain.flush_anchors(db)                      # retried: it was not marked published
    assert only.records[-1]["seq"] == 2


def test_one_working_sink_is_enough_and_the_failing_one_is_logged(env, caplog):
    db, sink, _ = env
    audit(db, 1)
    with caplog.at_level("WARNING", logger=chain.log.name):
        assert chain.startup_check(db, [FailingSink(), sink]).ok
    assert "failing" in caplog.text and sink.records
    count = len(sink.records)
    chain.flush_anchors(db)
    assert len(sink.records) == count            # published: not retried


def test_journal_publish_uses_a_send_timeout(monkeypatch):
    seen = {}

    class Sock:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def settimeout(self, t): seen["timeout"] = t
        def sendto(self, payload, path): raise BlockingIOError()

    monkeypatch.setattr(chain.socket, "socket", lambda *a, **k: Sock())
    with pytest.raises(OSError):
        chain.JournalAnchors(unit=None, journalctl="/x", socket_path="/nope").publish(
            {"v": 1, "kind": "head", "scope": "s", "stream": "audit", "seq": 1, "hash": "h"})
    assert 0 < seen["timeout"] <= 2


def test_adoption_is_recorded_anchored_and_reported(tmp_path):
    db = fresh(tmp_path)
    for _ in range(3):
        db.execute("INSERT INTO audit_entries(principal,action,resource,decision,details_json,created_at)"
                   " VALUES('p','a','r','allowed','{}','t')")
    sink = MemorySink()
    report = start(db, sink)
    assert report.ok and report.adopted == {"audit": 3}
    assert db.execute("SELECT adopted_up_to FROM chain_adoptions WHERE stream='audit'").fetchone()[0] == 3
    assert any(r["kind"] == "adopt" and r["stream"] == "audit" and r["adopted_up_to"] == 3 for r in sink.records)
    chain.forget(db)
    assert start(db, sink).adopted == {"audit": 3}      # restart: marker persists, not re-adopted
    assert sum(r["kind"] == "adopt" for r in sink.records) == 1
    with pytest.raises(sqlite3.DatabaseError):
        db.execute("UPDATE chain_adoptions SET adopted_up_to=1")
    chain.forget(db)
    db.close()


def test_adoption_marker_tampering_is_detected_against_the_anchor(tmp_path):
    db = fresh(tmp_path)
    db.execute("INSERT INTO audit_entries(principal,action,resource,decision,details_json,created_at)"
               " VALUES('p','a','r','allowed','{}','t')")
    sink = MemorySink()
    assert start(db, sink).ok
    chain.forget(db)
    db.execute("DROP TRIGGER chain_adoptions_no_update")
    db.execute("UPDATE chain_adoptions SET adopted_up_to=99")
    assert any("adoption marker" in p for p in chain.verify(db, [sink]).problems)
    db.close()


def test_a_fresh_chain_has_no_adoption(env):
    db, sink, _ = env
    audit(db, 2)
    assert start(db, sink).adopted == {}


def test_cli_verify_distinguishes_unanchored_from_verified(tmp_path, capsys):
    db = fresh(tmp_path)
    audit(db, 2)
    path = tmp_path / "synapse.sqlite"
    assert chain.main(["--db", str(path)]) == chain.EXIT_UNANCHORED == 2
    out = capsys.readouterr().out
    assert out.startswith("UNANCHORED") and "WARNING" in out and "NOT a pass" in out
    log = tmp_path / "chain-anchors.log"
    assert chain.main(["--db", str(path), "--anchor-file", str(log)]) == 2      # sink readable but empty
    assert chain.startup_check(db, [chain.FileAnchors(log)]).ok
    capsys.readouterr()
    assert chain.main(["--db", str(path), "--anchor-file", str(log)]) == 0
    assert capsys.readouterr().out.startswith("OK")
    chain.forget(db)
    drop_guards(db)
    db.execute("UPDATE audit_entries SET action='x' WHERE sequence=1")
    assert chain.main(["--db", str(path), "--anchor-file", str(log)]) == 1
    db.close()


def test_env_override_disables_the_journal_sink(tmp_path, monkeypatch):
    monkeypatch.setattr(chain.JournalAnchors, "available", staticmethod(lambda *a: True))
    monkeypatch.setenv(chain.DISABLE_JOURNAL_ENV, "1")
    assert [s.name for s in chain.default_sinks(tmp_path)] == ["file"]
    monkeypatch.delenv(chain.DISABLE_JOURNAL_ENV)
    assert [s.name for s in chain.default_sinks(tmp_path)] == ["journal", "file"]
