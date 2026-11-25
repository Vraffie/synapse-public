"""Run the read-only local Synapse daemon on a private Unix socket."""
from __future__ import annotations

import argparse
import asyncio
from contextlib import contextmanager
import logging
import os
from pathlib import Path
import re
import secrets
import signal
import socket
import stat
import sys
from collections.abc import Iterator

import uvicorn

from synapse.api.app import create_app
from synapse.application.message_gateway import MessageGateway
from synapse.application.session_authority import SessionAuthority
from synapse.application.approval_service import ApprovalService
from synapse.application.approval_requests import DaemonApprovalRequester
from synapse.infrastructure.passphrase_approver import build_approver_authority
from synapse.infrastructure.daemon_lock import daemon_lock
from synapse.infrastructure.socket_reclaim import reclaim_stale_socket
from synapse.mcp_gateway.ipc import private_ipc_server
from synapse.persistence import audit_chain
from synapse.persistence.backup import BackupError, _check_schema, code_schema_version
from synapse.persistence.agent_tasks import AGENT_PROFILE
from synapse.persistence.budget_binding import BudgetPolicyError, load_budget_policy
from synapse.daemon.agent_config import AgentConfigError, load_agent_config, registry_sha256
from synapse.daemon.agent_profiles import build_registry, load_agent_profiles, sync_profiles
from synapse.infrastructure.spawn_journal import SpawnJournal
from synapse.daemon.agent_runtime import AgentRuntime
from synapse.daemon.same_user_gate import ENV as ALLOW_ENV, SameUserAgentsRefused, check_same_user_agents, record_acknowledgement
from synapse.daemon.agent_service import AgentRunService, worktree_checker
from synapse.daemon.manifest_builder import ManifestBuilder
from synapse.daemon.manifest_service import ManifestService
from synapse.daemon.multi_repo import AttemptWorkspaces, RepoRegistry, resolve_commit_intents
from synapse.infrastructure.worktrees import WorktreeManager
from synapse.providers.credential_sweep import sweep_startup
from synapse.daemon.mock_runtime import MockRuntime
from synapse.infrastructure.daemon_database import daemon_database
from synapse.persistence.event_store import read_events
from synapse.persistence.migrations import default_migrations_dir
from synapse.persistence.operator_messages import read_threads, read_messages
from synapse.persistence.run_list import read_run_list
from synapse.persistence.run_snapshot import read_run_snapshot, read_run_tasks
from synapse.application.analytics_views import read_run_analytics
from synapse.persistence.usage_query import read_run_usage
from synapse.persistence.approval_query import read_approvals
from synapse.persistence.approval_expiry import run_expiry_sweeps
from synapse.persistence.workflow_store import publish_workflow, create_run

_TOKEN = re.compile(rb"[A-Za-z0-9_-]{43}")


def _private_directory(path: Path) -> int:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW)
    info = os.fstat(fd)
    if info.st_uid != os.geteuid() or info.st_mode & 0o077:
        os.close(fd)
        raise PermissionError("State directory must be private and user-owned")
    return fd


def _operator_token(dir_fd: int) -> str:
    """Load or atomically create a private local operator token under the lock."""
    created = False
    try:
        fd = os.open("operator.token", os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600, dir_fd=dir_fd)
        created = True
    except FileExistsError:
        fd = os.open("operator.token", os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=dir_fd)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077 or info.st_nlink != 1:
            raise PermissionError("Operator token must be a private, user-owned regular file with one link")
        if created:
            token = secrets.token_urlsafe(32).encode("ascii")
            with os.fdopen(fd, "wb", closefd=False) as stream:
                stream.write(token)
                stream.flush()
                os.fsync(fd)
            os.fsync(dir_fd)
        else:
            token = os.read(fd, 44)
            if token == b"" and info.st_size == 0:
                # A crash between create and write left an empty file; we hold the daemon lock.
                token = secrets.token_urlsafe(32).encode("ascii")
                wfd = os.open("operator.token", os.O_WRONLY | os.O_CLOEXEC | os.O_NOFOLLOW, dir_fd=dir_fd)
                try:
                    wi = os.fstat(wfd)
                    if (wi.st_ino, wi.st_dev) != (info.st_ino, info.st_dev):
                        raise PermissionError("Operator token changed during repair")
                    os.write(wfd, token)
                    os.fsync(wfd)
                finally:
                    os.close(wfd)
                os.fsync(dir_fd)
            if not _TOKEN.fullmatch(token):
                raise ValueError("Operator token file is invalid")
        return token.decode("ascii")
    finally:
        os.close(fd)


