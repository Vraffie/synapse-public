"""Keyset paging of GET /api/v1/runs/{id}/tasks and /usage: readers, cursors and the API surface."""
import base64
from contextlib import closing
import json
from pathlib import Path

from fastapi.testclient import TestClient
import pytest

from synapse.api.app import create_app
from synapse.cli import __main__ as cli
from synapse.desktop.mock_resume import MAX_EVIDENCE_TASKS, fetch_all_tasks
from synapse.persistence.migrations import apply_migrations, open_database
from synapse.persistence.paging import (
    MAX_PAGE_LIMIT, InvalidPageCursor, decode_page_cursor, encode_page_cursor, page_scope,
)
from synapse.persistence.run_snapshot import RunNotFound, read_run_tasks
from synapse.persistence.usage_query import read_run_usage

ROOT = Path(__file__).resolve().parents[1]
TOKEN = "t" * 43
HEADERS = {"Authorization": "Bearer " + TOKEN}
FIELDS = ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens", "cost_usd")


@pytest.fixture
def path(tmp_path):
    db_path = tmp_path / "paging.db"
    with closing(open_database(db_path)) as db:
        apply_migrations(db, ROOT / "migrations")
        db.execute("INSERT INTO workflow_versions VALUES('wf','1','{}','hash','now')")
        db.execute("INSERT INTO profiles(id,provider,display_name,config_ref,enabled,max_concurrency,created_at) "
                   "VALUES('p','claude','p','c',1,1,'now')")
        for run in ("run", "other", "empty"):
            db.execute("INSERT INTO runs VALUES(?,'wf','1','running',0,'now','now')", (run,))
    return db_path


@pytest.fixture
def db(path):
    with closing(open_database(path)) as connection:
        yield connection


def add_tasks(db, ids, run="run", attempts=1):
    for tid in ids:
        db.execute("INSERT INTO tasks(id,run_id,node_id,state,created_at,updated_at) VALUES(?,?,?,'created','now','now')",
                   (tid, run, "n-" + tid))
        for n in range(1, attempts + 1):
            db.execute("INSERT INTO task_attempts(id,task_id,attempt_no,profile_id,state) VALUES(?,?,?,'p','completed')",
                       (f"{tid}-a{n}", tid, n))


def observe(db, oid, attempt, observed_at, **values):
    row = {"id": oid, "attempt_id": attempt, "profile_id": "p", "source": "claude-cli", "confidence": "reported",
           "observed_at": observed_at, "source_event_id": "e-" + oid, **values}
    db.execute("INSERT INTO usage_observations(%s) VALUES(%s)" % (",".join(row), ",".join("?" * len(row))),
               tuple(row.values()))


def all_task_pages(db, run="run", limit=2):
    pages, cursor = [], None
    while True:
        page = read_run_tasks(db, run, limit=limit, cursor=cursor)
        pages.append(page)
        cursor = page["next_cursor"]
        if cursor is None:
            return pages


# ------------------------------------------------------------------ tasks reader
def test_tasks_first_next_last_page_in_id_order_with_attempts(db):
    add_tasks(db, ["t5", "t1", "t3", "t2", "t4"], attempts=2)
    add_tasks(db, ["x0", "x9"], run="other")
    pages = all_task_pages(db, limit=2)
    assert [[t["id"] for t in p["tasks"]] for p in pages] == [["t1", "t2"], ["t3", "t4"], ["t5"]]
    assert [p["next_cursor"] is None for p in pages] == [False, False, True]
    for page in pages:
        for task in page["tasks"]:
            assert [a["id"] for a in task["attempts"]] == [task["id"] + "-a1", task["id"] + "-a2"]
    assert not db.in_transaction


def test_tasks_exact_multiple_of_limit_ends_without_an_empty_page(db):
    add_tasks(db, ["t1", "t2", "t3", "t4"])
    pages = all_task_pages(db, limit=2)
    assert [len(p["tasks"]) for p in pages] == [2, 2] and pages[-1]["next_cursor"] is None


def test_tasks_empty_run_and_unknown_run(db):
    assert read_run_tasks(db, "empty") == {"tasks": [], "next_cursor": None}
    with pytest.raises(RunNotFound):
        read_run_tasks(db, "missing")
    assert not db.in_transaction


def test_tasks_default_is_one_bounded_page_never_the_whole_run(db):
    add_tasks(db, ["t%03d" % n for n in range(205)], attempts=0)
    page = read_run_tasks(db, "run")
    assert len(page["tasks"]) == 200 and page["next_cursor"] is not None
    rest = read_run_tasks(db, "run", cursor=page["next_cursor"])
    assert len(rest["tasks"]) == 5 and rest["next_cursor"] is None


