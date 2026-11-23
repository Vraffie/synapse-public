"""Behaviour of the bounded MCP stdin reader (size, depth, framing, UTF-8)."""
import asyncio
import os
import threading

import pytest

from synapse.mcp_gateway import bounded_stdio as bs
from synapse.mcp_gateway.bounded_stdio import BoundedStdin, _check_depth


def read_all(chunks: list[bytes]) -> list[str]:
    """Feed chunks through a pipe (from a thread) and collect lines until EOF."""
    r, w = os.pipe()

    def feed():
        try:
            for part in chunks:
                os.write(w, part)
        except OSError:
            pass
        finally:
            os.close(w)

    thread = threading.Thread(target=feed)
    thread.start()

    async def run():
        out = []
        async for line in BoundedStdin(r):
            out.append(line)
        return out

    try:
        return asyncio.run(asyncio.wait_for(run(), 10))
    finally:
        thread.join()
        os.close(r)


def nested(depth: int) -> bytes:
    return b"[" * depth + b"]" * depth + b"\n"


def test_lines_are_yielded_in_order_including_newline():
    assert read_all([b'{"a":1}\n{"b":2}\n']) == ['{"a":1}\n', '{"b":2}\n']


def test_line_split_across_reads_is_reassembled():
    assert read_all([b'{"a"', b":1}", b"\n"]) == ['{"a":1}\n']


def test_empty_input_ends_cleanly():
    assert read_all([]) == []


def test_eof_with_partial_line_is_rejected():
    with pytest.raises(ValueError, match="Incomplete"):
        read_all([b'{"a":1}'])


def test_line_of_exactly_max_bytes_is_accepted():
    line = b"x" * (bs.MAX_STDIO_LINE - 1) + b"\n"
    assert len(line) == bs.MAX_STDIO_LINE
    assert read_all([line]) == [line.decode()]


def test_line_one_byte_over_max_is_rejected():
    line = b"x" * bs.MAX_STDIO_LINE + b"\n"
    with pytest.raises(ValueError, match="exceeds limit"):
        read_all([line])


def test_unterminated_oversize_input_is_rejected():
    with pytest.raises(ValueError, match="exceeds limit"):
        read_all([b"x" * (bs.MAX_STDIO_LINE + 10)])


def test_good_line_before_oversize_line_is_still_delivered():
    r, w = os.pipe()
    big = b"y" * (bs.MAX_STDIO_LINE + 1) + b"\n"

    def feed():
        try:
            os.write(w, b"ok\n")
            os.write(w, big)
        except OSError:
            pass
        finally:
            os.close(w)

    t = threading.Thread(target=feed)
    t.start()

    async def run():
        it = BoundedStdin(r)
        first = await it.__anext__()
        with pytest.raises(ValueError, match="exceeds limit"):
            await it.__anext__()
        return first

    try:
        assert asyncio.run(asyncio.wait_for(run(), 10)) == "ok\n"
    finally:
        t.join()
        os.close(r)


def test_invalid_utf8_is_rejected():
    with pytest.raises(UnicodeDecodeError):
        read_all([b"\xff\xfe\n"])


def test_multibyte_utf8_survives_split_reads():
    data = '{"k":"åß€"}\n'.encode()
    assert read_all([data[:7], data[7:]]) == ['{"k":"åß€"}\n']


def test_depth_at_limit_accepted_and_one_over_rejected():
    _check_depth(nested(bs.MAX_JSON_DEPTH))
    with pytest.raises(ValueError, match="nesting"):
        _check_depth(nested(bs.MAX_JSON_DEPTH + 1))


def test_brackets_inside_strings_do_not_count_toward_depth():
    _check_depth(b'{"k":"' + b"[" * 500 + b'"}\n')


def test_escaped_quote_does_not_end_string_early():
    _check_depth(b'{"k":"\\"' + b"[" * 500 + b'"}\n')


def test_escaped_backslash_then_quote_does_end_string():
    # "\\" is a complete escape; the next quote closes the string, so the
    # following brackets are real structure and exceed the limit.
    with pytest.raises(ValueError, match="nesting"):
        _check_depth(b'"\\\\"' + b"[" * (bs.MAX_JSON_DEPTH + 1))


def test_deeply_nested_line_is_rejected_through_the_reader():
    with pytest.raises(ValueError, match="nesting"):
        read_all([nested(bs.MAX_JSON_DEPTH + 1)])


def test_sibling_arrays_do_not_accumulate_depth():
    _check_depth(b"[]" * 1000 + b"\n")
