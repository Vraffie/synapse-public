"""Size bounds of the MCP gateway IPC client (`_invoke`) and server (`handle_ipc`) that had no direct test."""
import asyncio
import os
import socket

import pytest

pytest.importorskip("mcp")
from synapse.mcp_gateway import __main__ as bridge
from synapse.mcp_gateway.ipc import MAX_LINE, MAX_RESPONSE, handle_ipc


class StubGateway:
    def __init__(self, result="ok"):
        self.result, self.calls = result, 0

    def invoke(self, credential, operation, raw):
        self.calls += 1
        return self.result


def _private_dir(tmp_path):
    d = tmp_path / "ipc"
    d.mkdir(mode=0o700)
    os.chmod(d, 0o700)
    return d


async def _serve(path, handler):
    sock = socket.socket(socket.AF_UNIX)
    previous = os.umask(0o177)
    try:
        sock.bind(str(path))
    finally:
        os.umask(previous)
    os.chmod(path, 0o600)
    sock.listen(1)
    sock.setblocking(False)
    return await asyncio.start_unix_server(handler, sock=sock, limit=MAX_LINE)


def test_client_refuses_an_oversized_request_before_connecting(tmp_path):
    path = _private_dir(tmp_path) / "s"

    async def go():
        server = await _serve(path, lambda r, w: handle_ipc(r, w, StubGateway()))
        try:
            with pytest.raises(ValueError, match="request too large"):
                await bridge._invoke(path, "c", "op", {"text": "x" * MAX_LINE})
        finally:
            server.close()
            await server.wait_closed()
    asyncio.run(go())


def test_server_turns_an_oversized_result_into_a_generic_failure(tmp_path):
    path = _private_dir(tmp_path) / "s"
    gateway = StubGateway(result="y" * MAX_RESPONSE)

    async def go():
        server = await _serve(path, lambda r, w: handle_ipc(r, w, gateway))
        try:
            with pytest.raises(ValueError, match="request failed"):  # generic: no size detail leaks
                await bridge._invoke(path, "c", "op", {})
        finally:
            server.close()
            await server.wait_closed()
    asyncio.run(go())
    assert gateway.calls == 1


@pytest.mark.parametrize("reply", [b"x" * (MAX_RESPONSE + 10) + b"\n", b'{"ok":true,"result":1}'])
def test_client_rejects_an_overlong_or_unterminated_response(tmp_path, reply):
    path = _private_dir(tmp_path) / "s"

    async def rogue(reader, writer):
        await reader.readline()
        writer.write(reply)
        await writer.drain()
        writer.close()

    async def go():
        server = await _serve(path, rogue)
        try:
            with pytest.raises((ValueError, asyncio.LimitOverrunError)):
                await bridge._invoke(path, "c", "op", {})
        finally:
            server.close()
            await server.wait_closed()
    asyncio.run(go())


def test_client_accepts_a_normal_response(tmp_path):
    path = _private_dir(tmp_path) / "s"

    async def go():
        server = await _serve(path, lambda r, w: handle_ipc(r, w, StubGateway(result={"n": 1})))
        try:
            return await bridge._invoke(path, "c", "op", {})
        finally:
            server.close()
            await server.wait_closed()
    assert asyncio.run(go()) == {"n": 1}
