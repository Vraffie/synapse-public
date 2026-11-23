from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect
import pytest

from synapse.api.app import create_app

TOKEN = "0123456789abcdef" * 2
HEADERS = {"Authorization": "Bearer " + TOKEN}
EVENTS = [{"sequence": 1, "id": "one"}, {"sequence": 3, "id": "three"}]


def reader(after, limit):
    return [event for event in EVENTS if event["sequence"] > after][:limit]


@pytest.fixture
def client():
    with TestClient(create_app(token=TOKEN, read_events=reader, poll_seconds=0.01), client=None) as c:
        yield c


def test_http_authentication_and_cursor(client):
    assert client.get("/api/v1/system/health").status_code == 401
    assert client.get("/api/v1/events", headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert client.get("/api/v1/events", headers={**HEADERS, "Origin": "https://example.com"}).status_code == 401
    response = client.get("/api/v1/events?after=1&limit=1", headers=HEADERS)
    assert response.json() == {"events": [EVENTS[1]]}
    assert response.headers["cache-control"] == "no-store"
    assert client.get("/api/v1/events?after=-1", headers=HEADERS).status_code == 422
    assert client.get("/api/v1/events?after=9223372036854775808", headers=HEADERS).status_code == 422
    assert client.get("/api/v1/events?limit=1001", headers=HEADERS).status_code == 422
    assert client.get("/api/v1/system/health", headers=HEADERS).json() == {"status": "ok"}


def test_rejects_tcp_even_with_valid_token():
    with TestClient(create_app(token=TOKEN, read_events=reader)) as c:
        assert c.get("/api/v1/system/health", headers=HEADERS).status_code == 401
        with pytest.raises(WebSocketDisconnect):
            with c.websocket_connect("/api/v1/events/stream", headers=HEADERS):
                pass


def test_websocket_replay_and_reconnect(client):
    with client.websocket_connect("/api/v1/events/stream?after=0", headers=HEADERS) as ws:
        assert ws.receive_json() == EVENTS[0]
        assert ws.receive_json() == EVENTS[1]
    with client.websocket_connect("/api/v1/events/stream?after=1", headers=HEADERS) as ws:
        assert ws.receive_json() == EVENTS[1]


def test_websocket_requires_header_and_valid_cursor(client):
    for path, headers in [
        ("/api/v1/events/stream?token=" + TOKEN, {}),
        ("/api/v1/events/stream?after=-1", HEADERS),
        ("/api/v1/events/stream?after=9223372036854775808", HEADERS),
        ("/api/v1/events/stream", {**HEADERS, "Origin": "http://localhost"}),
    ]:
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect(path, headers=headers):
                pass


def test_token_strength_and_openapi_are_guarded(client):
    with pytest.raises(ValueError):
        create_app(token="weak", read_events=reader)
    assert client.get("/openapi.json").status_code == 401
    assert client.get("/openapi.json", headers=HEADERS).status_code == 200


def test_http_over_real_unix_socket(tmp_path):
    import socket
    import threading
    import time
    import httpx
    import uvicorn

    path = str(tmp_path / "api.sock")
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.bind(path)
    sock.listen(8)
    server = uvicorn.Server(uvicorn.Config(create_app(token=TOKEN, read_events=reader), log_level="critical", lifespan="off"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 5
        while not server.started and thread.is_alive() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert server.started
        with httpx.Client(transport=httpx.HTTPTransport(uds=path), base_url="http://localhost") as c:
            assert c.get("/api/v1/events?after=1", headers=HEADERS).json() == {"events": [EVENTS[1]]}
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        sock.close()
    assert not thread.is_alive()


def test_websocket_accepts_max_sqlite_cursor(client):
    with client.websocket_connect("/api/v1/events/stream?after=9223372036854775807", headers=HEADERS):
        pass


def test_websocket_catch_up_does_not_idle_between_full_pages():
    events = [{"sequence": n, "id": str(n)} for n in range(1, 251)]
    app = create_app(token=TOKEN, read_events=lambda after, limit: [e for e in events if e["sequence"] > after][:limit],
                     poll_seconds=30)
    with TestClient(app, client=None) as c:
        with c.websocket_connect("/api/v1/events/stream?after=0", headers=HEADERS) as ws:
            # 250 events span three pages; a 30s idle per full page would time the test out.
            assert [ws.receive_json()["sequence"] for _ in range(200)] == list(range(1, 201))


def test_run_reader_data_fault_is_not_reported_as_missing_run():
    from synapse.persistence.run_snapshot import RunNotFound
    def reader_fn(run_id):
        if run_id == "missing":
            raise RunNotFound("Run does not exist")
        raise ValueError("corrupt row")
    app = create_app(token=TOKEN, read_events=reader, read_run=reader_fn, read_tasks=reader_fn)
    with TestClient(app, client=None, raise_server_exceptions=False) as c:
        assert c.get("/api/v1/runs/missing", headers=HEADERS).status_code == 404
        assert c.get("/api/v1/runs/broken", headers=HEADERS).status_code == 500