@contextmanager
def _socket(path: Path) -> Iterator[tuple[socket.socket, tuple[int, int]]]:
    """Bind a socket under exclusive ownership, reclaiming only a provably stale one.

    Reclaim and bind run while holding ``<socket>.lock`` (a flock, held for the whole
    lifetime of the listener), so two supervisors/daemons on one state directory cannot
    race one's reclaim against the other's bind-before-listen. The socket is created
    under ``umask(0o177)`` so it is never visible with wider permissions than 0600.
    Yields ``(sock, (st_dev, st_ino))``; the identity is captured immediately after
    ``bind`` (before chmod/listen) and must be passed on, never re-read from the path.
    (``os.umask`` is process-wide, so do not bind sockets from other threads concurrently.)
    """
    with daemon_lock(path.with_name(path.name + ".lock")):
        reclaim_stale_socket(path)
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        identity = None
        try:
            previous = os.umask(0o177)
            try:
                sock.bind(str(path))
                info = path.lstat()  # right after bind: nothing else between bind and capture
                identity = (info.st_dev, info.st_ino)
            finally:
                os.umask(previous)
            os.chmod(path, 0o600, follow_symlinks=False)
            sock.listen(128)
            yield sock, identity
        finally:
            sock.close()
            if identity is not None:
                try:
                    info = path.lstat()
                    if stat.S_ISSOCK(info.st_mode) and (info.st_dev, info.st_ino) == identity:
                        path.unlink()
                except FileNotFoundError:
                    pass


class SocketIdentityError(PermissionError):
    """The inherited socket path is no longer the inode the supervisor bound."""


# Exit status for SocketIdentityError (EX_CONFIG); the supervisor must not restart on it.
EXIT_SOCKET_IDENTITY = 78


@contextmanager
def _owned_socket(path: Path) -> Iterator[socket.socket]:
    with _socket(path) as (sock, _identity_pair):
        yield sock


@contextmanager
def _inherited_socket(fd: int, path: Path, identity: tuple[int, int] | None = None) -> Iterator[socket.socket]:
    """Use a trusted supervisor's listening socket without owning its path.

    ``identity`` is the ``(st_dev, st_ino)`` the supervisor recorded for the path right
    after it bound; when given, the path must still be that exact inode, so a same-user
    process that swapped the path for its own listener is detected (clients would
    otherwise send tokens to it). Without it (e.g. systemd activation, which cannot
    tell us) only the type/owner/mode/name checks apply.
    """
    if type(fd) is not int or fd < 3:
        raise ValueError("Inherited socket descriptor must be at least 3")
    sock = socket.socket(fileno=os.dup(fd))
    try:
        info = path.lstat()
        if (not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.geteuid()
                or info.st_mode & 0o077):
            raise PermissionError("Activated socket path must be private and user-owned")
        address = sock.getsockname()
        if (sock.family != socket.AF_UNIX or sock.getsockopt(socket.SOL_SOCKET, socket.SO_TYPE) != socket.SOCK_STREAM
                or not sock.getsockopt(socket.SOL_SOCKET, socket.SO_ACCEPTCONN)
                or not isinstance(address, str) or os.path.abspath(address) != os.path.abspath(path)):
            raise ValueError("Inherited descriptor must listen on the configured Unix socket")
        if identity is not None and (info.st_dev, info.st_ino) != tuple(identity):
            raise SocketIdentityError("Activated socket path was replaced after the supervisor bound it")
        yield sock
    finally:
        sock.close()


class _DaemonServer(uvicorn.Server):
    @contextmanager
    def capture_signals(self):
        # Uvicorn normally re-raises the signal before returning, which would
        # bypass our socket and database context managers.
        previous = {sig: signal.signal(sig, self.handle_exit) for sig in (signal.SIGINT, signal.SIGTERM)}
        try:
            yield
        finally:
            for sig, handler in previous.items():
                signal.signal(sig, handler)


EXPIRY_INTERVAL_SECONDS = 30.0


async def _anchor_loop(connection, interval):
    """Seal new events and publish chain heads on a short timer (ADR 018)."""
    while True:
        await asyncio.sleep(interval)
        try:
            audit_chain.flush_anchors(connection)
        except Exception:  # noqa: BLE001 - never take the daemon down; the gate check still fails closed
            logging.getLogger(__name__).exception("chain anchor flush failed")


