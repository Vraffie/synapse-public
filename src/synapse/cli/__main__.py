"""Read local daemon health and event pages over its private Unix socket."""
from __future__ import annotations

import argparse
import base64
from datetime import datetime, timedelta, timezone
import getpass
import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import socket
import stat
import sys
import uuid
from urllib.parse import quote, urlencode

from synapse.default_profile import read_default_profile
from synapse.persistence.migrations import default_migrations_dir
from synapse.persistence.approval_environment import ALLOWED_ENVIRONMENTS

_TOKEN = re.compile(rb"[A-Za-z0-9_-]{43}")
_MAX_RESPONSE = 16 * 1024 * 1024
_MAX_SEQUENCE = 9223372036854775807
_MAX_REQUEST = 1024 * 1024
_COMMAND_HEADER = re.compile(r"[\x21-\x7e]{1,256}\Z")


class DaemonHTTPError(RuntimeError):
    """HTTP rejection without retaining a potentially sensitive response body."""
    def __init__(self, status: int, locked_until: str | None = None, reason: str | None = None):
        self.status = status
        self.locked_until = locked_until  # validated HH:MM (UTC) from a fixed header, never a body
        self.reason = reason  # only a 422's short string "detail" (the daemon's refusal reason), sanitized and bounded
        message = f"daemon returned HTTP {status}"
        if reason:
            message += f": {reason}"
        if locked_until:
            message += f"; the approver is locked until {locked_until} UTC (operator reset: agentctl approver-unlock)"
        super().__init__(message)


class UnixConnection(http.client.HTTPConnection):
    def __init__(self, path: Path):
        super().__init__("localhost", timeout=5)
        self.path = path

    def connect(self) -> None:
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(str(self.path))


