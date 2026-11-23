"""Bounded daemon-side session gateway IPC handler.

Only the daemon may construct the gateway. The caller owns secure socket
binding and shutdown and must use the SQLite connection's owning event loop.
"""
import asyncio
from dataclasses import asdict, is_dataclass
import json

from synapse.infrastructure.daemon_lock import daemon_lock
from synapse.infrastructure.socket_reclaim import reclaim_stale_socket
from synapse.application.message_gateway import _finite_float, _reject_constant, _unique_pairs

MAX_LINE = 140000
MAX_RESPONSE = 1048576


def _json_value(value):
    if is_dataclass(value) and not isinstance(value, type):
        return asdict(value)
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    return value


async def handle_ipc(reader, writer, gateway):
    """Handle one request; no client can choose principal or daemon resources."""
    try:
        line = await asyncio.wait_for(reader.readline(), timeout=5)
        if not line.endswith(b'\n') or len(line) > MAX_LINE:
            raise ValueError('invalid frame')
        request = json.loads(line, object_pairs_hook=_unique_pairs, parse_constant=_reject_constant, parse_float=_finite_float)
        if not isinstance(request, dict) or set(request) != {'credential', 'operation', 'arguments'}:
            raise ValueError('invalid envelope')
        if not isinstance(request['arguments'], dict):
            raise ValueError('invalid arguments')
        raw = json.dumps(request['arguments'], allow_nan=False, separators=(',', ':')).encode('utf-8')
        result = gateway.invoke(request['credential'], request['operation'], raw)
        response = {'ok': True, 'result': _json_value(result)}
        encoded = json.dumps(response, allow_nan=False, separators=(',', ':')).encode('utf-8') + b'\n'
        if len(encoded) > MAX_RESPONSE:
            raise ValueError('response too large')
    except Exception:
        encoded = b'{"ok":false,"error":"request failed"}\n'
    try:
        writer.write(encoded)
        await asyncio.wait_for(writer.drain(), timeout=5)
    except (ConnectionError, asyncio.TimeoutError):
        pass
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except (ConnectionError, OSError):
            pass


from contextlib import ExitStack, asynccontextmanager
import os
from pathlib import Path
import socket
import stat


@asynccontextmanager
async def private_ipc_server(path: Path, gateway, *, max_connections: int = 16):
    """Serve on a new private socket while the caller holds daemon ownership.

    The existing private parent directory is the filesystem trust boundary.
    Only a provably stale socket (user-owned, connection refused) is reclaimed.
    """
    if type(max_connections) is not int or not 1 <= max_connections <= 256:
        raise ValueError('invalid connection limit')
    path = Path(path)
    directory = path.parent.lstat()
    if (not path.is_absolute() or not stat.S_ISDIR(directory.st_mode)
            or directory.st_uid != os.geteuid() or directory.st_mode & 0o077):
        raise PermissionError('IPC directory must be private and user-owned')
    # Reclaim+bind run under <socket>.lock, held for the listener's lifetime, so a
    # second instance cannot reclaim a socket between our bind and listen.
    locks = ExitStack()
    sock = None
    identity = None
    server = None
    active = set()
    try:
        locks.enter_context(daemon_lock(path.with_name(path.name + '.lock')))
        reclaim_stale_socket(path)
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        previous = os.umask(0o177)  # never visible wider than 0600 (process-wide umask)
        try:
            sock.bind(str(path))
            info = path.lstat()
            identity = (info.st_dev, info.st_ino)
        finally:
            os.umask(previous)
        os.chmod(path, 0o600, follow_symlinks=False)
        sock.listen(max_connections)
        sock.setblocking(False)

        async def client(reader, writer):
            task = asyncio.current_task()
            if len(active) >= max_connections:
                writer.close()
                return
            active.add(task)
            try:
                await handle_ipc(reader, writer, gateway)
            finally:
                active.discard(task)

        server = await asyncio.start_unix_server(client, sock=sock, limit=MAX_LINE)
        yield
    finally:
        if server is not None:
            server.close()
            await server.wait_closed()
            for task in tuple(active):
                task.cancel()
            if active:
                await asyncio.gather(*tuple(active), return_exceptions=True)
        if sock is not None:
            sock.close()
        try:
            if identity is not None:
                try:
                    info = path.lstat()
                    if stat.S_ISSOCK(info.st_mode) and (info.st_dev, info.st_ino) == identity:
                        path.unlink()
                except FileNotFoundError:
                    pass
        finally:
            locks.close()