async def _with_expiry_sweeps(connection, serve, interval=EXPIRY_INTERVAL_SECONDS, startup=None):
    """Run ``serve`` while sweeping overdue approvals on this thread's connection.

    ``startup`` (optional) runs once inside the loop before serving, e.g. to
    re-drive a mock run left parked on an approval.
    """
    if startup is not None:
        startup()
    sweeper = asyncio.create_task(run_expiry_sweeps(connection, interval))
    anchors = asyncio.create_task(_anchor_loop(connection, interval))
    try:
        await serve
    finally:
        sweeper.cancel()
        anchors.cancel()
        await asyncio.gather(sweeper, anchors, return_exceptions=True)


async def _serve_with_mock_ipc(server, sock, ipc_path, connection, startup=None):
    """Host a fail-closed IPC endpoint; no agent sessions are provisioned."""
    authority = SessionAuthority()
    gateway = MessageGateway(connection, authority)
    async with private_ipc_server(ipc_path, gateway):
        await _with_expiry_sweeps(connection, server.serve(sockets=[sock]), startup=startup)


def _identity(value: str) -> tuple[int, int]:
    match = re.fullmatch(r"([0-9]+):([0-9]+)", value)  # no sign, spaces, extra fields
    if match is None:
        raise argparse.ArgumentTypeError("must be DEV:INO (two nonnegative integers)")
    return int(match.group(1)), int(match.group(2))


