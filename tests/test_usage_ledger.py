"""Usage ledger: strict parsing, migration 0014, append-only rows, the run usage query, API and CLI."""
from contextlib import closing
import json
import math
from pathlib import Path
import sqlite3
from types import SimpleNamespace

from fastapi.testclient import TestClient
import pytest

from synapse.api.app import create_app
from synapse.cli import __main__ as cli
from synapse.persistence.migrations import apply_migrations, open_database
from synapse.persistence.run_snapshot import RunNotFound
from synapse.persistence.usage_query import read_run_usage
from synapse.providers.claude_cli import ClaudeCliProfile, ClaudeCliProvider
from synapse.providers.contracts import UsageObservation

ROOT = Path(__file__).resolve().parents[1]
TOKEN = "t" * 43
HEADERS = {"Authorization": "Bearer " + TOKEN}


# ---------------------------------------------------------------- parsing
def parse(result: dict):
    prov = ClaudeCliProvider(ClaudeCliProfile(executable="/usr/bin/claude", allowed_tools=("Read",), state_dir="/s"))
    ex = SimpleNamespace(result_ok=None, usage=None)
    prov._parse(ex, json.dumps({"type": "result", "subtype": "success", "is_error": False, **result}).encode())
    return ex.usage


def test_real_result_shape_is_captured():
    usage = parse({"total_cost_usd": 0.0131914, "usage": {
        "input_tokens": 2, "cache_creation_input_tokens": 3200, "cache_read_input_tokens": 3474, "output_tokens": 4}})
    assert usage == UsageObservation(2, 4, "claude-cli", 3474, 3200, 0.0131914)


def test_partial_usage_keeps_unknown_fields_none_not_zero():
    usage = parse({"usage": {"input_tokens": 5}})
    assert (usage.input_tokens, usage.output_tokens, usage.cache_read_tokens, usage.cache_write_tokens,
            usage.cost_usd) == (5, None, None, None, None)
    assert parse({"total_cost_usd": 0, "usage": {}}).cost_usd == 0.0  # a reported zero is a real zero


@pytest.mark.parametrize("bad", [True, False, -1, "7", 1.5, None, [1], {"a": 1}])
def test_token_fields_must_be_non_negative_ints(bad):
    assert parse({"usage": {"input_tokens": bad, "output_tokens": bad,
                            "cache_read_input_tokens": bad, "cache_creation_input_tokens": bad}}) is None


@pytest.mark.parametrize("bad", [True, -0.1, "0.1", math.inf, -math.inf, math.nan, None, [0.1]])
def test_cost_must_be_a_finite_non_negative_number(bad):
    assert parse({"total_cost_usd": bad, "usage": {}}) is None


def test_values_sqlite_cannot_store_are_unreportable_but_do_not_drop_the_rest():
    big = parse({"total_cost_usd": 1e300, "usage": {"input_tokens": 3, "output_tokens": 2**63, "cache_read_input_tokens": 2**70}})
    assert (big.input_tokens, big.output_tokens, big.cache_read_tokens, big.cost_usd) == (3, None, None, None)
    edge = parse({"total_cost_usd": 1e12, "usage": {"input_tokens": 2**63 - 1}})
    assert (edge.input_tokens, edge.cost_usd) == (2**63 - 1, 1e12)  # the largest storable values still count
    assert parse({"usage": {"input_tokens": 2**63}}) is None  # nothing usable at all: no observation


def test_result_without_usage_object_records_no_observation():
    assert parse({}) is None and parse({"usage": "lots"}) is None


def test_observation_stays_backward_compatible():
    old = UsageObservation(1, 2, "claude-cli")
    assert old.cache_read_tokens is None and old.cache_write_tokens is None and old.cost_usd is None


# ------------------------------------------------------------- migration
def _db_before_0014(tmp_path):
    directory = tmp_path / "m13"
    directory.mkdir()
    for path in sorted((ROOT / "migrations").glob("*.sql")):
        if int(path.name[:4]) < 14:
            (directory / path.name).write_bytes(path.read_bytes())
    return directory


