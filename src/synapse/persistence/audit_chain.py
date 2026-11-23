"""Hash chain, external anchors and verification for audit_entries and events (ADR 018).

Read docs/adr/018-audit-hash-chain.md before relying on this. In short: the chain is
unkeyed, so a same-user process can recompute it. What it cannot do is rewrite the
*anchors* the daemon publishes to the systemd journal (not writable by the user), so
edits, deletions and truncation of anchored history are detectable. Rows appended
after the last anchor, and anything done while the daemon is not running and has not
yet anchored, are not.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import logging
import os
from pathlib import Path
import shutil
import socket
import sqlite3
import subprocess
import threading
from datetime import datetime, timezone

log = logging.getLogger(__name__)
DISABLE_JOURNAL_ENV = "SYNAPSE_DISABLE_JOURNAL_ANCHORS"

_SPEC = {
    "audit": ("audit_entries", ("sequence", "principal", "action", "resource", "decision",
                                "correlation_id", "details_json", "created_at")),
    "events": ("events", ("sequence", "id", "aggregate_type", "aggregate_id", "run_id", "type",
                          "schema_version", "payload_json", "occurred_at", "correlation_id",
                          "causation_id")),
}
DEFAULT_WINDOW = 20000   # links re-hashed per stream at startup; startup cost is O(window), not O(table)
ANCHOR_READ_LIMIT = 5000
SEAL_BATCH = 5000


class ChainBroken(RuntimeError):
    """The audit chain failed verification; gated work must not proceed."""


def genesis(stream: str) -> str:
    return hashlib.sha256(("synapse-chain-v1:" + stream).encode()).hexdigest()


def link_hash(stream: str, prev_hash: str, row: tuple) -> str:
    body = json.dumps(list(row), separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256((prev_hash + "\0" + body).encode()).hexdigest()


# ---------------------------------------------------------------- anchors

class FileAnchors:
    """Append-only text file. Same-user writable: only catches edits made without knowing about it."""
    name, strength = "file", "local"

    def __init__(self, path: Path):
        self.path = Path(path)

    def publish(self, record: dict) -> None:
        line = (json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n").encode()
        fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600)
        try:
            os.write(fd, line)
        finally:
            os.close(fd)

    def read(self) -> list[dict]:
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()[-ANCHOR_READ_LIMIT:]
        except FileNotFoundError:
            return []
        return [r for r in (_parse(line) for line in lines) if r]


class AnchorUnavailable(RuntimeError):
    pass


class JournalAnchors:
    """Anchors in the systemd journal; records the user cannot alter or delete without root.

    ``unit`` is the user unit the daemon runs in. Only with a unit filter does the reader
    ignore records injected by other same-user processes (journald stamps the trusted
    ``_SYSTEMD_USER_UNIT`` from the sender's cgroup), so only then is the sink "trusted".
    """
    IDENT = "synapse-chain-anchor"
    SOCKET = "/run/systemd/journal/socket"
    name = "journal"
    SEND_TIMEOUT = 0.5

    def __init__(self, unit: str | None = None, *, uid: int | None = None, journalctl: str | None = None,
                 run=subprocess.run, socket_path: str | None = None):
        self.unit, self.uid = unit, os.geteuid() if uid is None else uid
        self._journalctl = journalctl or shutil.which("journalctl")
        self._run, self._socket = run, socket_path or self.SOCKET

    @property
    def strength(self) -> str:
        return "journal-trusted" if self.unit else "journal"

    @staticmethod
    def available(socket_path: str = SOCKET) -> bool:
        return os.path.exists(socket_path) and shutil.which("journalctl") is not None

    def publish(self, record: dict) -> None:
        message = "synapse-chain-anchor " + json.dumps(record, sort_keys=True, separators=(",", ":"))
        payload = f"SYSLOG_IDENTIFIER={self.IDENT}\nPRIORITY=6\nMESSAGE={message}\n".encode()
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
            sock.settimeout(self.SEND_TIMEOUT)     # a full journald socket must not stall the request path
            sock.sendto(payload, self._socket)

    def read(self) -> list[dict]:
        if not self._journalctl:
            raise AnchorUnavailable("journalctl not found")
        cmd = [self._journalctl, "-t", self.IDENT, "-o", "json", "--no-pager", "-q",
               "-n", str(ANCHOR_READ_LIMIT), f"_UID={self.uid}"]
        if self.unit:
            cmd.append(f"_SYSTEMD_USER_UNIT={self.unit}")
        try:
            out = self._run(cmd, capture_output=True, text=True, timeout=10, check=True).stdout
        except (OSError, subprocess.SubprocessError) as exc:
            raise AnchorUnavailable("journalctl failed") from exc
        records = []
        for line in out.splitlines():
            try:
                message = json.loads(line).get("MESSAGE")
            except ValueError:
                continue
            if isinstance(message, str) and message.startswith("synapse-chain-anchor "):
                if record := _parse(message.split(" ", 1)[1]):
                    records.append(record)
        return records


def _parse(text: str) -> dict | None:
    try:
        record = json.loads(text)
    except ValueError:
        return None
    ok = (isinstance(record, dict) and record.get("v") == 1 and record.get("stream") in _SPEC
          and type(record.get("seq")) is int and isinstance(record.get("hash"), str)
          and isinstance(record.get("scope"), str)
          and record.get("kind") in ("head", "reset", "adopt"))
    return record if ok else None


def current_unit(cgroup: str = "/proc/self/cgroup") -> str | None:
    """Name of the systemd user service this process runs in, if any."""
    try:
        for line in Path(cgroup).read_text().splitlines():
            leaf = line.rsplit("/", 1)[-1]
            if leaf.endswith(".service") and "user@" in line:
                return leaf
    except OSError:
        pass
    return None


def journal_enabled() -> bool:
    """False when SYNAPSE_DISABLE_JOURNAL_ANCHORS is set to a non-empty, non-"0" value (tests, containers)."""
    return os.environ.get(DISABLE_JOURNAL_ENV, "") in ("", "0")


def default_sinks(state_dir: Path) -> list:
    sinks: list = []
    if journal_enabled() and JournalAnchors.available():
        sinks.append(JournalAnchors(unit=current_unit()))
    sinks.append(FileAnchors(Path(state_dir) / "chain-anchors.log"))
    return sinks


# ------------------------------------------------------------------ state

class _State:
    def __init__(self, sinks):
        self.sinks = list(sinks)
        self.broken: list[str] | None = None
        # stream -> {"committed": (seq, hash), "pending": set of (seq, hash) written in transactions
        # this process has not yet seen committed}. Only mutated under _LOCK.
        self.head: dict[str, dict] = {}
        self.published: dict[str, tuple[int, str]] = {}


_STATES: dict[str, _State] = {}
_LOCK = threading.RLock()


def _key(db: sqlite3.Connection) -> str:
    path = db.execute("PRAGMA database_list").fetchone()[2]
    return os.path.realpath(path) if path else f"mem:{id(db)}"


def scope_of(db: sqlite3.Connection) -> str:
    """Identity of the database for anchors: derived from its path, which a database edit cannot change.

    Anchors from other databases of the same user (other state dirs, tests) share the journal and must not
    be confused with this one's.
    """
    return hashlib.sha256(_key(db).encode()).hexdigest()[:16]


def _tail(db, stream) -> tuple[int, str]:
    row = db.execute("SELECT sequence,entry_hash FROM chain_links WHERE stream=? ORDER BY sequence DESC LIMIT 1",
                     (stream,)).fetchone()
    return (row[0], row[1]) if row else (0, genesis(stream))


def _max_seq(db, stream) -> int:
    return db.execute(f"SELECT COALESCE(MAX(sequence),0) FROM {_SPEC[stream][0]}").fetchone()[0]


def seal(db: sqlite3.Connection, stream: str, *, batch: int = SEAL_BATCH, adopt: bool = False) -> int:
    """Append links for rows that have none. Returns the number sealed.

    ``adopt`` marks a first seal of pre-chain history: a chain_adoptions row records the highest
    sequence that was adopted trust-on-first-use (not attested by any earlier anchor).
    """
    table, cols = _SPEC[stream]
    sealed = 0
    own = not db.in_transaction
    if own:
        db.execute("BEGIN IMMEDIATE")
    try:
        seq, prev = _tail(db, stream)
        while True:
            rows = db.execute(f"SELECT {','.join(cols)} FROM {table} WHERE sequence>? ORDER BY sequence LIMIT ?",
                              (seq, batch)).fetchall()
            if not rows:
                break
            links = []
            for row in rows:
                prev = link_hash(stream, prev, row)
                seq = row[0]
                links.append((stream, seq, prev))
            db.executemany("INSERT INTO chain_links(stream,sequence,entry_hash) VALUES(?,?,?)", links)
            sealed += len(links)
        if adopt and sealed and db.execute("SELECT 1 FROM sqlite_master WHERE name='chain_adoptions'").fetchone():
            db.execute("INSERT OR IGNORE INTO chain_adoptions(stream,adopted_up_to,adopted_at) VALUES(?,?,?)",
                       (stream, seq, datetime.now(timezone.utc).isoformat()))
        if own:
            db.execute("COMMIT")
    except BaseException:
        if own and db.in_transaction:
            db.execute("ROLLBACK")
        raise
    if sealed:
        with _LOCK:
            state = _STATES.get(_key(db))
            if state is not None and stream == "events":
                state.head[stream] = {"committed": (seq, prev), "pending": set()}
    return sealed


def append_audit(db: sqlite3.Connection, principal: str, action: str, resource: str, decision: str,
                 correlation_id: str | None, details_json: str, created_at: str) -> int:
    """Insert an audit row and its chain link in one transaction (the caller's, or a new one).

    Once startup_check has run for this database, a head that differs from what this
    process last wrote means something else appended or rewrote audit data: the chain is
    marked broken and ChainBroken is raised (so the decision being recorded is refused).
    """
    if db.execute("SELECT 1 FROM sqlite_master WHERE name='chain_links'").fetchone() is None:
        # Database older than migration 0015 (e.g. a restored v1-v14 backup): nothing to chain yet.
        return db.execute("INSERT INTO audit_entries(principal,action,resource,decision,correlation_id,details_json,created_at)"
                          " VALUES(?,?,?,?,?,?,?)",
                          (principal, action, resource, decision, correlation_id, details_json, created_at)).lastrowid
    own = not db.in_transaction
    if own:
        db.execute("BEGIN IMMEDIATE")
    try:
        with _LOCK:
            state = _STATES.get(_key(db))
        seq, prev = _tail(db, "audit")
        if state is not None and state.broken is not None:
            raise ChainBroken("; ".join(state.broken[:3]))
        if state is not None:
            _check_audit_head(db, state, seq, prev, settled=own)
        elif state is None and _max_seq(db, "audit") > seq:
            seal(db, "audit", adopt=True)   # no startup check in this process (tests, tools): adopt
            seq, prev = _tail(db, "audit")
        db.execute("INSERT INTO audit_entries(principal,action,resource,decision,correlation_id,details_json,created_at)"
                   " VALUES(?,?,?,?,?,?,?)",
                   (principal, action, resource, decision, correlation_id, details_json, created_at))
        new = db.execute(f"SELECT {','.join(_SPEC['audit'][1])} FROM audit_entries WHERE sequence=last_insert_rowid()").fetchone()
        digest = link_hash("audit", prev, new)
        db.execute("INSERT INTO chain_links(stream,sequence,entry_hash) VALUES('audit',?,?)", (new[0], digest))
        if own:
            db.execute("COMMIT")
    except BaseException:
        if own and db.in_transaction:
            db.execute("ROLLBACK")
        raise
    if state is not None:
        with _LOCK:
            entry = state.head.setdefault("audit", {"committed": (seq, prev), "pending": set()})
            if own:     # committed just now
                entry["committed"], entry["pending"] = (new[0], digest), set()
            else:       # the caller's transaction may still roll back: remember it, never trust it as committed
                entry["pending"].add((new[0], digest))
    if own:
        flush_anchors(db)
    return new[0]


def _check_audit_head(db, state: _State, seq: int, prev: str, *, settled: bool) -> None:
    """``settled``: no uncommitted work of ours is visible (we just began the transaction, or none is open).

    Then the tail is committed data: it may be promoted to the committed head. Otherwise (inside a
    caller-owned transaction) the tail may include our own uncommitted links, which are accepted
    but never promoted; a rollback simply returns the tail to the committed head.
    """
    problem = None
    with _LOCK:
        head = state.head.get("audit")
        if _max_seq(db, "audit") > seq:
            problem = "audit rows exist without chain links"
        elif head is not None:
            if (seq, prev) == head["committed"]:
                pass
            elif (seq, prev) in head["pending"]:
                if settled:
                    head["committed"], head["pending"] = (seq, prev), set()
            else:
                problem = "audit chain head changed outside the daemon"
        if problem:
            state.broken = [problem]
    if problem:
        raise ChainBroken(problem)


# ------------------------------------------------------------ verification

@dataclass(frozen=True)
class ChainReport:
    ok: bool
    problems: tuple[str, ...]
    strength: str | None   # strongest anchor sink that was readable, None if no anchors were available
    checked: dict
    anchor_records: int = 0                       # anchor records found for this database (all readable sinks)
    adopted: dict = field(default_factory=dict)   # stream -> highest sequence adopted trust-on-first-use


def _verify_stream(db, stream: str, window: int | None) -> tuple[list[str], int]:
    table, cols = _SPEC[stream]
    problems: list[str] = []
    tail_seq, _ = _tail(db, stream)
    limit = "" if window is None else f" LIMIT {int(window)}"
    links = db.execute("SELECT sequence,entry_hash FROM chain_links WHERE stream=? ORDER BY sequence DESC" + limit,
                       (stream,)).fetchall()
    links.reverse()
    if not links:
        return problems, 0
    first = links[0][0]
    before = db.execute("SELECT entry_hash FROM chain_links WHERE stream=? AND sequence<? ORDER BY sequence DESC LIMIT 1",
                        (stream, first)).fetchone()
    prev = before[0] if before else genesis(stream)
    rows = {r[0]: r for r in db.execute(
        f"SELECT {','.join(cols)} FROM {table} WHERE sequence BETWEEN ? AND ?", (first, tail_seq))}
    for seq, stored in links:
        row = rows.get(seq)
        if row is None:
            problems.append(f"{stream}: row {seq} is missing (deleted)")
            prev = stored      # keep going so later edits are still found
            continue
        if link_hash(stream, prev, row) != stored:
            problems.append(f"{stream}: row {seq} does not match its chain link (edited)")
        prev = stored      # continue from the stored link so one edit does not mask later ones
    return problems, len(links)


def verify(db: sqlite3.Connection, sinks=(), *, window: int | None = DEFAULT_WINDOW) -> ChainReport:
    """Verify links over the last ``window`` rows per stream (None = whole history) and all readable anchors.

    Cost is bounded by ``window`` plus the anchor read limit, independent of table size.
    """
    problems: list[str] = []
    checked = {}
    strength = None
    order = {"local": 1, "journal": 2, "journal-trusted": 3}
    scope = scope_of(db)
    anchors: dict[str, list[dict]] = {s: [] for s in _SPEC}
    adopted: dict[str, int] = {}
    if db.execute("SELECT 1 FROM sqlite_master WHERE name='chain_adoptions'").fetchone():
        adopted = dict(db.execute("SELECT stream,adopted_up_to FROM chain_adoptions"))
    for sink in sinks:
        try:
            records = sink.read()
        except AnchorUnavailable:
            continue
        if strength is None or order[sink.strength] > order[strength]:
            strength = sink.strength
        # A reset record discards older anchors of its sink. Only a journal-trusted sink makes
        # forging one detectable (it is stamped with the daemon's unit); see the ADR.
        for stream in _SPEC:
            mine = [r for r in records if r["stream"] == stream and r["scope"] == scope]
            resets = [i for i, r in enumerate(mine) if r["kind"] == "reset"]
            if resets:
                mine = mine[resets[-1]:]
            anchors[stream] += mine
    for stream in _SPEC:
        for record in anchors[stream]:
            if record["kind"] == "adopt" and adopted.get(stream) != record.get("adopted_up_to"):
                problems.append(f"{stream}: adoption marker differs from its anchor")
        found, count = _verify_stream(db, stream, window)
        problems += found
        checked[stream] = count
        tail_seq, _ = _tail(db, stream)
        if stream == "audit" and tail_seq and _max_seq(db, stream) > tail_seq:
            problems.append("audit: rows exist without chain links")
        seen = set()
        for record in anchors[stream]:
            seq, digest = record["seq"], record["hash"]
            if (seq, digest) in seen:
                continue
            seen.add((seq, digest))
            if seq == 0:
                continue                      # a reset recorded over an empty stream
            if seq > tail_seq:
                problems.append(f"{stream}: truncated (head {tail_seq}, anchored {seq})")
                continue
            row = db.execute("SELECT entry_hash FROM chain_links WHERE stream=? AND sequence=?", (stream, seq)).fetchone()
            if row is None or row[0] != digest:
                problems.append(f"{stream}: link {seq} differs from its anchor (history rewritten)")
        if stream == "audit" and anchors[stream]:
            latest = max(r["seq"] for r in anchors[stream])
            if _max_seq(db, stream) > latest:
                problems.append(f"audit: rows beyond the last anchor ({latest}); appended while the daemon was not anchoring")
    return ChainReport(not problems, tuple(dict.fromkeys(problems)), strength, checked,
                       sum(len(v) for v in anchors.values()), adopted)


# -------------------------------------------------------- daemon lifecycle

def _publish(state: _State, scope: str, stream: str, seq: int, digest: str, kind: str = "head",
             **extra) -> bool:
    """Publish a head to every sink. Marks it published only if at least one sink took it (or there are none),
    so a later flush retries. Each failing sink is logged (sink name and error class only)."""
    record = {"v": 1, "kind": kind, "scope": scope, "stream": stream, "seq": seq, "hash": digest,
              "ts": datetime.now(timezone.utc).isoformat(), **extra}
    ok = not state.sinks
    for sink in state.sinks:
        try:
            sink.publish(record)
            ok = True
        except (OSError, AnchorUnavailable) as exc:
            log.warning("audit chain anchor sink %r failed to publish %s head %d: %s",
                        getattr(sink, "name", type(sink).__name__), stream, seq, type(exc).__name__)
    if ok:
        with _LOCK:
            state.published[stream] = (seq, digest)
    return ok


def startup_check(db: sqlite3.Connection, sinks, *, window: int | None = DEFAULT_WINDOW,
                  accept_reset: bool = False, schema_problems: tuple[str, ...] = ()) -> ChainReport:
    """Verify (bounded), adopt legacy rows on first use, register state and anchor the head.

    On failure the chain is marked broken and require_intact() raises until the process
    restarts. ``accept_reset`` re-baselines anchor-versus-database disagreements only (a restored
    backup, or an unexpected crash); links that no longer match their rows are never reset.
    """
    state = _State(sinks)
    with _LOCK:
        _STATES[_key(db)] = state
    adopted: set[str] = set()
    for stream in _SPEC:
        if _tail(db, stream)[0] == 0 and _max_seq(db, stream) > 0:
            seal(db, stream, adopt=True)    # first start after the migration: trust on first use, recorded
            adopted.add(stream)
    seal(db, "events")
    report = verify(db, sinks, window=window)
    problems = list(report.problems) + list(schema_problems)
    if accept_reset:
        internal = [p for p in problems if "does not match its chain link" in p or "is missing" in p
                    or p.startswith("schema")]
        if not internal:
            seal(db, "audit")
            problems = []
    if problems:
        state.broken = problems
    for stream in _SPEC:
        seq, digest = _tail(db, stream)
        with _LOCK:
            state.head[stream] = {"committed": (seq, digest), "pending": set()}
        if not problems and (accept_reset or seq):
            if stream in adopted:
                _publish(state, scope_of(db), stream, seq, digest, "adopt",
                         adopted_up_to=report.adopted.get(stream, seq))
            else:
                _publish(state, scope_of(db), stream, seq, digest, "reset" if accept_reset else "head")
    return ChainReport(not problems, tuple(problems), report.strength, report.checked,
                       report.anchor_records, report.adopted)


def flush_anchors(db: sqlite3.Connection) -> None:
    """Seal new events and publish heads that changed. Cheap; safe to call often. No-op before startup_check."""
    if db.in_transaction:
        return
    with _LOCK:
        state = _STATES.get(_key(db))
    if state is None or state.broken is not None:
        return
    seal(db, "events")
    for stream in _SPEC:
        seq, digest = _tail(db, stream)
        if seq and state.published.get(stream) != (seq, digest):
            _publish(state, scope_of(db), stream, seq, digest)


def require_intact(db: sqlite3.Connection) -> None:
    """Gate check: raise ChainBroken if startup verification failed or the head moved behind our back."""
    with _LOCK:
        state = _STATES.get(_key(db))
    if state is None:
        return                            # chain not activated in this process
    if state.broken is not None:
        raise ChainBroken("; ".join(state.broken[:3]))
    seq, prev = _tail(db, "audit")
    _check_audit_head(db, state, seq, prev, settled=not db.in_transaction)
    flush_anchors(db)


def require_decision_audited(db: sqlite3.Connection, approval_id: str, principal: str) -> None:
    """An approved approval must have a chained audit row for that decision by that principal."""
    with _LOCK:
        active = _key(db) in _STATES
    if not active:
        return
    row = db.execute(
        "SELECT 1 FROM audit_entries a JOIN chain_links c ON c.stream='audit' AND c.sequence=a.sequence "
        "WHERE a.action='approval.decide' AND a.decision='allowed' AND a.resource=? AND a.principal=? "
        "AND json_extract(a.details_json,'$.decision')='approved' LIMIT 1", (approval_id, principal)).fetchone()
    if row is None:
        raise ChainBroken("approval has no chained audit record of its decision")


def forget(db: sqlite3.Connection) -> None:
    """Drop in-process chain state for this database (tests)."""
    with _LOCK:
        _STATES.pop(_key(db), None)


EXIT_OK, EXIT_BROKEN, EXIT_UNANCHORED = 0, 1, 2


def main(argv: list[str] | None = None) -> int:
    """Exit status: 0 verified against at least one anchor, 1 broken, 2 links consistent but UNANCHORED
    (no readable sink, or no anchors for this database): that is not a pass against a competent attacker."""
    import argparse
    parser = argparse.ArgumentParser(description="Verify the audit hash chain of a Synapse database (read-only)")
    parser.add_argument("--db", required=True, type=Path)
    parser.add_argument("--full", action="store_true", help="re-hash the whole history, not just the recent window")
    parser.add_argument("--anchor-file", type=Path, help="state-dir chain-anchors.log")
    parser.add_argument("--unit", help="user unit of the daemon, to trust only its journal anchors")
    args = parser.parse_args(argv)
    sinks = [FileAnchors(args.anchor_file)] if args.anchor_file else []
    if journal_enabled() and JournalAnchors.available():
        sinks.append(JournalAnchors(unit=args.unit))
    db = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    try:
        report = verify(db, sinks, window=None if args.full else DEFAULT_WINDOW)
    finally:
        db.close()
    unanchored = report.ok and (report.strength is None or report.anchor_records == 0)
    status = "BROKEN" if not report.ok else "UNANCHORED" if unanchored else "OK"
    print(f"{status} anchors={report.strength} anchor_records={report.anchor_records} checked={report.checked}")
    for stream, upto in sorted(report.adopted.items()):
        print(f"NOTE: {stream} rows up to seq {upto} were adopted trust-on-first-use, not attested")
    if unanchored:
        print("WARNING: no anchors were available for this database (no readable sink, or no anchor records for it); "
              "the links are self-consistent but nothing external vouches for them. This is NOT a pass.")
    for problem in report.problems:
        print(" -", problem)
    return EXIT_BROKEN if not report.ok else EXIT_UNANCHORED if unanchored else EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