def run(state_dir: Path, migrations: Path, *, enable_mock_mcp_ipc: bool = False,
        socket_fd: int | None = None, claude_cli_config: Path | None = None,
        agent_config_path: Path | None = None, socket_identity: tuple[int, int] | None = None, accept_chain_reset: bool = False,
        allow_same_user_agents: bool = False, budget_policy_path: Path | None = None) -> None:
    dir_fd = _private_directory(state_dir)
    approver_authority = build_approver_authority(dir_fd)
    try:
        # Fail before recovering or mutating state if the operator config is malformed.
        agent_config = None if claude_cli_config is None else load_agent_config(claude_cli_config, state_dir)
        claude_profile = None if agent_config is None else agent_config.profile
        if agent_config_path is not None and agent_config is None:
            raise AgentConfigError("--agent-config requires --claude-cli-config (the base profile)")
        profile_registry = None if agent_config is None else (
            build_registry(agent_config) if agent_config_path is None else load_agent_profiles(agent_config_path, agent_config))
        # A malformed policy stops the start; it never degrades to "no policy" (ADR 020 D2).
        known_profiles = frozenset({AGENT_PROFILE}) | (
            frozenset() if profile_registry is None else frozenset(s.id for s in profile_registry.profiles))
        budget_policy = None if budget_policy_path is None else load_budget_policy(
            budget_policy_path, profile_ids=known_profiles)
        # A codex profile (only possible with the base sandbox off) runs with no Synapse sandbox at all: say so.
        codex_unsandboxed = profile_registry is not None and any(
            s.provider == "codex" and s.enabled for s in profile_registry.profiles)
        # Fail closed (security review F1/F2) before touching state unless the operator acknowledged.
        acknowledged = check_same_user_agents(
            state_dir, agents_enabled=agent_config is not None and agent_config.profile.enabled,
            allow=allow_same_user_agents,
            sandbox_configured=None if agent_config is None else bool(agent_config.profile.sandbox),
            codex_unsandboxed=codex_unsandboxed)
        worktrees = None
        if agent_config is not None and agent_config.repos:
            try:
                worktrees = WorktreeManager(agent_config.repos, agent_config.worktree_root)
            except (ValueError, OSError) as exc:
                raise AgentConfigError("invalid repos/worktree_root: %s" % exc) from exc
            if agent_config.registry:  # ADR 026 D1: what the operator's registry was at this start (ids, no paths)
                logging.getLogger(__name__).info(
                    "repository registry: %d entries, registry_sha256=%s", len(agent_config.registry),
                    registry_sha256(agent_config.registry))
        # Fail before recovering or mutating state if activation is malformed.
        if socket_fd is not None:
            with _inherited_socket(socket_fd, state_dir / "agentd.sock", socket_identity):
                pass
        with daemon_database(state_dir / "synapse.sqlite", migrations) as connection:
            try:  # a dropped or altered trigger is reported as a broken chain (same check as backup-verify)
                _check_schema(connection, migrations, code_schema_version(migrations))
                schema_problems = ()
            except BackupError as exc:
                schema_problems = ("schema: " + str(exc),)
            report = audit_chain.startup_check(connection, audit_chain.default_sinks(state_dir),
                                               accept_reset=accept_chain_reset, schema_problems=schema_problems)
            if not report.ok:
                print("agentd: audit chain verification FAILED; gated tasks are refused until resolved "
                      "(see docs/adr/018-audit-hash-chain.md): " + "; ".join(report.problems[:5]), file=sys.stderr)
            elif report.strength is None:
                print("agentd: audit chain has no readable external anchor; tamper evidence is limited", file=sys.stderr)
            token = _operator_token(dir_fd)
            if acknowledged:
                record_acknowledgement(connection, codex_unsandboxed=codex_unsandboxed)
            if agent_config is not None and agent_config.registry:
                # ADR 026 D1: the startup audit event records which registry was loaded (hash and ids, no paths).
                import json as _json
                from datetime import datetime as _dt, timezone as _tz
                audit_chain.append_audit(
                    connection, "daemon:startup", "agent.repo_registry_loaded", "agent-runtime", "allowed", None,
                    _json.dumps({"registry_sha256": registry_sha256(agent_config.registry),
                                 "repo_ids": sorted(e.id for e in agent_config.registry)}, sort_keys=True),
                    _dt.now(_tz.utc).isoformat())
            try:
                os.mkdir("artifacts", 0o700, dir_fd=dir_fd)
            except FileExistsError:
                pass
            artifacts = state_dir / "artifacts"
            artifact_fd = _private_directory(artifacts)
            os.close(artifact_fd)
            approval_service = ApprovalService(connection, approver_authority)
            mock_runtime = MockRuntime(connection, artifacts)
            mock_runtime.recover_interrupted()
            try:
                sweep_startup(state_dir)  # remove scratch HOMEs/credential copies left by a SIGKILLed daemon
            except Exception:
                logging.getLogger(__name__).exception("credential sweep failed; continuing startup")
            # Refuses start without an enabled profile; the API only works with repos + worktree_root too.
            agent_runtime = AgentRuntime(
                connection, artifacts, profile=claude_profile,
                approval_policy=None if agent_config is None else agent_config.approval_policy,
                worktree_check=None if worktrees is None else worktree_checker(worktrees),
                budget_policy=budget_policy, registry=profile_registry,
                spawn_journal=SpawnJournal(state_dir / "spawn-journal"),
                workspaces=None if worktrees is None else AttemptWorkspaces(
                    worktrees, allow_unsandboxed=agent_config.allow_multi_repo_unsandboxed),
                commit_recovery=lambda db: resolve_commit_intents(db, worktrees))
            agent_runtime.recover_interrupted()
            if profile_registry is not None:
                sync_profiles(connection, profile_registry)
            agent_service = AgentRunService(connection, agent_runtime, worktrees, profile_registry,
                                            RepoRegistry(() if agent_config is None else agent_config.registry))
            manifests = ManifestService(
                connection, None if worktrees is None else ManifestBuilder(connection, agent_runtime.blobs, worktrees),
                DaemonApprovalRequester(connection))
            def resume_parked():
                mock_runtime.resume_parked()
                agent_runtime.resume_parked()
            app = create_app(
                token=token,
                start_mock=mock_runtime.start,
                cancel_mock=mock_runtime.cancel,
                resume_mock=mock_runtime.resume,
                start_agent_run=agent_service.start,
                read_agent_run=agent_service.show,
                cancel_agent_run=agent_service.cancel,
                abandon_agent_run=agent_runtime.abandon,
                remove_agent_worktree=agent_service.remove_worktree,
                commit_agent_worktree=agent_service.commit_worktree,
                build_manifest=manifests.build, list_manifests=manifests.list, show_manifest=manifests.show,
                recheck_manifest=manifests.recheck, request_manifest_approval=manifests.request_approval,
                list_profiles=agent_service.list_profiles,
                set_profile_cooldown=agent_service.set_cooldown,
                clear_profile_cooldown=agent_service.clear_cooldown,
                read_events=lambda after, limit: read_events(connection, after_sequence=after, limit=limit),
                read_run=lambda run_id: read_run_snapshot(connection, run_id),
                read_tasks=lambda run_id, limit, cursor: read_run_tasks(connection, run_id, limit=limit, cursor=cursor),
                read_usage=lambda run_id, limit, cursor: read_run_usage(connection, run_id, limit=limit, cursor=cursor),
                read_analytics=lambda run_id, limit, cursor: read_run_analytics(connection, run_id, limit=limit, cursor=cursor).to_dict(),
                read_runs=lambda state, limit, cursor: read_run_list(connection, state=state, limit=limit, cursor=cursor),
                read_approvals=lambda after, limit: read_approvals(connection, after=after, limit=limit, read_blob=agent_runtime.blobs.read),
                decide_approval=lambda token, approval_id, *, key, correlation, **fields: approval_service.decide(
                    token, approval_id=approval_id, idempotency_key=key, correlation_id=correlation, **fields),
                audit_denied_decision=approval_service.record_denied,
                preauthenticate_decision=approval_service.preauthenticate,
                publish_workflow=lambda workflow_id, version, nodes, key: publish_workflow(connection, workflow_id=workflow_id, version=version, nodes=nodes, principal="local-operator", idempotency_key=key),
                create_run=lambda workflow_id, version, key: create_run(connection, workflow_id=workflow_id, version=version, principal="local-operator", idempotency_key=key),
                read_threads=lambda run_id, after, limit: read_threads(connection, run_id, after, limit),
                read_messages=lambda run_id, thread_id, after, limit: read_messages(connection, run_id, thread_id, after, limit),
            )
            socket_path = state_dir / "agentd.sock"
            listener = _owned_socket(socket_path) if socket_fd is None else _inherited_socket(socket_fd, socket_path, socket_identity)
            with listener as sock:
                config = uvicorn.Config(app, log_level="info", access_log=False, lifespan="off")
                server = _DaemonServer(config)
                if enable_mock_mcp_ipc:
                    # Database and both transports stay on the same owning thread.
                    asyncio.run(_serve_with_mock_ipc(server, sock, state_dir / "mock-mcp.sock", connection, resume_parked))
                else:
                    asyncio.run(_with_expiry_sweeps(connection, server.serve(sockets=[sock]), startup=resume_parked))
            audit_chain.flush_anchors(connection)
    finally:
        approver_authority.close()
        os.close(dir_fd)