def _seed(db, *, run="r1", attempts=1):
    db.execute("INSERT INTO workflow_versions(workflow_id,version,definition_json,definition_sha256,published_at) "
               "VALUES('w','1','{}','x','now')")
    db.execute("INSERT INTO runs(id,workflow_id,workflow_version,state,state_version,created_at,updated_at) "
               "VALUES(?,?,?,?,?,?,?)", (run, "w", "1", "running", 0, "now", "now"))
    db.execute("INSERT INTO profiles(id,provider,display_name,config_ref,enabled,max_concurrency,created_at) "
               "VALUES('p','claude','p','c',1,1,'now')")
    db.execute("INSERT INTO tasks(id,run_id,node_id,state,created_at,updated_at) VALUES('t1',?,'n','running','now','now')", (run,))
    for n in range(1, attempts + 1):
        db.execute("INSERT INTO task_attempts(id,task_id,attempt_no,profile_id,state) VALUES(?,?,?,?,?)",
                   ("a%d" % n, "t1", n, "p", "completed"))


def _observe(db, attempt, **values):
    row = {"id": "u-" + attempt, "attempt_id": attempt, "profile_id": "p", "source": "claude-cli", "confidence": "reported",
           "observed_at": "2026-11-20T00:00:00+00:00", "source_event_id": "e-" + attempt, **values}
    db.execute("INSERT INTO usage_observations(%s) VALUES(%s)" % (",".join(row), ",".join("?" * len(row))), tuple(row.values()))


def test_migration_0014_upgrades_0013_and_retains_rows(tmp_path):
    directory = _db_before_0014(tmp_path)
    with closing(open_database(tmp_path / "u.db")) as db:
        assert apply_migrations(db, directory)[-1] == 13
        _seed(db)
        _observe(db, "a1", input_tokens=3, output_tokens=4)
        before = db.execute("SELECT id,attempt_id,input_tokens,output_tokens FROM usage_observations").fetchall()
        source = ROOT / "migrations" / "0014_usage_ledger.sql"
        (directory / source.name).write_bytes(source.read_bytes())
        assert apply_migrations(db, directory) == [14]
        assert db.execute("SELECT id,attempt_id,input_tokens,output_tokens FROM usage_observations").fetchall() == before
        assert db.execute("SELECT cost_usd FROM usage_observations").fetchone() == (None,)  # retained rows: cost unknown
        db.execute("INSERT INTO usage_observations(id,attempt_id,profile_id,source,confidence,observed_at,source_event_id,cost_usd) "
                   "VALUES('new','a1','p','claude-cli','reported','now','e2',0.5)")


def test_ledger_rows_are_append_only_and_cost_is_non_negative(tmp_path):
    with closing(open_database(tmp_path / "ao.db")) as db:
        apply_migrations(db, ROOT / "migrations")
        _seed(db)
        _observe(db, "a1", input_tokens=1)
        with pytest.raises(sqlite3.DatabaseError, match="append-only"):
            db.execute("UPDATE usage_observations SET input_tokens=99")
        with pytest.raises(sqlite3.DatabaseError, match="append-only"):
            db.execute("DELETE FROM usage_observations")
        with pytest.raises(sqlite3.IntegrityError):
            _observe(db, "a1", source_event_id="other", cost_usd=-1.0)
        assert db.execute("SELECT input_tokens FROM usage_observations").fetchall() == [(1,)]


# ------------------------------------------------------------------ query
def test_usage_query_totals_only_known_values_and_reports_coverage(tmp_path):
    with closing(open_database(tmp_path / "q.db")) as db:
        apply_migrations(db, ROOT / "migrations")
        _seed(db, attempts=3)
        _observe(db, "a1", input_tokens=10, output_tokens=2, cache_read_tokens=100, cost_usd=0.25)
        _observe(db, "a2", input_tokens=5, output_tokens=None, cost_usd=None)
        # attempt a3 reported nothing at all
        usage = read_run_usage(db, "r1")
        assert not db.in_transaction
    assert usage["attempts_total"] == 3 and usage["attempts_with_usage"] == 2
    assert usage["totals"] == {"input_tokens": 15, "output_tokens": 2, "cache_read_tokens": 100,
                               "cache_write_tokens": None, "cost_usd": 0.25}  # nobody reported cache writes: unknown, not 0
    assert [o["attempt_id"] for o in usage["observations"]] == ["a1", "a2"]
    assert usage["observations"][1]["output_tokens"] is None and usage["observations"][0]["task_id"] == "t1"