def test_tasks_limit_one_and_max(db):
    add_tasks(db, ["t%03d" % n for n in range(MAX_PAGE_LIMIT + 1)], attempts=0)
    one = read_run_tasks(db, "run", limit=1)
    assert [t["id"] for t in one["tasks"]] == ["t000"] and one["next_cursor"]
    most = read_run_tasks(db, "run", limit=MAX_PAGE_LIMIT)
    assert len(most["tasks"]) == MAX_PAGE_LIMIT and most["next_cursor"]
    assert [t["id"] for t in read_run_tasks(db, "run", limit=MAX_PAGE_LIMIT, cursor=most["next_cursor"])["tasks"]] == ["t500"]


@pytest.mark.parametrize("limit", [0, -1, MAX_PAGE_LIMIT + 1, True, 1.0, "5"])
def test_readers_reject_limits_outside_range(db, limit):
    for reader in (read_run_tasks, read_run_usage):
        with pytest.raises(ValueError, match="Invalid page size"):
            reader(db, "run", limit=limit)
    assert not db.in_transaction


def test_tasks_new_task_between_pages_does_not_duplicate_or_skip_existing(db):
    add_tasks(db, ["t1", "t2", "t4", "t5"])
    first = read_run_tasks(db, "run", limit=2)
    add_tasks(db, ["t0", "t3"])  # t0 sorts before the cursor (not repeated), t3 after it (listed)
    second = read_run_tasks(db, "run", limit=2, cursor=first["next_cursor"])
    third = read_run_tasks(db, "run", limit=2, cursor=second["next_cursor"])
    seen = [t["id"] for p in (first, second, third) for t in p["tasks"]]
    assert seen == ["t1", "t2", "t3", "t4", "t5"] and third["next_cursor"] is None


# ------------------------------------------------------------------ cursors
def _forge(items):
    raw = json.dumps(items).encode()
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


@pytest.mark.parametrize("cursor", [
    "bad", "!!!!", "é" * 4, "a" * 1025, _forge({"k": "v"}), _forge(["only-scope"]), _forge([1, 2]),
    _forge(["x" * 300, "t1"]), _forge([page_scope("tasks", "run"), ""]),
    _forge([page_scope("tasks", "run"), "t1", "extra"]),
])
def test_bad_tampered_oversized_or_non_ascii_cursor_is_invalid(db, cursor):
    add_tasks(db, ["t1", "t2"])
    with pytest.raises(InvalidPageCursor):
        read_run_tasks(db, "run", cursor=cursor)
    with pytest.raises(InvalidPageCursor):
        read_run_usage(db, "run", cursor=cursor)
    assert not db.in_transaction


def test_cursor_from_another_run_or_route_is_rejected_and_leaks_nothing(db):
    add_tasks(db, ["a1", "a2"], run="other")
    add_tasks(db, ["b1", "b2", "b3"])
    other = read_run_tasks(db, "other", limit=1)["next_cursor"]
    with pytest.raises(InvalidPageCursor):
        read_run_tasks(db, "run", cursor=other)
    with pytest.raises(InvalidPageCursor):
        read_run_usage(db, "other", cursor=other)  # a tasks cursor is not a usage cursor
    # Even a cursor forged with this run's scope only ever filters this run's rows.
    forged = encode_page_cursor(page_scope("tasks", "run"), "a0")
    assert [t["id"] for t in read_run_tasks(db, "run", cursor=forged)["tasks"]] == ["b1", "b2", "b3"]


def test_cursor_round_trip_and_limits():
    cursor = encode_page_cursor("s" * 16, "2026-11-20T00:00:00+00:00", "0" * 36)
    assert cursor.isascii() and len(cursor) <= 1024
    assert decode_page_cursor(cursor, "s" * 16, 2) == ["2026-11-20T00:00:00+00:00", "0" * 36]
    with pytest.raises(InvalidPageCursor):
        decode_page_cursor(cursor, "t" * 16, 2)
    with pytest.raises(ValueError):
        encode_page_cursor("s", "x" * 257)
    with pytest.raises(ValueError):  # never issue a cursor the decoder would refuse
        encode_page_cursor("s", "\U0001F600" * 256)


# ------------------------------------------------------------------ usage reader
def _seed_usage(db):
    add_tasks(db, ["t1", "t2"], attempts=2)
    add_tasks(db, ["f1"], run="other")
    observe(db, "o3", "t1-a1", "2026-11-20T00:00:02+00:00", input_tokens=5, cost_usd=0.25)
    observe(db, "o1", "t1-a1", "2026-11-20T00:00:01+00:00", input_tokens=1)
    observe(db, "o2", "t1-a2", "2026-11-20T00:00:01+00:00", input_tokens=2, output_tokens=0)
    observe(db, "o4", "t2-a1", "2026-11-20T00:00:03+00:00")
    observe(db, "o5", "t2-a1", "2026-11-20T00:00:04+00:00", input_tokens=3, cost_usd=0.5)
    observe(db, "f", "f1-a1", "2026-11-20T00:00:00+00:00", input_tokens=1000, cache_read_tokens=7)