def _stderr_handler() -> logging.Handler:
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))
    return handler


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Run the read-only Synapse local daemon")
    parser.add_argument("--state-dir", required=True, type=Path, help="existing private state directory")
    parser.add_argument("--migrations", default=default_migrations_dir(), type=Path, help="versioned migration directory (default: packaged)")
    parser.add_argument("--enable-mock-mcp-ipc", action="store_true",
                        help="start an isolated mock IPC endpoint without provisioning agent sessions")
    parser.add_argument("--socket-identity", type=_identity, help="DEV:INO the supervisor recorded for the socket path")
    parser.add_argument("--socket-fd", type=int, help="trusted supervisor's inherited listening socket descriptor")
    parser.add_argument("--accept-chain-reset", action="store_true",
                        help="after a restore or crash, accept the database head as the new audit-chain baseline "
                             "(recorded as a reset anchor; never clears edited rows)")
    parser.add_argument("--claude-cli-config", type=Path,
                        help="operator JSON enabling the real Claude CLI agent runtime (see docs/operations/agent-runtime.md)")
    parser.add_argument("--budget-policy", type=Path,
                        help="operator JSON budget policy (BudgetPolicy format); bound immutably to each run at start. "
                             "Without it runs have no limits (docs/adr/020-m5-budget-policy-wiring.md)")
    parser.add_argument("--agent-config", type=Path,
                        help="operator JSON declaring extra agent profiles that narrow the --claude-cli-config "
                             "profile (see docs/operations/agent-profiles.md)")
    parser.add_argument("--allow-same-user-agents", action="store_true",
                        default=os.environ.get(ALLOW_ENV) == "1",
                        help="acknowledge that agents run as the user that can read the approver token and write "
                             "the database (approvals are not a security boundary); required to enable agent runs")
    args = parser.parse_args(argv)
    logging.getLogger("synapse").addHandler(_stderr_handler())
    if args.socket_identity is not None and args.socket_fd is None:
        parser.error("--socket-identity requires --socket-fd")
    try:
        run(args.state_dir, args.migrations, enable_mock_mcp_ipc=args.enable_mock_mcp_ipc, socket_fd=args.socket_fd, socket_identity=args.socket_identity,
            claude_cli_config=args.claude_cli_config, agent_config_path=args.agent_config, accept_chain_reset=args.accept_chain_reset,
            allow_same_user_agents=args.allow_same_user_agents, budget_policy_path=args.budget_policy)
    except BudgetPolicyError as exc:
        print(f"agentd: {exc}", file=sys.stderr)
        raise SystemExit(2) from None
    except SameUserAgentsRefused as exc:
        print(f"agentd: {exc}", file=sys.stderr)
        raise SystemExit(2) from None
    except SocketIdentityError as exc:
        print(f"agentd: {exc}", file=sys.stderr)
        raise SystemExit(EXIT_SOCKET_IDENTITY) from None


if __name__ == "__main__":
    main()
