"""Bound newline-delimited MCP input before the SDK parses JSON."""
from __future__ import annotations

import asyncio
import os

MAX_STDIO_LINE = 131072
MAX_JSON_DEPTH = 64


def _check_depth(line: bytes) -> None:
    depth = 0
    quoted = False
    escaped = False
    for char in line:
        if quoted:
            if escaped:
                escaped = False
            elif char == 92:  # backslash
                escaped = True
            elif char == 34:
                quoted = False
        elif char == 34:
            quoted = True
        elif char in (91, 123):
            depth += 1
            if depth > MAX_JSON_DEPTH:
                raise ValueError('MCP input nesting exceeds limit')
        elif char in (93, 125):
            depth -= 1


class BoundedStdin:
    """Async line iterator backed by one Unix FD, bounded before UTF-8 decode."""
    def __init__(self, fd: int):
        self._fd = fd
        self._buffer = bytearray()
        self._eof = False

    def __aiter__(self):
        return self

    async def __anext__(self) -> str:
        while True:
            newline = self._buffer.find(b'\n')
            if newline >= 0:
                if newline + 1 > MAX_STDIO_LINE:
                    raise ValueError('MCP input exceeds limit')
                line = bytes(self._buffer[:newline + 1])
                del self._buffer[:newline + 1]
                _check_depth(line)
                return line.decode('utf-8', errors='strict')
            if len(self._buffer) > MAX_STDIO_LINE:
                raise ValueError('MCP input exceeds limit')
            if self._eof:
                if self._buffer:
                    raise ValueError('Incomplete MCP input')
                raise StopAsyncIteration
            loop = asyncio.get_running_loop()
            ready = loop.create_future()
            def on_readable():
                if not ready.done():
                    ready.set_result(None)
            loop.add_reader(self._fd, on_readable)
            try:
                await ready
                data = os.read(self._fd, MAX_STDIO_LINE + 1 - len(self._buffer))
            finally:
                loop.remove_reader(self._fd)
            if not data:
                self._eof = True
            else:
                self._buffer.extend(data)