def test_usage_totals_are_whole_run_on_every_page_and_only_observations_page(db):
    _seed_usage(db)
    unpaged = read_run_usage(db, "run", limit=MAX_PAGE_LIMIT)
    assert unpaged["next_cursor"] is None and len(unpaged["observations"]) == 5
    pages, cursor = [], None
    while True:
        page = read_run_usage(db, "run", limit=2, cursor=cursor)
        pages.append(page)
        cursor = page["next_cursor"]
        if cursor is None:
            break
    assert [[o["id"] for o in p["observations"]] for p in pages] == [["o1", "o2"], ["o3", "o4"], ["o5"]]
    for page in pages:
        for key in ("run_id", "attempts_total", "attempts_with_usage", "totals", "coverage"):
            assert page[key] == unpaged[key], key
    assert unpaged["totals"] == {"input_tokens": 11, "output_tokens": 0, "cache_read_tokens": None,
                                 "cache_write_tokens": None, "cost_usd": 0.75}
    assert unpaged["attempts_total"] == 4 and unpaged["attempts_with_usage"] == 3
    assert unpaged["coverage"]["input_tokens"] == {"known": 3, "attempts": 4}
    assert unpaged["coverage"]["output_tokens"] == {"known": 1, "attempts": 4}
    assert unpaged["coverage"]["cache_read_tokens"] == {"known": 0, "attempts": 4}
    o4 = pages[1]["observations"][1]
    assert all(o4[f] is None for f in FIELDS)  # unknown stays unknown per observation, never 0


def test_usage_totals_match_python_sum_of_all_observations(db):
    _seed_usage(db)
    every = read_run_usage(db, "run", limit=MAX_PAGE_LIMIT)
    for field in FIELDS:
        known = [o[field] for o in every["observations"] if o[field] is not None]
        assert every["totals"][field] == (sum(known) if known else None)
        assert every["coverage"][field]["known"] == len({o["attempt_id"] for o in every["observations"] if o[field] is not None})


def test_usage_empty_run_keeps_unknown_none_and_unknown_run_raises(db):
    add_tasks(db, ["e1"], run="empty")
    usage = read_run_usage(db, "empty")
    assert usage["observations"] == [] and usage["next_cursor"] is None
    assert usage["totals"] == dict.fromkeys(FIELDS) and usage["attempts_with_usage"] == 0
    assert all(usage["coverage"][f] == {"known": 0, "attempts": 1} for f in FIELDS)
    with pytest.raises(RunNotFound):
        read_run_usage(db, "missing")


def test_usage_new_observation_between_pages(db):
    _seed_usage(db)
    first = read_run_usage(db, "run", limit=2)
    observe(db, "o6", "t2-a2", "2026-11-20T00:00:09+00:00", input_tokens=100)
    observe(db, "o0", "t2-a2", "2026-11-20T00:00:00+00:00", input_tokens=1000)  # sorts before the cursor
    rest, cursor = [], first["next_cursor"]
    while cursor is not None:
        page = read_run_usage(db, "run", limit=2, cursor=cursor)
        rest.append(page)
        cursor = page["next_cursor"]
    ids = [o["id"] for p in [first, *rest] for o in p["observations"]]
    assert ids == ["o1", "o2", "o3", "o4", "o5", "o6"]  # no duplicates, nothing already seen is repeated
    assert rest[-1]["totals"]["input_tokens"] == 1111  # totals are a fresh whole-run snapshot per page


# ------------------------------------------------------------------ API
def _client(path):
    def tasks(run_id, limit, cursor):
        with closing(open_database(path)) as db:
            return read_run_tasks(db, run_id, limit=limit, cursor=cursor)

    def usage(run_id, limit, cursor):
        with closing(open_database(path)) as db:
            return read_run_usage(db, run_id, limit=limit, cursor=cursor)
    return TestClient(create_app(token=TOKEN, read_events=lambda *_: [], read_tasks=tasks, read_usage=usage), client=None)