def _load_token(state_dir: Path) -> str:
    directory = os.open(state_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW)
    try:
        info = os.fstat(directory)
        if info.st_uid != os.geteuid() or info.st_mode & 0o077:
            raise PermissionError("state directory must be private and user-owned")
        fd = os.open("operator.token", os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077 or info.st_nlink != 1:
                raise PermissionError("operator token must be a private user-owned regular file with one link")
            token = os.read(fd, 44)
            if not _TOKEN.fullmatch(token):
                raise ValueError("operator token file is invalid")
            return token.decode("ascii")
        finally:
            os.close(fd)
    finally:
        os.close(directory)


def _follow_fetch(state_dir: Path):
    """``events --follow`` fetcher. A missing state dir or operator token is a configuration error, not a daemon
    that is down: it raises FollowError (never retried), so only the socket itself is a transient failure."""
    from synapse.cli.follow import FollowError

    def fetch(endpoint: str) -> object:
        try:
            _load_token(state_dir)
        except (OSError, ValueError) as exc:
            raise FollowError(f"cannot use state directory {state_dir}: "
                              f"{getattr(exc, 'strerror', None) or exc}") from None
        return _request(state_dir, endpoint)
    return fetch


_REASON_MAX = 200
_PROFILE_ID = re.compile(r"[A-Za-z0-9._-]{1,64}")  # the daemon's profile id pattern (api/agent_commands.py)


def _refusal_reason(response) -> str | None:
    """The daemon's 422 reason: only a plain string ``detail`` (its own fixed refusal text), read with a small cap,
    stripped of control characters and truncated. Validation-error lists (which could echo input) yield nothing."""
    try:
        detail = json.loads(response.read(4096)).get("detail")
    except (ValueError, AttributeError, OSError, http.client.HTTPException):
        return None
    if not isinstance(detail, str):
        return None
    text = re.sub(r"[^\x20-\x7e]", "", detail).strip()[:_REASON_MAX]
    return text or None


def _run_profiles(result) -> list[str]:
    """Distinct account ids of an agent-show answer: tasks[].profile_id (claimed) else tasks[].spec.profile
    (requested), plus a top-level ``profile`` if the daemon returns one. Non-id values are ignored."""
    if not isinstance(result, dict):
        return []
    values = [result.get("profile")]
    for task in (result.get("tasks") if isinstance(result.get("tasks"), list) else [])[:50]:
        if isinstance(task, dict):
            spec = task.get("spec") if isinstance(task.get("spec"), dict) else {}
            values.append(task.get("profile_id") if task.get("profile_id") is not None else spec.get("profile"))
    found = []
    for value in values:
        if isinstance(value, str) and _PROFILE_ID.fullmatch(value) and value not in found:
            found.append(value)
    return found


def _profile_id(value: str) -> str:
    if _PROFILE_ID.fullmatch(value) is None:
        raise argparse.ArgumentTypeError("profile ID must be 1-64 characters of [A-Za-z0-9._-]")
    return value


def _request(state_dir: Path, endpoint: str, *, body: bytes | None = None,
             idempotency_key: str | None = None, correlation_id: str | None = None,
             approver_token: str | None = None, plain_method: str | None = None) -> object:
    """``plain_method``: an operator command without an Idempotency-Key (profile cooldowns), always answered 200."""
    token = _load_token(state_dir)
    path = state_dir / "agentd.sock"
    info = path.lstat()
    if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
        raise PermissionError("daemon socket must be private and user-owned")
    connection = UnixConnection(path)
    try:
        headers = {"Authorization": "Bearer " + token, "Accept": "application/json"}
        if plain_method is not None:
            method, expected = plain_method, 200
            headers["X-Correlation-ID"] = correlation_id or str(uuid.uuid4())
            if body is not None:
                headers["Content-Type"] = "application/json"
        elif body is None:
            method, expected = "GET", 200
        else:
            method = "POST"
            expected = 201 if endpoint == "/api/v1/workflows" else 200 if endpoint.endswith("/decision") else 202
            if endpoint.split("?", 1)[0].endswith(("/agent-abandon", "/agent-worktree-remove", "/agent-worktree-commit")) \
                    or "/candidate-manifests" in endpoint:
                expected = 200
            if endpoint == "/api/v1/agent-runs":
                expected = (200, 201)  # 201 new start, 200 idempotent replay
            headers.update({"Content-Type": "application/json", "Idempotency-Key": idempotency_key,
                            "X-Correlation-ID": correlation_id})
            if approver_token is not None:
                if _TOKEN.fullmatch(approver_token.encode("utf-8", "replace")):
                    headers["X-Approver-Token"] = approver_token
                else:  # passphrase mode: header-safe base64url of the UTF-8 text
                    headers["X-Approver-Passphrase"] = base64.urlsafe_b64encode(approver_token.encode("utf-8")).decode("ascii").rstrip("=")
        connection.request(method, endpoint, body=body, headers=headers)
        response = connection.getresponse()
        if response.status not in (expected if isinstance(expected, tuple) else (expected,)):
            until = response.getheader("X-Approver-Locked-Until") if response.status == 403 else None
            raise DaemonHTTPError(response.status, until if until and re.fullmatch(r"[0-2][0-9]:[0-5][0-9]", until) else None,
                                  _refusal_reason(response) if response.status in (409, 422) else None)
        data = response.read(_MAX_RESPONSE + 1)
        if len(data) > _MAX_RESPONSE:
            raise RuntimeError("daemon response exceeds size limit")
        return json.loads(data)
    finally:
        connection.close()


def _get(state_dir: Path, endpoint: str) -> object:
    """Read-only transport retained for the desktop poller."""
    return _request(state_dir, endpoint)


def _read_workflow(path: Path, *, kind: str = "workflow") -> bytes:
    fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError(f"{kind} input must be a regular file")
        with os.fdopen(fd, "rb", closefd=False) as stream:
            data = stream.read(_MAX_REQUEST + 1)
        if len(data) > _MAX_REQUEST:
            raise ValueError(f"{kind} input exceeds 1 MiB")
        return data
    finally:
        os.close(fd)


def _command_header(value: str) -> str:
    if _COMMAND_HEADER.fullmatch(value) is None:
        raise argparse.ArgumentTypeError("must be 1–256 printable non-space ASCII characters")
    return value


def _page_cursor(value: str) -> str:
    if not 1 <= len(value) <= 1024:
        raise argparse.ArgumentTypeError("must be 1-1024 characters (the previous next_cursor)")
    return value


def _integer(minimum: int, maximum: int):
    def parse(value: str) -> int:
        try:
            result = int(value)
        except ValueError:
            raise argparse.ArgumentTypeError("must be an integer") from None
        if not minimum <= result <= maximum:
            raise argparse.ArgumentTypeError(f"must be between {minimum} and {maximum}")
        return result
    return parse


def _approver_init_passphrase(state_dir: Path, *, rotate: bool) -> int:
    """Create the passphrase verifier. The passphrase is only ever read from a hidden prompt."""
    from synapse.infrastructure.passphrase_approver import MAX_LENGTH, MIN_LENGTH, provision_passphrase_verifier
    if not sys.stdin.isatty():
        print("agentctl: the passphrase must be typed at an interactive terminal", file=sys.stderr)
        return 1
    first = getpass.getpass(f"New approver passphrase ({MIN_LENGTH}-{MAX_LENGTH} characters): ")
    if getpass.getpass("Repeat passphrase: ") != first:
        print("agentctl: the passphrases do not match", file=sys.stderr)
        return 1
    try:
        directory = os.open(state_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW)
        try:
            info = os.fstat(directory)
            if info.st_uid != os.geteuid() or info.st_mode & 0o077:
                raise PermissionError("state directory must be private and user-owned")
            try:
                operator = _load_token(state_dir)
            except (OSError, ValueError):
                operator = None
            provision_passphrase_verifier(directory, first, operator_token=operator, rotate=rotate)
        finally:
            os.close(directory)
    except FileExistsError:
        print("agentctl: an approver passphrase already exists; use --rotate to replace it", file=sys.stderr)
        return 1
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"agentctl: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print("agentctl: passphrase verifier written; any approver.token was removed. Restart the daemon to use it.")
    return 0


def _approver_unlock(state_dir: Path) -> int:
    """Operator-only lockout reset. Writes a marker the daemon consumes; it needs no passphrase and can
    approve nothing, it only clears the failed-attempt counter. Requires an interactive terminal."""
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        print("agentctl: approver-unlock must be run at an interactive terminal", file=sys.stderr)
        return 1
    if input("Reset the approver lockout counter (this approves nothing)? Type 'unlock' to confirm: ").strip() != "unlock":
        print("agentctl: not confirmed", file=sys.stderr)
        return 1
    return _write_unlock_marker(state_dir)


def _write_unlock_marker(state_dir: Path) -> int:
    from synapse.infrastructure.passphrase_approver import UNLOCK_FILE
    try:
        directory = os.open(state_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW)
        try:
            info = os.fstat(directory)
            if info.st_uid != os.geteuid() or info.st_mode & 0o077:
                raise PermissionError("state directory must be private and user-owned")
            fd = os.open(UNLOCK_FILE, os.O_WRONLY | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600, dir_fd=directory)
            os.close(fd)
        finally:
            os.close(directory)
    except OSError as exc:
        print(f"agentctl: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print("agentctl: unlock requested; the daemon clears the lockout on the next approval attempt.")
    return 0


def _approver_init(state_dir: Path, *, rotate: bool) -> int:
    from synapse.infrastructure.local_approver import provision_approver_token
    from synapse.infrastructure.passphrase_approver import read_mode_hint
    if read_mode_hint(state_dir) == "passphrase":
        print("agentctl: passphrase mode is selected; remove approver.json deliberately to return to token mode",
              file=sys.stderr)
        return 1
    try:
        directory = os.open(state_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW)
        try:
            info = os.fstat(directory)
            if info.st_uid != os.geteuid() or info.st_mode & 0o077:
                raise PermissionError("state directory must be private and user-owned")
            try:
                operator = _load_token(state_dir)
            except (OSError, ValueError):
                operator = None
            token = provision_approver_token(directory, operator_token=operator, rotate=rotate)
        finally:
            os.close(directory)
    except FileExistsError:
        print("agentctl: approver token already exists; use --rotate to replace it", file=sys.stderr)
        return 1
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"agentctl: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print(token)
    return 0


def _blob_backup_command(args) -> int:
    from synapse.application import blob_backup as bb
    from synapse.infrastructure.artifact_blobs import ArtifactIntegrityError
    try:
        if args.command == "blobs-backup":
            info = bb.backup_blobs(args.state_dir, args.db_backup, args.dest)
            print(f"blob backup written: {args.dest} ({info.blob_count} blobs, {info.total_bytes} bytes)")
        elif args.command == "blobs-verify":
            info = bb.verify_blob_backup(args.dir, args.db_backup)
            print(f"blob backup ok: {info.blob_count} blobs, {info.total_bytes} bytes")
        else:
            info = bb.restore_blobs(args.state_dir, args.dir, args.db_backup)
            print(f"blobs restored: {info.blob_count} blobs, {info.total_bytes} bytes")
    except (bb.BlobBackupError, ArtifactIntegrityError, OSError, ValueError) as exc:
        print(f"agentctl: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    return 0


def _backup_command(args) -> int:
    from synapse.persistence import backup as bk
    try:
        if args.command == "backup":
            info = bk.create_backup(args.state_dir, args.dest, args.migrations)
            print(f"backup written: {args.dest} (schema version {info.schema_version}, {info.size} bytes)")
        elif args.command == "backup-verify":
            info = bk.verify_backup(args.file, args.migrations)
            print(f"backup ok: schema version {info.schema_version}, {info.size} bytes")
        else:
            aside = bk.restore_backup(args.state_dir, args.file, args.migrations)
            print("restored; previous database kept at " + (str(aside) if aside else "(none existed)"))
    except (bk.BackupError, OSError, ValueError) as exc:
        print(f"agentctl: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    return 0


def _add_runs_parser(commands) -> None:
    runs = commands.add_parser("runs", help="list runs, newest first")
    runs.add_argument("--state", choices=("created", "queued", "running", "awaiting_approval", "paused",
                                          "recovering", "succeeded", "failed", "cancelled"))
    runs.add_argument("--limit", type=_integer(1, 200), default=50)
    runs.add_argument("--cursor", help="opaque next_cursor from the previous page")
    runs.add_argument("--json", action="store_true", help="print the raw JSON response")


def _runs_command(args) -> int:
    query = {"limit": args.limit}
    if args.state:
        query["state"] = args.state
    if args.cursor:
        query["cursor"] = args.cursor
    try:
        result = _request(args.state_dir, "/api/v1/runs?" + urlencode(query))
    except (OSError, ValueError, RuntimeError, http.client.HTTPException) as exc:
        print(f"agentctl: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(result, ensure_ascii=False))
        return 0
    print(f"{'RUN':<36} {'WORKFLOW':<20} {'STATE':<18} {'TASKS':<24} APPR  CREATED")
    for run in result["runs"]:
        tasks = ",".join(f"{k}={v}" for k, v in sorted(run["task_counts"].items())) or "-"
        workflow = f"{run['workflow_id']}@{run['workflow_version']}"
        print(f"{run['id']:<36} {workflow:<20} {run['state']:<18} {tasks:<24} {run['pending_approvals']:<5} {run['created_at']}")
    if result.get("next_cursor"):
        print(f"next: --cursor {result['next_cursor']}")
    return 0


_RESTARTABLE = ("recovering", "failed", "cancelled", "succeeded")


def _restart_command(args, parser) -> int:
    """New run from an old one's recorded request. The prompt is stored only as a hash, so it is passed again and checked."""
    if not args.run_id or len(args.run_id) > 256 or "/" in args.run_id:
        parser.error("run ID must be a nonempty path segment up to 256 characters")
    try:
        old = _request(args.state_dir, "/api/v1/runs/" + quote(args.run_id, safe="") + "/agent")
        tasks = old.get("tasks") or []
        if old.get("run_state") not in _RESTARTABLE or len(tasks) != 1:
            print("agentctl: only a finished or recovering single-task agent run can be restarted", file=sys.stderr)
            return 1
        spec = tasks[0]["spec"]
        prompt = _read_prompt(args.prompt_file)
        if hashlib.sha256(prompt.encode("utf-8")).hexdigest() != spec["prompt_sha256"] and not args.allow_changed_prompt:
            print("agentctl: the prompt differs from the old run's prompt (use --allow-changed-prompt to accept)", file=sys.stderr)
            return 1
        body = {"repo": spec["repo"], "base_ref": spec["base_ref"], "prompt": prompt, "allowed_tools": spec["allowed_tools"],
                "permission_mode": spec["permission_mode"], "timeout_seconds": spec["timeout_seconds"]}
        step = spec.get("approval_step")
        if isinstance(step, dict) and step.get("operation") and step.get("target"):
            body["approval_step"] = {k: step[k] for k in ("operation", "target", "ttl_seconds") if step.get(k) is not None}
        print("old run %s is %s; its worktree change: %s" % (args.run_id, old["run_state"],
              ((old.get("worktree") or {}).get("diff_stat") or "none recorded").strip()), file=sys.stderr)
        result = _request(args.state_dir, "/api/v1/agent-runs", body=json.dumps(body, ensure_ascii=True).encode("utf-8"),
                          idempotency_key=args.idempotency_key or str(uuid.uuid4()),
                          correlation_id=args.correlation_id or str(uuid.uuid4()))
    except (OSError, ValueError, RuntimeError, KeyError, http.client.HTTPException) as exc:
        print(f"agentctl: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


_PRUNE_AGE = re.compile(r"([1-9][0-9]{0,4})([hd])")
_PRUNE_STATES = ("succeeded", "failed", "cancelled")


def _prune_command(args) -> int:
    """Client side only: lists finished agent runs and calls the audited, never-forced worktree removal."""
    match = _PRUNE_AGE.fullmatch(args.older_than)
    if match is None:
        print("agentctl: --older-than must look like 12h or 7d", file=sys.stderr)
        return 2
    cutoff = datetime.now(timezone.utc) - timedelta(**{"hours" if match[2] == "h" else "days": int(match[1])})
    removed = kept = failed = 0
    try:
        for state in _PRUNE_STATES:
            cursor = None
            while True:
                query = {"limit": 100, "state": state, **({"cursor": cursor} if cursor else {})}
                page = _request(args.state_dir, "/api/v1/runs?" + urlencode(query))
                for run in page["runs"]:
                    if not run["workflow_id"].startswith("synapse.agent.") \
                            or datetime.fromisoformat(run["updated_at"]) >= cutoff:
                        continue
                    if args.dry_run:
                        print(f"would remove {run['id']} ({state}, last change {run['updated_at']})")
                        continue
                    try:
                        result = _request(args.state_dir, "/api/v1/runs/" + quote(run["id"], safe="") + "/agent-worktree-remove",
                                          body=b"", idempotency_key=str(uuid.uuid4()), correlation_id=str(uuid.uuid4()))
                    except RuntimeError as exc:  # 409: uncommitted changes or removal refused; the tree stays
                        kept += 1
                        print(f"kept {run['id']}: {exc}")
                        continue
                    if result.get("removed"):
                        removed += 1
                        print(f"removed {run['id']}")
                cursor = page.get("next_cursor")
                if not cursor:
                    break
    except (OSError, ValueError, RuntimeError, http.client.HTTPException) as exc:
        print(f"agentctl: {type(exc).__name__}: {exc}", file=sys.stderr)
        failed = 1
    if not args.dry_run:
        print(f"worktrees removed: {removed}, kept: {kept}")
    return 1 if failed else 0


_MAX_TASKS_FILE = 6 * 100 * 1024  # JSON escaping can inflate a 32 KiB prompt; the server still enforces 32 KiB per prompt
_MAX_PROMPT = 32 * 1024


def _read_prompt(source: str, limit: int = _MAX_PROMPT) -> str:
    """Read the prompt from a regular file or stdin ('-'); it is never taken from argv."""
    if source == "-":
        data = sys.stdin.buffer.read(limit + 1)
    else:
        try:
            fd = os.open(source, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK)
        except OSError:
            # argparse accepts "--prompt TEXT" as an abbreviation of --prompt-file: never echo the value.
            raise ValueError("cannot read the prompt file (pass a path or - for stdin, never the prompt text)") from None
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise ValueError("prompt file must be a regular file")
            data = os.read(fd, limit + 1)
        finally:
            os.close(fd)
    if len(data) > limit:
        raise ValueError("input exceeds %d KiB" % (limit // 1024))
    return data.decode("utf-8")


def _add_agent_parsers(commands) -> None:
    start = commands.add_parser("agent-start", help="start a REAL Claude agent run in a fresh git worktree")
    start.add_argument("--repo", required=True, help="absolute path of an operator-allowlisted repository")
    start.add_argument("--base-ref", default="HEAD", help="commit-ish to branch the worktree from (default HEAD)")
    # One of the two is required, so a mistyped prompt option fails with "one of ... is required" and
    # argparse never echoes the stray value (which could be prompt text) in an "unrecognized arguments" error.
    source = start.add_mutually_exclusive_group(required=True)
    source.add_argument("--prompt-file", help="file holding the prompt, or - for stdin (never argv)")
    source.add_argument("--tasks-file", help="JSON file with the \"tasks\" list (id, prompt, allowed_tools, depends_on ...) "
                                             "for a multi-task run, instead of --prompt-file/--tool; - for stdin")
    start.add_argument("--tool", action="append", dest="tools", help="allowed tool (repeatable; with --prompt-file)")
    start.add_argument("--profile", type=_profile_id,
                       help="Claude account profile for this run (see `agentctl profiles`); omitted: the daemon default. "
                            "There is no fallback: an unknown or disabled profile is refused at start (HTTP 422); a profile in a "
                            "cooldown is not refused at start, the task fails closed before spawning")
    start.add_argument("--permission-mode", default="acceptEdits", choices=("acceptEdits", "plan", "dontAsk", "manual"))
    start.add_argument("--timeout-seconds", type=float, default=600)
    start.add_argument("--approval-operation")
    start.add_argument("--approval-target")
    start.add_argument("--approval-ttl-seconds", type=_integer(1, 86400))
    start.add_argument("--idempotency-key", required=True, type=_command_header)
    start.add_argument("--correlation-id", required=True, type=_command_header)
    commands.add_parser("agent-show", help="show a real agent run: outcome, summary, worktree diff").add_argument("run_id")
    cancel = commands.add_parser("agent-cancel", help="request cancellation of a running real agent run")
    cancel.add_argument("run_id")
    cancel.add_argument("--idempotency-key", type=_command_header)
    cancel.add_argument("--correlation-id", type=_command_header)
    abandon = commands.add_parser("agent-abandon", help="end a RECOVERING real agent run (fails it, cancels its tasks)")
    abandon.add_argument("run_id")
    abandon.add_argument("--expected-run-version", required=True, type=_integer(0, 2**31 - 1))
    abandon.add_argument("--assert-no-agent-process-remains", action="store_true", required=True,
                         help="you have checked that no agent process for this run is still running")
    abandon.add_argument("--idempotency-key", type=_command_header)
    abandon.add_argument("--correlation-id", type=_command_header)
    remove = commands.add_parser("agent-worktree-remove", help="remove the worktree of a FINISHED real agent run")
    remove.add_argument("run_id")
    remove.add_argument("--force", action="store_true",
                        help="discard uncommitted changes and unmerged commits on the run's branch")
    remove.add_argument("--idempotency-key", type=_command_header)
    remove.add_argument("--correlation-id", type=_command_header)
    prune = commands.add_parser("agent-worktree-prune", help="remove the worktrees of FINISHED real agent runs older "
                                "than a given age (never forced: a tree with uncommitted changes is kept)")
    prune.add_argument("--older-than", default="7d", metavar="AGE",
                       help="minimum time since the run last changed, such as 12h or 7d (default 7d)")
    prune.add_argument("--dry-run", action="store_true", help="list what would be removed and remove nothing")
    restart = commands.add_parser("agent-restart", help="start a NEW run with the same repo, base ref, tools and limits as a "
                                  "stuck or finished single-task agent run (the old run is not touched)")
    restart.add_argument("run_id")
    restart.add_argument("--prompt-file", required=True,
                         help="the prompt again (file, or - for stdin); it must match the old run's stored SHA-256")
    restart.add_argument("--allow-changed-prompt", action="store_true", help="accept a prompt that differs from the old one")
    restart.add_argument("--idempotency-key", type=_command_header)
    restart.add_argument("--correlation-id", type=_command_header)
    commit = commands.add_parser("agent-worktree-commit", help="commit a FINISHED real agent run's edits onto its own "
                                 "branch (host-side; the sandboxed agent cannot commit; never touches your checkout)")
    commit.add_argument("run_id")
    commit.add_argument("--message", "-m", required=True, help="commit message (max 2000 characters)")
    commit.add_argument("--idempotency-key", type=_command_header)
    commit.add_argument("--correlation-id", type=_command_header)


def _add_manifest_parsers(commands) -> None:
    def common(p, key=True):
        p.add_argument("run_id")
        if key:
            p.add_argument("--idempotency-key", type=_command_header)
            p.add_argument("--correlation-id", type=_command_header)
    build = commands.add_parser("manifest-build", help="build the candidate manifest v2 of a finished multi-repository run")
    common(build)
    build.add_argument("--source", action="append", default=[], metavar="TASK=ATTEMPT",
                       help="use this attempt of a writer task (default: its latest completed attempt); repeatable")
    commands.add_parser("manifest-list", help="list a run's candidate manifests with their invalidations").add_argument("run_id")
    show = commands.add_parser("manifest-show", help="show one candidate manifest, its invalidations and diffs")
    show.add_argument("run_id")
    show.add_argument("manifest_sha256", type=_sha256_arg)
    recheck = commands.add_parser("manifest-recheck", help="re-verify a manifest (records baseline_moved or candidate_invalid)")
    common(recheck)
    recheck.add_argument("manifest_sha256", type=_sha256_arg)
    approve = commands.add_parser("manifest-approve", help="request a deploy approval bound to a live manifest (rechecked first); "
                                  "the owner decides it with approval-decide")
    common(approve)
    approve.add_argument("manifest_sha256", type=_sha256_arg)
    approve.add_argument("--environment", required=True, choices=sorted(ALLOWED_ENVIRONMENTS),
                         help="an ADR 022 allowlisted environment (" + ", ".join(sorted(ALLOWED_ENVIRONMENTS)) + ")")
    approve.add_argument("--policy-version", default="1")
    approve.add_argument("--ttl-seconds", type=_integer(1, 3600), default=3600)


def _sha256_arg(value: str) -> str:
    if re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise argparse.ArgumentTypeError("must be 64 lowercase hex characters")
    return value


def _manifest_command(args, parser) -> int:
    import uuid
    if not args.run_id.strip() or len(args.run_id) > 256 or "/" in args.run_id or args.run_id in (".", ".."):
        parser.error("run ID must be a nonempty path segment up to 256 characters")
    base = "/api/v1/runs/" + quote(args.run_id, safe="") + "/candidate-manifests"
    def post(endpoint, body):
        return _request(args.state_dir, endpoint, body=json.dumps(body).encode("utf-8"),
                        idempotency_key=args.idempotency_key or str(uuid.uuid4()),
                        correlation_id=args.correlation_id or str(uuid.uuid4()))
    try:
        if args.command == "manifest-build":
            sources = {}
            for item in args.source:
                task, _, attempt = item.partition("=")
                if (not task or not attempt.isdecimal() or not 1 <= int(attempt) <= 2**31 - 1 or task in sources
                        or re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", task) is None):
                    parser.error("--source must be TASK=ATTEMPT (attempt 1 to 2**31-1), once per task")
                sources[task] = int(attempt)
            result = post(base, {"sources": sources} if sources else {})
        elif args.command == "manifest-list":
            result = _request(args.state_dir, base)
        elif args.command == "manifest-show":
            result = _request(args.state_dir, base + "/" + args.manifest_sha256)
        elif args.command == "manifest-recheck":
            result = _request(args.state_dir, base + "/" + args.manifest_sha256 + "/recheck", body=b"",
                              idempotency_key=args.idempotency_key or str(uuid.uuid4()),
                              correlation_id=args.correlation_id or str(uuid.uuid4()))
        else:
            result = post(base + "/" + args.manifest_sha256 + "/approval",
                          {"environment": args.environment, "policy_version": args.policy_version,
                           "ttl_seconds": args.ttl_seconds})
    except (OSError, ValueError, RuntimeError, http.client.HTTPException) as exc:
        print(f"agentctl: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


def _agent_command(args, parser) -> int:
    import uuid
    if args.command != "agent-start" and (not args.run_id.strip() or len(args.run_id) > 256 or "/" in args.run_id
                                          or args.run_id in (".", "..")):
        parser.error("run ID must be a nonempty path segment up to 256 characters")
    try:
        if args.command == "agent-start":
            if bool(args.approval_operation) != bool(args.approval_target):
                parser.error("--approval-operation and --approval-target must be given together")
            if args.tasks_file:
                if args.tools or args.approval_operation:
                    parser.error("--tasks-file excludes --tool and --approval-*")
                try:
                    tasks = json.loads(_read_prompt(args.tasks_file, _MAX_TASKS_FILE))
                except json.JSONDecodeError:
                    raise ValueError("tasks file is not valid JSON") from None
                if not isinstance(tasks, list):
                    raise ValueError("tasks file must hold a JSON list of tasks")
                for task in tasks:
                    if isinstance(task, dict) and task.get("profile") is not None and (
                            not isinstance(task["profile"], str) or _PROFILE_ID.fullmatch(task["profile"]) is None):
                        raise ValueError("a task's profile must be 1-64 characters of [A-Za-z0-9._-]")
                body = {"repo": args.repo, "base_ref": args.base_ref, "tasks": tasks,
                        "permission_mode": args.permission_mode, "timeout_seconds": args.timeout_seconds}
            else:
                if not args.tools:
                    parser.error("--tool is required with --prompt-file")
                body = {"repo": args.repo, "base_ref": args.base_ref, "prompt": _read_prompt(args.prompt_file),
                        "allowed_tools": args.tools, "permission_mode": args.permission_mode,
                        "timeout_seconds": args.timeout_seconds}
            profile = args.profile or read_default_profile(args.state_dir)  # the operator's default account when none is named
            if profile:
                body["profile"] = profile
            if args.approval_operation:
                step = {"operation": args.approval_operation, "target": args.approval_target}
                if args.approval_ttl_seconds is not None:
                    step["ttl_seconds"] = args.approval_ttl_seconds
                body["approval_step"] = step
            result = _request(args.state_dir, "/api/v1/agent-runs",
                              body=json.dumps(body, ensure_ascii=True).encode("utf-8"),
                              idempotency_key=args.idempotency_key, correlation_id=args.correlation_id)
        elif args.command == "agent-show":
            result = _request(args.state_dir, "/api/v1/runs/" + quote(args.run_id, safe="") + "/agent")
            for account in _run_profiles(result):
                print("profile: " + account, file=sys.stderr)
        elif args.command == "agent-abandon":
            result = _request(args.state_dir, "/api/v1/runs/" + quote(args.run_id, safe="") + "/agent-abandon",
                              body=json.dumps({"expected_run_version": args.expected_run_version,
                                               "assertion": "no-agent-process-remains"}).encode("utf-8"),
                              idempotency_key=args.idempotency_key or "agentctl-abandon-%s-%d" % (args.run_id[:180], args.expected_run_version),
                              correlation_id=args.correlation_id or str(uuid.uuid4()))
        elif args.command == "agent-worktree-remove":
            result = _request(args.state_dir, "/api/v1/runs/" + quote(args.run_id, safe="") + "/agent-worktree-remove"
                              + ("?force=true" if args.force else ""), body=b"",
                              idempotency_key=args.idempotency_key or str(uuid.uuid4()),
                              correlation_id=args.correlation_id or str(uuid.uuid4()))
        elif args.command == "agent-worktree-commit":
            result = _request(args.state_dir, "/api/v1/runs/" + quote(args.run_id, safe="") + "/agent-worktree-commit",
                              body=json.dumps({"message": args.message}).encode("utf-8"),
                              idempotency_key=args.idempotency_key or str(uuid.uuid4()),
                              correlation_id=args.correlation_id or str(uuid.uuid4()))
        else:
            result = _request(args.state_dir, "/api/v1/runs/" + quote(args.run_id, safe="") + "/agent-cancel", body=b"",
                              idempotency_key=args.idempotency_key or "agentctl-cancel-" + args.run_id[:200],
                              correlation_id=args.correlation_id or str(uuid.uuid4()))
    except (OSError, ValueError, RuntimeError, http.client.HTTPException) as exc:
        # Never echo the prompt, a server body or request headers.
        print(f"agentctl: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


def _profiles_table(rows: list) -> str:
    """id, enabled, cooldown, tolerant of missing fields; control characters are stripped from every cell."""
    def cell(value) -> str:
        return re.sub(r"[^\x20-\x7e]", "", str(value))[:64]
    lines = ["%-32s %-8s %s" % ("ID", "ENABLED", "COOLDOWN")]
    for row in rows[:100]:
        if not isinstance(row, dict):
            continue
        remaining = row.get("cooldown_remaining_seconds")
        cooling = isinstance(remaining, (int, float)) and not isinstance(remaining, bool) and remaining > 0
        lines.append("%-32s %-8s %s" % (cell(row.get("id", "?")), "yes" if row.get("enabled") else "no",
                                        "cooling down %.0f s" % remaining if cooling else "-"))
    return "\n".join(lines)


def _profile_command(args, parser) -> int:
    try:
        if args.command == "profiles":
            result = _request(args.state_dir, "/api/v1/profiles")
            if not args.json:
                if not isinstance(result, dict) or not isinstance(result.get("profiles"), list):
                    raise ValueError("unexpected profiles response")
                print(_profiles_table(result["profiles"]))
                return 0
        else:
            if re.fullmatch(r"[A-Za-z0-9._][A-Za-z0-9._-]{0,63}", args.profile) is None:
                parser.error("profile ID must be 1-64 characters of [A-Za-z0-9._-]")
            endpoint = "/api/v1/profiles/" + args.profile + "/cooldown"
            if args.clear:
                result = _request(args.state_dir, endpoint, plain_method="DELETE")
            else:
                if not 0 < args.seconds <= 86400 or args.seconds != args.seconds:
                    parser.error("--seconds must be above 0 and at most 86400")
                result = _request(args.state_dir, endpoint, body=json.dumps({"seconds": args.seconds}).encode("utf-8"),
                                  plain_method="POST")
    except (OSError, ValueError, RuntimeError, http.client.HTTPException) as exc:
        print(f"agentctl: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Inspect the local Synapse daemon")
    parser.add_argument("--state-dir", type=Path, required=True, help="private daemon state directory")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("health", help="show daemon health")
    events = commands.add_parser("events", help="replay one page of events (--follow to tail them)")
    events.add_argument("--after", type=_integer(0, _MAX_SEQUENCE), default=None)
    events.add_argument("--limit", type=_integer(1, 1000), default=100)
    events.add_argument("--follow", action="store_true",
                        help="keep reading, print one JSON event per line, resume from a saved cursor")
    events.add_argument("--cursor-file", type=Path, default=None, help="cursor file (default: STATE_DIR/events.cursor)")
    events.add_argument("--interval", type=float, default=1.0, help="seconds between polls when idle (--follow)")
    events.add_argument("--stop-when-idle", action="store_true", help="with --follow: exit once caught up")
    for command in ("run", "tasks", "usage"):
        query = commands.add_parser(command, help=f"show run {command} details" + (
            "" if command == "run" else " (one page; follow next_cursor with --cursor)"))
        query.add_argument("run_id", help="run identifier")
        if command != "run":
            query.add_argument("--limit", type=_integer(1, 500), default=200)
            query.add_argument("--cursor", type=_page_cursor, help="opaque next_cursor from the previous page")
    analytics = commands.add_parser("analytics", help="show run analytics (measured/estimated/unknown; one page of tasks)")
    analytics.add_argument("run_id", help="run identifier")
    analytics.add_argument("--limit", type=_integer(1, 200), default=50, help="tasks per page (1-200)")
    analytics.add_argument("--cursor", type=_page_cursor, help="opaque next_cursor from the previous page")
    threads = commands.add_parser("threads", help="list one page of run threads")
    threads.add_argument("run_id", help="run identifier")
    messages = commands.add_parser("messages", help="list one page of thread messages")
    messages.add_argument("run_id", help="run identifier")
    messages.add_argument("thread_id", help="thread identifier")
    for query in (threads, messages):
        query.add_argument("--after", type=_integer(0, _MAX_SEQUENCE), default=0)
        query.add_argument("--limit", type=_integer(1, 100), default=100)
    commands.add_parser("profiles", help="list agent profiles (id, enabled, cooldown); --json for the full daemon answer"
                        ).add_argument("--json", action="store_true", help="print the daemon's JSON instead of the table")
    cooldown = commands.add_parser("profile-cooldown", help="set or clear an operator cooldown on a declared profile")
    cooldown.add_argument("profile", help="profile ID")
    group = cooldown.add_mutually_exclusive_group(required=True)
    group.add_argument("--seconds", type=float, help="refuse tasks naming the profile for this long (0 < N <= 86400)")
    group.add_argument("--clear", action="store_true", help="end the cooldown now")
    approvals = commands.add_parser("approvals", help="list one page of approvals")
    approvals.add_argument("--after", default="", help="last approval ID from previous page")
    approvals.add_argument("--limit", type=_integer(1, 1000), default=100)
    _add_runs_parser(commands)
    _add_agent_parsers(commands)
    _add_manifest_parsers(commands)
    publish = commands.add_parser("publish-workflow", help="publish a workflow definition from JSON")
    publish.add_argument("file", type=Path, help="regular JSON workflow file")
    queue = commands.add_parser("queue-run", help="create a queued run")
    queue.add_argument("workflow_id")
    queue.add_argument("workflow_version")
    init = commands.add_parser("approver-init", help="create the human approver token (printed once) or, with --passphrase, a passphrase verifier")
    init.add_argument("--rotate", action="store_true", help="replace an existing approver token/passphrase (revokes the old one)")
    init.add_argument("--passphrase", action="store_true",
                      help="opt in to passphrase mode: prompt for a passphrase and store only a scrypt verifier")
    commands.add_parser("approver-unlock", help="operator-only: reset the approver lockout counter (needs a TTY; needs no passphrase; approves nothing)")
    decide = commands.add_parser("approval-decide", help="approve or reject a request; prompts for the approver token or passphrase")
    decide.add_argument("approval_id")
    decide.add_argument("--decision", required=True, choices=("approved", "rejected"))
    decide.add_argument("--expected-version", required=True, type=_integer(0, _MAX_SEQUENCE))
    decide.add_argument("--operation", required=True)
    decide.add_argument("--target", required=True)
    decide.add_argument("--candidate-sha256", required=True)
    decide.add_argument("--policy-version", required=True)
    decide.add_argument("--reason")
    for name, text in (("backup", "write a verified online snapshot of the state database"),
                       ("backup-verify", "verify a backup file"),
                       ("restore", "OFFLINE ONLY: replace the state database from a backup")):
        sub = commands.add_parser(name, help=text)
        sub.add_argument("dest" if name == "backup" else "file", type=Path)
        sub.add_argument("--migrations", type=Path, default=default_migrations_dir(),
                         help="migration directory defining the supported schema version")
    for name, text in (("blobs-backup", "copy the artifact blobs a database backup references into a new directory"),
                       ("blobs-verify", "verify a blob backup against its database backup"),
                       ("blobs-restore", "add a verified blob backup to the state directory (run after restore)")):
        sub = commands.add_parser(name, help=text)
        sub.add_argument("dest" if name == "blobs-backup" else "dir", type=Path)
        sub.add_argument("--db-backup", type=Path, required=True,
                         help="the database backup file this blob backup belongs to")
    mock_start = commands.add_parser("mock-start", help="start the daemon's fixed synthetic mock fixture")
    mock_start.add_argument("run_id", help="queued run identifier")
    mock_cancel = commands.add_parser("mock-cancel", help="request cancellation of a running synthetic mock run")
    mock_cancel.add_argument("run_id", help="running mock run identifier")
    mock_resume = commands.add_parser("mock-resume", help="resume only a reconciled fixed synthetic mock run")
    mock_resume.add_argument("run_id", help="recovering run identifier")
    mock_resume.add_argument("evidence_file", type=Path, help="explicit operator-authored JSON evidence")
    for command in (publish, queue, mock_start, mock_cancel, mock_resume, decide):
        command.add_argument("--idempotency-key", required=True, type=_command_header)
        command.add_argument("--correlation-id", required=True, type=_command_header)
    args = parser.parse_args(argv)
    if args.command == "runs":
        return _runs_command(args)
    if args.command == "agent-worktree-prune":
        return _prune_command(args)
    if args.command == "agent-restart":
        return _restart_command(args, parser)
    if args.command in ("profiles", "profile-cooldown"):
        return _profile_command(args, parser)
    if args.command in ("agent-start", "agent-show", "agent-cancel", "agent-abandon", "agent-worktree-remove",
                        "agent-worktree-commit"):
        return _agent_command(args, parser)
    if args.command.startswith("manifest-"):
        return _manifest_command(args, parser)
    if args.command == "approver-unlock":
        return _approver_unlock(args.state_dir)
    if args.command == "approver-init":
        if args.passphrase:
            return _approver_init_passphrase(args.state_dir, rotate=args.rotate)
        return _approver_init(args.state_dir, rotate=args.rotate)
    if args.command in ("backup", "backup-verify", "restore"):
        return _backup_command(args)
    if args.command in ("blobs-backup", "blobs-verify", "blobs-restore"):
        return _blob_backup_command(args)
    if args.command in ("run", "tasks", "usage", "analytics", "threads", "messages", "mock-start", "mock-cancel", "mock-resume"):
        if (not args.run_id.strip() or len(args.run_id) > 256 or "/" in args.run_id
                or args.run_id in (".", "..")):
            parser.error("run ID must be a nonempty path segment up to 256 characters")
        endpoint = "/api/v1/runs/" + quote(args.run_id, safe="")
        if args.command in ("tasks", "usage"):
            endpoint += "/" + args.command + "?" + urlencode(
                {"limit": args.limit, **({"cursor": args.cursor} if args.cursor is not None else {})})
        elif args.command == "analytics":
            endpoint += "/analytics?" + urlencode(
                {"limit": args.limit, **({"cursor": args.cursor} if args.cursor is not None else {})})
        elif args.command in ("mock-start", "mock-cancel", "mock-resume"):
            endpoint += "/" + args.command
        elif args.command in ("threads", "messages"):
            endpoint += "/threads"
            if args.command == "messages":
                if (not args.thread_id.strip() or len(args.thread_id) > 256 or "/" in args.thread_id
                        or args.thread_id in (".", "..")):
                    parser.error("thread ID must be a nonempty path segment up to 256 characters")
                endpoint += "/" + quote(args.thread_id, safe="") + "/messages"
            endpoint += "?" + urlencode({"after": args.after, "limit": args.limit})
    elif args.command == "publish-workflow":
        endpoint = "/api/v1/workflows"
    elif args.command == "queue-run":
        if any(not value.strip() or len(value) > 256 for value in (args.workflow_id, args.workflow_version)):
            parser.error("workflow ID and version must be nonempty and at most 256 characters")
        endpoint = "/api/v1/runs"
    elif args.command == "approval-decide":
        if (not args.approval_id.strip() or len(args.approval_id) > 256 or "/" in args.approval_id
                or args.approval_id in (".", "..")):
            parser.error("approval ID must be a nonempty path segment up to 256 characters")
        endpoint = "/api/v1/approvals/" + quote(args.approval_id, safe="") + "/decision"
    elif args.command == "approvals":
        if len(args.after) > 256:
            parser.error("approval cursor must be at most 256 characters")
        endpoint = "/api/v1/approvals?" + urlencode({"after": args.after, "limit": args.limit})
    elif args.command == "events":
        if args.follow:
            from synapse.cli.follow import CURSOR_NAME, follow_events
            if not 0.05 <= args.interval <= 60:
                parser.error("--interval must be between 0.05 and 60 seconds")
            default_cursor = args.state_dir / CURSOR_NAME
            cursor_file = args.cursor_file or default_cursor
            return follow_events(_follow_fetch(args.state_dir), cursor_file=cursor_file,
                                 # only a file other than the default one may be rewound by --after
                                 cursor_file_explicit=cursor_file.resolve() != default_cursor.resolve(),
                                 after=args.after,
                                 limit=args.limit, interval=args.interval, stop_when_idle=args.stop_when_idle)
        endpoint = "/api/v1/events?" + urlencode({"after": args.after or 0, "limit": args.limit})
    else:
        endpoint = "/api/v1/system/health"
    try:
        body = None
        if args.command == "publish-workflow":
            body = _read_workflow(args.file)
        elif args.command == "mock-resume":
            body = _read_workflow(args.evidence_file, kind="reconciliation evidence")
        elif args.command == "queue-run":
            body = json.dumps({"workflow_id": args.workflow_id, "workflow_version": args.workflow_version},
                              ensure_ascii=True).encode("utf-8")
        elif args.command in ("mock-start", "mock-cancel"):
            body = b""
        approver_token = None
        if args.command == "approval-decide":
            from synapse.infrastructure.passphrase_approver import passphrase_shape_problem, read_mode_hint
            if read_mode_hint(args.state_dir) == "passphrase":
                # Passphrase mode: hidden prompt only; never the environment and never argv.
                approver_token = getpass.getpass("Approver passphrase: ")
                if passphrase_shape_problem(approver_token) is not None:
                    print("agentctl: the approver passphrase has an invalid length", file=sys.stderr)
                    return 1
            else:
                approver_token = (os.environ.get("SYNAPSE_APPROVER_TOKEN") or getpass.getpass("Approver token: ")).strip()
            if read_mode_hint(args.state_dir) != "passphrase" and _TOKEN.fullmatch(approver_token.encode("ascii", "replace")) is None:
                # Never echo the supplied value or let http.client format it into an error.
                print("agentctl: the approver token is not a valid 43-character token", file=sys.stderr)
                return 1
            if args.reason is not None and approver_token in args.reason:
                print("agentctl: the reason must not contain the approver token", file=sys.stderr)
                return 1
            body = json.dumps({"decision": args.decision, "expected_version": args.expected_version,
                               "operation": args.operation, "target": args.target,
                               "candidate_sha256": args.candidate_sha256, "policy_version": args.policy_version,
                               "reason": args.reason}, ensure_ascii=True).encode("utf-8")
        extra = {} if approver_token is None else {"approver_token": approver_token}
        result = _request(args.state_dir, endpoint, body=body,
                          idempotency_key=getattr(args, "idempotency_key", None),
                          correlation_id=getattr(args, "correlation_id", None), **extra)
    except (OSError, ValueError, RuntimeError, http.client.HTTPException) as exc:
        # Never include a server body or request headers, which may contain secrets.
        print(f"agentctl: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False))
    if args.command in ("tasks", "usage", "analytics") and isinstance(result, dict) and result.get("next_cursor") is not None:
        # stdout stays one JSON document; the hint that more pages exist goes to stderr so it is never missed.
        cursor = result["next_cursor"]
        shown = cursor if isinstance(cursor, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,1024}", cursor) else "(invalid)"
        noun = "tasks" if args.command == "analytics" else args.command
        print(f"agentctl: more {noun} exist; next page: --cursor {shown}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
