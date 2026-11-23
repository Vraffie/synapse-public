"""GET /runs/{id}/analytics and `agentctl analytics`: the library view served unchanged, provenance intact."""
import json
import re
import sqlite3

import pytest
from fastapi.testclient import TestClient

from synapse.api.app import create_app
from synapse.application.analytics_views import AnalyticsTooLarge, read_run_analytics
from synapse.cli.__main__ import main
from test_analytics_views import populated  # noqa: F401
from test_message_store import db  # noqa: F401  (run 'run': 3 attempts, estimates, messages)
from test_cli import daemon, state  # noqa: F401

TOKEN = "t" * 40
AUTH = {"Authorization": "Bearer " + TOKEN}
NUMBER_KEYS = {"kind", "value", "unit", "basis", "known", "of", "lower_bound"}


def _walk_quantities(node):
    if isinstance(node, dict):
        if "kind" in node and "lower_bound" in node:
            yield node
        else:
            for value in node.values():
                yield from _walk_quantities(value)
    elif isinstance(node, list):
        for value in node:
            yield from _walk_quantities(value)


@pytest.fixture
def client(populated):  # noqa: F811
    path = populated.execute("PRAGMA database_list").fetchone()[2]

    def reader(run_id, limit, cursor):
        connection = sqlite3.connect(path, isolation_level=None)
        try:
            return read_run_analytics(connection, run_id, limit=limit, cursor=cursor).to_dict()
        finally:
            connection.close()

    with TestClient(create_app(token=TOKEN, read_events=lambda a, b: [], read_analytics=reader),
                    client=None, raise_server_exceptions=False) as c:
        yield c


def test_happy_path_every_number_has_provenance(client):
    r = client.get("/api/v1/runs/run/analytics", headers=AUTH)
    assert r.status_code == 200
    body = r.json()
    quantities = list(_walk_quantities(body))
    assert quantities and all(set(q) == NUMBER_KEYS for q in quantities)
    assert body["attempts"]["value"] == 3 and body["attempts"]["kind"] == "measured"
    assert body["next_cursor"] is None and body["source"] == "tables"


def test_estimated_never_merged_into_measured_and_unknown_stays_null(client):
    body = client.get("/api/v1/runs/run/analytics", headers=AUTH).json()
    est = body["estimated"]["chars/4"]["est_tokens"]
    assert (est["kind"], est["value"]) == ("estimated", 150)
    assert "est_tokens" not in body["usage"]
    assert body["usage"]["input_tokens"]["kind"] == "measured" and body["usage"]["input_tokens"]["value"] == 31
    cache = body["usage"]["cache_read_tokens"]
    assert cache["kind"] == "unknown" and cache["value"] is None and cache["lower_bound"] is False
    assert all(q["kind"] != "estimated" for q in _walk_quantities({k: v for k, v in body.items() if k != "estimated"}))
    assert all(q["value"] is None for q in _walk_quantities(body) if q["kind"] == "unknown")


def test_task_paging_follows_next_cursor(client):
    first = client.get("/api/v1/runs/run/analytics", headers=AUTH, params={"limit": 1}).json()
    assert [t["task_id"] for t in first["tasks"]] == ["t1"] and first["next_cursor"]
    second = client.get("/api/v1/runs/run/analytics", headers=AUTH,
                        params={"limit": 1, "cursor": first["next_cursor"]}).json()
    assert [t["task_id"] for t in second["tasks"]] == ["t2"] and second["next_cursor"] is None
    assert second["attempts"] == first["attempts"]  # run-level totals are not paged


def test_unknown_run_is_404(client):
    assert client.get("/api/v1/runs/missing/analytics", headers=AUTH).status_code == 404


def test_blank_run_id_is_422(client):
    assert client.get("/api/v1/runs/%20/analytics", headers=AUTH).status_code == 422


@pytest.mark.parametrize("params", [{"limit": 0}, {"limit": 201}, {"limit": "x"}, {"cursor": ""},
                                    {"cursor": "!!bad!!"}, {"cursor": "c" * 1025}])
