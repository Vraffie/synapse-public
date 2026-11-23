"""MCP stdio child proxy: no SQLite access and no daemon authority object."""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import stat
import sys

from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

from synapse.mcp_gateway.ipc import MAX_LINE, MAX_RESPONSE
from synapse.mcp_gateway.bounded_stdio import BoundedStdin

# These are the exact arguments accepted by the daemon's scoped gateway.
_TOOLS = {
    'send_message': ('send', ['idempotency_key', 'message_id', 'thread_id', 'type', 'payload', 'created_at', 'recipients'],
                     {'idempotency_key': 'string', 'message_id': 'string', 'thread_id': 'string', 'type': 'string', 'payload': 'object', 'created_at': 'string', 'recipients': 'array', 'artifact_refs': 'array', 'correlation_id': 'string', 'causation_id': 'string'}),
    'get_inbox': ('claim', ['idempotency_key'], {'idempotency_key': 'string', 'limit': 'integer'}),
    'read_delivery': ('read', ['delivery_id'], {'delivery_id': 'string'}),
    'ack_delivery': ('complete', ['idempotency_key', 'delivery_id', 'token', 'success'],
                     {'idempotency_key': 'string', 'delivery_id': 'string', 'token': 'string', 'success': 'boolean', 'result_artifact_id': 'string'}),
}


def _trusted_socket(path: Path):
    parent = path.parent.lstat()
    info = path.lstat()
    uid = os.geteuid()
    if (not stat.S_ISDIR(parent.st_mode) or parent.st_uid != uid or parent.st_mode & 0o077
            or not stat.S_ISSOCK(info.st_mode) or info.st_uid != uid or info.st_mode & 0o077):
        raise PermissionError('IPC socket is not private')


async def _invoke(path, credential, operation, arguments):
    _trusted_socket(path)
    request = json.dumps({'credential': credential, 'operation': operation, 'arguments': arguments},
                         allow_nan=False, separators=(',', ':')).encode('utf-8') + b'\n'
    if len(request) > MAX_LINE:
        raise ValueError('request too large')
    reader, writer = await asyncio.wait_for(asyncio.open_unix_connection(str(path), limit=MAX_RESPONSE), timeout=5)
    try:
        writer.write(request)
        await asyncio.wait_for(writer.drain(), timeout=5)
        line = await asyncio.wait_for(reader.readline(), timeout=10)
        if not line.endswith(b'\n') or len(line) > MAX_RESPONSE:
            raise ValueError('invalid response')
        response = json.loads(line)
        if not isinstance(response, dict) or response.get('ok') is not True or 'result' not in response:
            raise ValueError('request failed')
        return response['result']
    finally:
        writer.close()
        await writer.wait_closed()


def create_server(path: Path, credential: str):
    server = Server('synapse-agent-messaging')

    @server.list_tools()
    async def list_tools():
        return [Tool(name=name, description='Scoped Synapse agent messaging',
                     inputSchema={'type': 'object', 'properties': {key: {'type': kind} for key, kind in props.items()},
                                  'required': required, 'additionalProperties': False})
                for name, (_, required, props) in _TOOLS.items()]

    @server.call_tool()
    async def call_tool(name: str, arguments: dict):
        try:
            if name not in _TOOLS or not isinstance(arguments, dict):
                raise ValueError('invalid tool')
            result = await _invoke(path, credential, _TOOLS[name][0], arguments)
            return [TextContent(type='text', text=json.dumps(result, ensure_ascii=False))]
        except Exception:
            # Suppress raw input, credentials, server exceptions and tracebacks.
            raise ValueError('Request failed') from None
    return server


async def run():
    socket_path = os.environ.pop('SYNAPSE_MCP_SOCKET', None)
    credential = os.environ.pop('SYNAPSE_MCP_CREDENTIAL', None)
    if not socket_path or not credential:
        raise RuntimeError('Trusted launcher configuration is required')
    path = Path(socket_path)
    if not path.is_absolute():
        raise ValueError('IPC socket path must be absolute')
    _trusted_socket(path)
    server = create_server(path, credential)
    async with stdio_server(stdin=BoundedStdin(sys.stdin.fileno())) as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == '__main__':
    asyncio.run(run())
