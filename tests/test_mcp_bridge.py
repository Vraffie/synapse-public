"""MCP SDK client exercises a stdio subprocess and daemon-owned IPC handler."""
import asyncio
import json
import os
from pathlib import Path
import sys

import pytest

pytest.importorskip('mcp')
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from synapse.application.message_gateway import MessageGateway
from synapse.application.session_authority import SessionAuthority
from synapse.mcp_gateway.ipc import MAX_LINE, handle_ipc, private_ipc_server
from test_message_store import db


def test_real_stdio_mcp_client_uses_daemon_owned_gateway(db, tmp_path):
    async def exercise():
        authority = SessionAuthority()
        sender = authority.issue(instance_id='sender', run_id='run', grants=[('message.send', 'thread'), ('message.recipient', 'receiver')])
        gateway = MessageGateway(db, authority)
        path = tmp_path / 'agent-ipc.sock'
        context = private_ipc_server(path, gateway)
        await context.__aenter__()
        env = dict(os.environ)
        env['SYNAPSE_MCP_SOCKET'] = str(path)
        env['SYNAPSE_MCP_CREDENTIAL'] = sender.secret
        params = StdioServerParameters(command=sys.executable, args=['-m', 'synapse.mcp_gateway'], env=env)
        try:
            async with stdio_client(params) as streams:
                async with ClientSession(*streams) as session:
                    await session.initialize()
                    tools = await session.list_tools()
                    assert {tool.name for tool in tools.tools} == {'send_message', 'get_inbox', 'read_delivery', 'ack_delivery'}
                    request = {'idempotency_key': 's', 'message_id': 'msg', 'thread_id': 'thread',
                               'type': 'question', 'payload': {'text': 'hello'},
                               'created_at': '2026-11-19T12:00:00Z', 'recipients': ['receiver']}
                    response = await session.call_tool('send_message', request)
                    assert not response.isError
                    payload = json.loads(response.content[0].text)
                    assert payload['replayed'] is False and len(payload['response']) == 1
                    request['sender_instance_id'] = 'foreign'
                    denied = await session.call_tool('send_message', request)
                    assert denied.isError
                    assert db.execute('SELECT sender_instance_id FROM messages').fetchone() == ('sender',)
                    authority.revoke(sender.identity.principal)
                    request.pop('sender_instance_id')
                    denied = await session.call_tool('send_message', request)
                    assert denied.isError and sender.secret not in denied.content[0].text
            receiver = authority.issue(instance_id='receiver', run_id='run', grants=[('inbox.claim', 'receiver')])
            env['SYNAPSE_MCP_CREDENTIAL'] = receiver.secret
            receiver_params = StdioServerParameters(command=sys.executable, args=['-m', 'synapse.mcp_gateway'], env=env)
            async with stdio_client(receiver_params) as streams:
                async with ClientSession(*streams) as session:
                    await session.initialize()
                    assert (await session.call_tool('get_inbox', {})).isError
                    first = await session.call_tool('get_inbox', {'idempotency_key': 'poll'})
                    second = await session.call_tool('get_inbox', {'idempotency_key': 'poll'})
                    one = json.loads(first.content[0].text)
                    two = json.loads(second.content[0].text)
                    assert not first.isError and not second.isError
                    assert one['replayed'] is False and two['replayed'] is True
                    assert one['response'] == two['response']
                    assert db.execute('SELECT attempts FROM message_deliveries').fetchone() == (1,)
        finally:
            await context.__aexit__(None, None, None)
        assert not path.exists()
    asyncio.run(exercise())


def test_ipc_rejects_bad_frames_without_exposing_errors(db, tmp_path):
    async def exercise():
        gateway = MessageGateway(db, SessionAuthority())
        path = tmp_path / 'socket'
        server = await asyncio.start_unix_server(lambda r, w: handle_ipc(r, w, gateway), path=path, limit=MAX_LINE)
        try:
            for raw in (b'{"credential":"bad","credential":"bad","operation":"claim","arguments":{}}\n',
                        b'{"credential":"bad","operation":"claim","arguments":{"limit":1e999}}\n',
                        b'{}\n'):
                reader, writer = await asyncio.open_unix_connection(str(path))
                writer.write(raw)
                await writer.drain()
                response = json.loads(await reader.readline())
                assert response == {'ok': False, 'error': 'request failed'}
                writer.close()
                await writer.wait_closed()
            assert db.execute('SELECT COUNT(*) FROM messages').fetchone() == (0,)
        finally:
            server.close()
            await server.wait_closed()
    asyncio.run(exercise())


def test_ipc_server_does_not_replace_existing_path_and_checks_directory(db, tmp_path):
    async def exercise():
        gateway = MessageGateway(db, SessionAuthority())
        path = tmp_path / 'busy'
        path.write_text('do not remove')
        with pytest.raises(OSError):
            async with private_ipc_server(path, gateway):
                pass
        assert path.read_text() == 'do not remove'
        tmp_path.chmod(0o755)
        with pytest.raises(PermissionError):
            async with private_ipc_server(tmp_path / 'socket', gateway):
                pass
    asyncio.run(exercise())


def test_ipc_capacity_and_oversized_frame(db, tmp_path):
    async def exercise():
        gateway = MessageGateway(db, SessionAuthority())
        path = tmp_path / 'socket'
        async with private_ipc_server(path, gateway, max_connections=1):
            first_reader, first = await asyncio.open_unix_connection(str(path))
            first.write(b'{')
            await first.drain()
            await asyncio.sleep(0.02)
            second_reader, second = await asyncio.open_unix_connection(str(path))
            assert await asyncio.wait_for(second_reader.read(), timeout=2) == b''
            second.close()
            await second.wait_closed()
            first.close()
            await first.wait_closed()
            await asyncio.sleep(0.02)
            reader, writer = await asyncio.open_unix_connection(str(path))
            writer.write(b'x' * (MAX_LINE + 1) + b'\n')
            await writer.drain()
            assert json.loads(await asyncio.wait_for(reader.readline(), timeout=2)) == {'ok': False, 'error': 'request failed'}
            writer.close()
            await writer.wait_closed()
    asyncio.run(exercise())


def test_authenticated_direct_ipc_cannot_claim_without_idempotency_key(db, tmp_path):
    async def exercise():
        auth = SessionAuthority()
        receiver = auth.issue(instance_id='receiver', run_id='run', grants=[('inbox.claim', 'receiver')])
        gateway = MessageGateway(db, auth)
        path = tmp_path / 'socket'
        async with private_ipc_server(path, gateway):
            for arguments in ({}, {'idempotency_key': None}):
                reader, writer = await asyncio.open_unix_connection(str(path))
                frame = {'credential': receiver.secret, 'operation': 'claim', 'arguments': arguments}
                writer.write(json.dumps(frame).encode() + b'\n')
                await writer.drain()
                assert json.loads(await reader.readline()) == {'ok': False, 'error': 'request failed'}
                writer.close()
                await writer.wait_closed()
            assert db.execute("SELECT COUNT(*) FROM idempotency_keys WHERE operation='delivery.claim'").fetchone() == (0,)
    asyncio.run(exercise())
