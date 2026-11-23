"""Retain a private daemon listener and restart a crashed child, boundedly."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import math
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

from synapse.daemon.__main__ import EXIT_SOCKET_IDENTITY, _private_directory, _socket
from synapse.infrastructure.daemon_lock import DaemonAlreadyRunning
from synapse.persistence.migrations import default_migrations_dir


def _nonnegative_int(value: str) -> int:
    try:
        result = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("must be a nonnegative integer") from None
    if not 0 <= result <= 100:
        raise argparse.ArgumentTypeError("must be between 0 and 100")
    return result


def _positive_seconds(value: str) -> float:
    try:
        result = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError("must be a positive number") from None
    if not math.isfinite(result) or not 0 < result <= 300:
        raise argparse.ArgumentTypeError("must be greater than zero and at most 300 seconds")
    return result


@contextmanager
def _shutdown_signals(stop: list[bool]):
    def handle(_signal, _frame):
        stop[0] = True
    previous = {sig: signal.signal(sig, handle) for sig in (signal.SIGINT, signal.SIGTERM)}
    try:
        yield
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


def _nonnegative_seconds(value: str) -> float:
    try:
        result = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError("must be a non-negative number") from None
    if not math.isfinite(result) or not 0 <= result <= 86400:
        raise argparse.ArgumentTypeError("must be between zero and 86400 seconds")
    return result


def run(state_dir: Path, migrations: Path, *, max_restarts: int = 5,
        base_delay: float = 0.25, max_delay: float = 8.0, shutdown_timeout: float = 5.0,
        healthy_seconds: float = 60.0, enable_mock_mcp_ipc: bool = False,
        claude_cli_config: Path | None = None, agent_config: Path | None = None) -> int:
    """Supervise one child. Child state remains authoritative and daemon-owned."""
    if type(max_restarts) is not int or not 0 <= max_restarts <= 100:
        raise ValueError("max_restarts must be between 0 and 100")
    if isinstance(healthy_seconds, bool) or not isinstance(healthy_seconds, (int, float)) or not 0 <= healthy_seconds <= 86400:
        raise ValueError("healthy_seconds must be between zero and 86400")
    for value in (base_delay, max_delay, shutdown_timeout):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 < value <= 300:
            raise ValueError("delays must be positive finite values up to 300 seconds")
    if base_delay > max_delay:
        raise ValueError("base delay must not exceed maximum delay")
    directory = _private_directory(state_dir)
    os.close(directory)
    stop = [False]
    restarts = 0
    child = None
    with _shutdown_signals(stop), _socket(state_dir / "agentd.sock") as (listener, identity):
        try:
            while not stop[0]:
                command = [sys.executable, "-m", "synapse.daemon", "--state-dir", str(state_dir),
                           "--migrations", str(migrations), "--socket-fd", str(listener.fileno()),
                           "--socket-identity", f"{identity[0]}:{identity[1]}"]
                if enable_mock_mcp_ipc:
                    command.append("--enable-mock-mcp-ipc")
                if claude_cli_config is not None:
                    command += ["--claude-cli-config", str(claude_cli_config)]
                if agent_config is not None:
                    command += ["--agent-config", str(agent_config)]
                started = time.monotonic()
                child = subprocess.Popen(command, pass_fds=(listener.fileno(),), close_fds=True)
                print(f"agentd-supervisor: started daemon pid={child.pid}", file=sys.stderr, flush=True)
                while child.poll() is None and not stop[0]:
                    time.sleep(0.1)
                if stop[0]:
                    break
                code = child.returncode
                child = None
                if time.monotonic() - started >= healthy_seconds:
                    restarts = 0  # A healthy run earns a fresh restart budget.
                if code == 0:
                    return 0
                if code == EXIT_SOCKET_IDENTITY:
                    print("agentd-supervisor: daemon rejected the socket identity; not restarting", file=sys.stderr, flush=True)
                    return 1
                if restarts >= max_restarts:
                    print("agentd-supervisor: restart limit reached", file=sys.stderr, flush=True)
                    return 1
                delay = min(max_delay, base_delay * (2 ** restarts))
                restarts += 1
                print(f"agentd-supervisor: daemon exited ({code}); retry {restarts} in {delay:g}s", file=sys.stderr, flush=True)
                deadline = time.monotonic() + delay
                while not stop[0] and (remaining := deadline - time.monotonic()) > 0:
                    time.sleep(min(0.1, remaining))
            return 0
        finally:
            if child is not None and child.poll() is None:
                child.terminate()
                try:
                    child.wait(timeout=shutdown_timeout)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Supervise a local Synapse daemon with bounded restart")
    parser.add_argument("--state-dir", required=True, type=Path)
    parser.add_argument("--migrations", default=default_migrations_dir(), type=Path)
    parser.add_argument("--max-restarts", type=_nonnegative_int, default=5)
    parser.add_argument("--base-delay", type=_positive_seconds, default=0.25)
    parser.add_argument("--max-delay", type=_positive_seconds, default=8.0)
    parser.add_argument("--healthy-seconds", type=_nonnegative_seconds, default=60.0)
    parser.add_argument("--shutdown-timeout", type=_positive_seconds, default=5.0)
    parser.add_argument("--enable-mock-mcp-ipc", action="store_true",
                        help="forwarded to the daemon: start the isolated mock IPC endpoint")
    parser.add_argument("--claude-cli-config", type=Path,
                        help="forwarded to the daemon: operator JSON enabling the real Claude CLI agent runtime")
    parser.add_argument("--agent-config", type=Path,
                        help="forwarded to the daemon: operator JSON declaring extra agent profiles")
    args = parser.parse_args(argv)
    agent_config = args.agent_config.resolve() if args.agent_config is not None else None
    claude_cli_config = args.claude_cli_config.resolve() if args.claude_cli_config is not None else None
    try:
        return run(args.state_dir, args.migrations, max_restarts=args.max_restarts,
                   base_delay=args.base_delay, max_delay=args.max_delay, shutdown_timeout=args.shutdown_timeout, healthy_seconds=args.healthy_seconds,
                   enable_mock_mcp_ipc=args.enable_mock_mcp_ipc, claude_cli_config=claude_cli_config,
                   agent_config=agent_config)
    except (OSError, ValueError, DaemonAlreadyRunning) as exc:
        print(f"agentd-supervisor: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