def test_bad_limit_or_cursor_is_422_without_echo(client, params):
    r = client.get("/api/v1/runs/run/analytics", headers=AUTH, params=params)
    assert r.status_code == 422
    assert "bad!!" not in r.text and "ccccc" not in r.text


def test_no_reader_is_503_and_auth_required():
    with TestClient(create_app(token=TOKEN, read_events=lambda a, b: []), client=None, raise_server_exceptions=False) as c:
        assert c.get("/api/v1/runs/run/analytics", headers=AUTH).status_code == 503
        assert c.get("/api/v1/runs/run/analytics").status_code == 401


# ------------------------------------------------------------------ CLI
def test_cli_prints_json_and_cursor_on_stderr(state, capsys):  # noqa: F811
    body = json.dumps({"run_id": "r", "next_cursor": "abc_-123\x1b[31m\n\x00evil"}).encode()  # not base64url: refused
    with daemon(state, body) as received:
        assert main(["--state-dir", str(state), "analytics", "r", "--limit", "7", "--cursor", "c1"]) == 0
    out = capsys.readouterr()
    assert received[0].startswith(b"GET /api/v1/runs/r/analytics?limit=7&cursor=c1 HTTP/1.1")
    assert json.loads(out.out)["run_id"] == "r"
    assert out.err.strip() == "agentctl: more tasks exist; next page: --cursor (invalid)"
    assert not re.search(r"[\x00-\x1f\x7f]", out.err.strip())


def test_cli_prints_a_clean_next_cursor_on_stderr(state, capsys):  # noqa: F811
    with daemon(state, b'{"next_cursor": "abc_-123"}'):
        assert main(["--state-dir", str(state), "analytics", "r"]) == 0
    assert capsys.readouterr().err.strip() == "agentctl: more tasks exist; next page: --cursor abc_-123"


def test_cli_last_page_has_no_stderr_and_default_limit(state, capsys):  # noqa: F811
    with daemon(state, b'{"next_cursor": null}') as received:
        assert main(["--state-dir", str(state), "analytics", "run one"]) == 0
    out = capsys.readouterr()
    assert received[0].startswith(b"GET /api/v1/runs/run%20one/analytics?limit=50 HTTP/1.1") and out.err == ""


@pytest.mark.parametrize("args", [["--limit", "0"], ["--limit", "201"], ["--limit", "x"]])
def test_cli_rejects_bad_limit_before_connecting(state, args):  # noqa: F811
    with pytest.raises(SystemExit) as error:
        main(["--state-dir", str(state), "analytics", "r", *args])
    assert error.value.code == 2


def test_cross_run_cursor_is_422_through_the_api(populated, client):  # noqa: F811
    populated.execute("INSERT INTO tasks(id,run_id,node_id,state,created_at,updated_at) VALUES('x1','other','n','created','now','now')")
    first = client.get("/api/v1/runs/run/analytics", headers=AUTH, params={"limit": 1}).json()["next_cursor"]
    r = client.get("/api/v1/runs/other/analytics", headers=AUTH, params={"cursor": first})
    assert r.status_code == 422 and first not in r.text


def test_too_large_run_is_422_with_fixed_message():
    def reader(run_id, limit, cursor):
        raise AnalyticsTooLarge("Run has more than 20000 attempts SECRET-DETAIL")
    with TestClient(create_app(token=TOKEN, read_events=lambda a, b: [], read_analytics=reader),
                    client=None, raise_server_exceptions=False) as c:
        r = c.get("/api/v1/runs/run/analytics", headers=AUTH)
    assert r.status_code == 422 and "SECRET-DETAIL" not in r.text and "Run too large for analytics" in r.text


def test_cli_rejects_empty_cursor_before_connecting(state):  # noqa: F811
    with pytest.raises(SystemExit) as error:
        main(["--state-dir", str(state), "analytics", "r", "--cursor", ""])
    assert error.value.code == 2