def test_coverage_is_per_field_so_a_partial_total_is_visible(tmp_path):
    # a1 reported tokens AND cost, a2 only tokens: every attempt "has usage", yet the cost total covers 1 of 2.
    with closing(open_database(tmp_path / "cov.db")) as db:
        apply_migrations(db, ROOT / "migrations")
        _seed(db, attempts=3)
        _observe(db, "a1", input_tokens=10, output_tokens=2, cost_usd=0.25)
        _observe(db, "a2", input_tokens=5)
        _observe(db, "a3", input_tokens=1, output_tokens=1, cost_usd=0.0)  # a reported zero is a known value
        usage = read_run_usage(db, "r1")
    assert usage["attempts_total"] == 3 and usage["attempts_with_usage"] == 3  # the old counter cannot show the gap
    cov = usage["coverage"]
    assert cov["input_tokens"] == {"known": 3, "attempts": 3}
    assert cov["output_tokens"] == {"known": 2, "attempts": 3}
    assert cov["cost_usd"] == {"known": 2, "attempts": 3}
    assert cov["cache_read_tokens"] == {"known": 0, "attempts": 3} and usage["totals"]["cache_read_tokens"] is None
    assert usage["totals"]["cost_usd"] == 0.25 and set(cov) == set(usage["totals"])


def test_usage_query_for_a_run_without_observations_has_null_totals(tmp_path):
    with closing(open_database(tmp_path / "e.db")) as db:
        apply_migrations(db, ROOT / "migrations")
        _seed(db)
        usage = read_run_usage(db, "r1")
    assert usage["attempts_total"] == 1 and usage["attempts_with_usage"] == 0 and usage["observations"] == []
    assert set(usage["totals"].values()) == {None}


def test_usage_query_unknown_run(tmp_path):
    with closing(open_database(tmp_path / "n.db")) as db:
        apply_migrations(db, ROOT / "migrations")
        with pytest.raises(RunNotFound):
            read_run_usage(db, "nope")
        assert not db.in_transaction


# -------------------------------------------------------------------- API
def _client(reader):
    return TestClient(create_app(token=TOKEN, read_events=lambda *_: [], read_usage=reader), client=None)


def test_usage_endpoint_returns_the_ledger_and_maps_errors(tmp_path):
    path = tmp_path / "api.db"
    with closing(open_database(path)) as db:
        apply_migrations(db, ROOT / "migrations")
        _seed(db)
        _observe(db, "a1", input_tokens=7, cost_usd=0.1)

    def reader(run_id):  # a fresh connection per call, as the daemon's per-request readers do
        with closing(open_database(path)) as db:
            return read_run_usage(db, run_id)
    with _client(reader) as client:
        ok = client.get("/api/v1/runs/r1/usage", headers=HEADERS)
        missing = client.get("/api/v1/runs/nope/usage", headers=HEADERS)
        anon = client.get("/api/v1/runs/r1/usage")
        schema = client.get("/openapi.json", headers=HEADERS).json()
    assert ok.status_code == 200 and ok.json()["totals"]["input_tokens"] == 7 and ok.json()["totals"]["output_tokens"] is None
    assert missing.status_code == 404 and anon.status_code in (401, 403)
    from jsonschema import Draft202012Validator
    op = schema["paths"]["/api/v1/runs/{run_id}/usage"]["get"]
    Draft202012Validator(op["responses"]["200"]["content"]["application/json"]["schema"]).validate(ok.json())


def test_usage_endpoint_unavailable_without_a_reader():
    with _client(None) as client:
        assert client.get("/api/v1/runs/r1/usage", headers=HEADERS).status_code == 503


# -------------------------------------------------------------------- CLI
def test_cli_usage_command_requests_the_run_usage_endpoint(monkeypatch, capsys):
    seen = []
    monkeypatch.setattr(cli, "_request", lambda state_dir, endpoint, **kw: seen.append(endpoint) or {"run_id": "r1"})
    assert cli.main(["--state-dir", "/state", "usage", "r1"]) == 0
    assert seen == ["/api/v1/runs/r1/usage"] and json.loads(capsys.readouterr().out) == {"run_id": "r1"}
    with pytest.raises(SystemExit):
        cli.main(["--state-dir", "/state", "usage", "a/b"])
    assert len(seen) == 1