def test_api_pages_validates_and_maps_errors(path):
    with closing(open_database(path)) as db:
        _seed_usage(db)
        add_tasks(db, ["t3"], attempts=0)
    with _client(path) as client:
        for route, key in (("tasks", "tasks"), ("usage", "observations")):
            url = f"/api/v1/runs/run/{route}"
            first = client.get(url, params={"limit": 2}, headers=HEADERS)
            assert first.status_code == 200 and len(first.json()[key]) == 2 and first.json()["next_cursor"]
            second = client.get(url, params={"limit": 2, "cursor": first.json()["next_cursor"]}, headers=HEADERS)
            assert second.status_code == 200
            assert not {x["id"] for x in first.json()[key]} & {x["id"] for x in second.json()[key]}
            for limit in (0, 501, -1, "x"):
                assert client.get(url, params={"limit": limit}, headers=HEADERS).status_code == 422
            assert client.get(url, params={"limit": 1}, headers=HEADERS).status_code == 200
            assert client.get(url, params={"limit": 500}, headers=HEADERS).status_code == 200
            for cursor in ("bad", "", "c" * 1025, "é", _forge([page_scope(route, "other"), "zzz"]),
                           _forge([page_scope(route, "run"), "zzz"] + (["x"] if route == "tasks" else []))):
                r = client.get(url, params={"cursor": cursor}, headers=HEADERS)
                assert r.status_code == 422, (route, cursor)
                assert len(cursor) < 4 or cursor not in r.text
            other = client.get(f"/api/v1/runs/other/{route}", params={"limit": 1}, headers=HEADERS).json()
            if other["next_cursor"]:
                assert client.get(url, params={"cursor": other["next_cursor"]}, headers=HEADERS).status_code == 422
            assert client.get(f"/api/v1/runs/missing/{route}", headers=HEADERS).status_code == 404
            assert client.get(f"/api/v1/runs/%20/{route}", headers=HEADERS).status_code == 422
            assert client.get(url).status_code in (401, 403)


def test_api_503_without_readers():
    with TestClient(create_app(token=TOKEN, read_events=lambda *_: []), client=None) as client:
        for route in ("tasks", "usage"):
            assert client.get(f"/api/v1/runs/run/{route}", headers=HEADERS).status_code == 503


# ------------------------------------------------------------------ CLI and desktop consumers
@pytest.mark.parametrize("command", ["tasks", "usage"])
def test_cli_passes_limit_and_cursor_and_flags_more_pages(monkeypatch, capsys, command):
    seen = []
    monkeypatch.setattr(cli, "_request", lambda state_dir, endpoint, **kw: seen.append(endpoint) or {"next_cursor": "abc_-1"})
    assert cli.main(["--state-dir", "/s", command, "r1", "--limit", "5", "--cursor", "abc"]) == 0
    assert seen == [f"/api/v1/runs/r1/{command}?limit=5&cursor=abc"]
    out = capsys.readouterr()
    assert json.loads(out.out) == {"next_cursor": "abc_-1"} and "--cursor abc_-1" in out.err
    monkeypatch.setattr(cli, "_request", lambda state_dir, endpoint, **kw: {"next_cursor": "\x1b[2Jevil"})
    assert cli.main(["--state-dir", "/s", command, "r1"]) == 0
    assert "\x1b" not in capsys.readouterr().err
    for bad in ("0", "501"):
        with pytest.raises(SystemExit):
            cli.main(["--state-dir", "/s", command, "r1", "--limit", bad])


def test_cli_last_page_prints_no_hint(monkeypatch, capsys):
    monkeypatch.setattr(cli, "_request", lambda state_dir, endpoint, **kw: {"tasks": [], "next_cursor": None})
    assert cli.main(["--state-dir", "/s", "tasks", "r1"]) == 0
    assert capsys.readouterr().err == ""


def test_desktop_evidence_follows_every_task_page():
    pages = {None: {"tasks": [{"id": "a"}], "next_cursor": "c1"}, "c1": {"tasks": [{"id": "b"}], "next_cursor": None}}
    calls = []

    def fetch(_state, endpoint):
        calls.append(endpoint)
        cursor = endpoint.split("cursor=")[1] if "cursor=" in endpoint else None
        return pages[cursor]
    assert fetch_all_tasks(fetch, "/s", "/api/v1/runs/r") == {"tasks": [{"id": "a"}, {"id": "b"}]}
    assert calls == ["/api/v1/runs/r/tasks?limit=500", "/api/v1/runs/r/tasks?limit=500&cursor=c1"]


@pytest.mark.parametrize("page", [
    [], {"tasks": None}, {"tasks": [], "next_cursor": "c"}, {"tasks": [{"id": "a"}], "next_cursor": 5},
    {"tasks": [{"id": "a"}], "next_cursor": ""},
])
def test_desktop_evidence_fails_closed_on_bad_pages(page):
    with pytest.raises(ValueError):
        fetch_all_tasks(lambda *_: page, "/s", "/api/v1/runs/r")


def test_desktop_evidence_is_bounded_against_an_endless_daemon():
    calls = []

    def fetch(_state, endpoint):
        calls.append(endpoint)
        return {"tasks": [{"id": str(len(calls))}] * 400, "next_cursor": "same"}
    with pytest.raises(ValueError, match="Too many"):
        fetch_all_tasks(fetch, "/s", "/api/v1/runs/r")
    assert len(calls) == MAX_EVIDENCE_TASKS // 400 + 1
