"""Background poller: one QThread owning all daemon reads and serialized commands.

Panes never talk to the daemon; they react to this thread's signals.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from threading import Event, Lock
from urllib.parse import quote, urlencode

from PySide6.QtCore import QThread, Signal

from synapse.cli.__main__ import DaemonHTTPError, _get, _request
from synapse.desktop import approval_decision
from synapse.desktop.agent_usage import MAX_ROWS, sanitize_usage
from synapse.desktop.agent_view import UNAVAILABLE_STATUSES, clean, sanitize_detail
from synapse.desktop.cursor_store import Cursor, CursorError, cursor_path, read_cursor, valid_event_id, write_cursor
from synapse.desktop.mock_resume import ASSERTION, collect_evidence, fetch_all_tasks

API_VERSION = "v1"  # the only API prefix this client speaks (/api/v1/...)
MAX_BACKOFF = 15.0
EVENT_PAGE = 100
FEED_EVERY = 3  # the Control Center's run list and message feed refresh every Nth poll
FEED_THREADS = 8  # threads polled per feed cycle; the rest are visited round-robin on later cycles
FEED_MESSAGES = 50
FEED_PAGES = 2  # message pages per thread per cycle, so a long backlog is caught up over several cycles
APPROVAL_PAGES = 5  # pages (100 rows each) the Control Center walks, independent of the Developer tab's paging
AGENT_RETRY_POLLS = 10  # after 'unavailable', re-probe the agent endpoint only every Nth poll


def backoff_delay(interval: float, failures: int, cap: float = MAX_BACKOFF) -> float:
    """Exponential reconnect delay: interval, 2x, 4x ... capped; failures counts consecutive failures (>= 1)."""
    return min(cap, interval * (2 ** max(0, min(failures, 16) - 1)))


def _op_response_ok(action, run_id, result) -> bool:
    """Minimal documented success shape (openapi_contracts); anything else is an unexpected response."""
    if not isinstance(result, dict) or not isinstance(result.get('run_id'), str):
        return False
    if action == 'abandon':
        return result.get('state') == 'failed'
    if action == 'commit':
        return _commit_tasks(result) is not None
    return isinstance(result.get('removed'), bool) and isinstance(result.get('tasks'), list)


def _commit_tasks(result):
    """Bounded, control-free [(branch, head12)] from a commit response, or None when it is not the documented shape."""
    tasks = result.get('tasks')
    if not isinstance(tasks, list) or not tasks or len(tasks) > 200:
        return None
    rows = []
    for task in tasks:
        if (not isinstance(task, dict) or not isinstance(task.get('task_id'), str) or not isinstance(task.get('branch'), str)
                or not isinstance(task.get('head'), str) or not task['branch'] or not task['head']
                or not isinstance(task.get('paths'), list)):
            return None
        rows.append((clean(task['branch'], 200), clean(task['head'], 64)[:12]))
    return rows


class Poller(QThread):
    health = Signal(str)
    connection = Signal(str, int, float)  # state ('connected' | 'reconnecting'), consecutive failures, retry delay
    event = Signal(object)
    approvals = Signal(object, object)
    run_snapshot = Signal(str, object, object)
    run_error = Signal(str, object)
    approvals_error = Signal(object)
    threads = Signal(str, object, object, object)
    threads_error = Signal(str, object, object)
    messages = Signal(str, object, str, object, object, object)
    messages_error = Signal(str, object, str, object, object)
    mock_started = Signal(str, bool)
    mock_cancel_requested = Signal(str, bool)
    mock_resume_prepared = Signal(str, object, object)
    mock_resume_finished = Signal(str, str)
    approval_decided = Signal(str, str)
    agent_started = Signal(str, object, str)  # outcome ('accepted' | 'unavailable' | 'invalid' | 'failed'), result dict | None, key
    agent_cancel_finished = Signal(str, str)  # run id, outcome
    agent_op_finished = Signal(str, str, str)  # action ('abandon' | 'remove' | 'commit'), run id, outcome
    agent_commit_done = Signal(str, object)  # run id, [(branch, short head)]; emitted just before agent_op_finished
    agent_usage = Signal(str, object, object)  # run id, generation, sanitized usage
    agent_usage_error = Signal(str, object, str)  # run id, generation, 'unavailable' | 'failed'
    agent_detail = Signal(str, object, object)  # run id, generation, sanitized detail
    agent_detail_error = Signal(str, object, str)  # run id, generation, 'unavailable' | 'failed'
    profiles_list = Signal(object)  # raw GET /api/v1/profiles answer for the run-start pickers; None: explicit 404/503 (unavailable)
    profiles_failed = Signal(str)  # the profiles could not be loaded (not 404/503): short fixed reason, pickers stay disabled
    runs_list = Signal(object)  # recent runs (GET /api/v1/runs) for the Control Center switcher
    run_feed = Signal(str, object, object)  # run id, generation, new messages across the run's threads
    cc_approvals = Signal(object)  # approval rows for the Control Center gate, independent of the Developer tab's page
    cursor_notice = Signal(str)  # user-visible cursor persistence status; '' clears it
    health_info = Signal(object)  # {'status': str, 'version': str | None}: only fields safe to display

    def __init__(self, state_dir: Path, *, fetch=_get, send=_request, interval: float = 1.0):
        super().__init__()
        self.state_dir = state_dir
        self.fetch = fetch
        self.send = send
        self._command_lock = Lock()
        self._command = None
        self.interval = interval
        self.stop_event = Event()
        self._wake = Event()
        self.cursor = 0
        self._cursor_path = cursor_path(state_dir)
        self._profiles_loaded = False
        self._cursor_resumed = False  # saved cursor loaded and verified (or none/untrusted) for this process
        self._save_failed = False
        self.failures = 0
        self.max_backoff = MAX_BACKOFF
        self._run_lock = Lock()
        self._run_id = ""
        self._run_generation = 0
        self._thread_id = ""
        self._thread_generation = 0
        self._thread_cursor = 0
        self._thread_page_generation = 0
        self._message_cursor = 0
        self._message_page_generation = 0
        self._approval_cursor = ""
        self._approval_page_generation = 0
        self._agent_skip = 0
        self._cycle = 0
        self._feed_cursors = {}  # (run id, thread id) -> last message cursor already delivered
        self._feed_run = None
        self._feed_rr = 0

    def select_approval_page(self, cursor: str):
        with self._run_lock:
            self._approval_cursor = cursor
            self._approval_page_generation += 1
            return self._approval_page_generation

    def _selected_approval_page(self):
        with self._run_lock:
            return self._approval_cursor, self._approval_page_generation

    def select_run(self, run_id: str):
        with self._run_lock:
            self._run_id = run_id
            self._run_generation += 1
            self._thread_id = ""
            self._thread_generation += 1
            self._agent_skip = 0
            return self._run_generation

    def select_thread(self, thread_id: str):
        with self._run_lock:
            self._thread_id = thread_id
            self._thread_generation += 1
            return self._thread_generation

    def select_threads_page(self, cursor: int):
        with self._run_lock:
            self._thread_cursor = cursor
            self._thread_page_generation += 1
            return self._thread_page_generation

    def select_messages_page(self, cursor: int):
        with self._run_lock:
            self._message_cursor = cursor
            self._message_page_generation += 1
            return self._message_page_generation

    def _selection(self):
        with self._run_lock:
            return (self._run_id, self._run_generation, self._thread_id, self._thread_generation,
                    self._thread_cursor, self._thread_page_generation, self._message_cursor, self._message_page_generation)

    def _selected_run(self):
        with self._run_lock:
            return self._run_id, self._run_generation

    def request_mock_start(self, run_id: str, key: str) -> bool:
        with self._command_lock:
            if self._command is not None:
                return False
            self._command = ("start", run_id, key, None, None)
            return True

    def request_mock_cancel(self, run_id: str, key: str) -> bool:
        with self._command_lock:
            if self._command is not None:
                return False
            self._command = ("cancel", run_id, key, None, None)
            return True

    def request_agent_start(self, key: str, correlation: str, body: bytes) -> bool:
        with self._command_lock:
            if self._command is not None:
                return False
            self._command = ("agent-start", "", key, body, correlation)
            return True

    def request_agent_cancel(self, run_id: str, key: str) -> bool:
        with self._command_lock:
            if self._command is not None:
                return False
            self._command = ("agent-cancel", run_id, key, b"", None)
            return True

    def request_agent_op(self, action: str, run_id: str, key: str, version: int | None = None) -> bool:
        """agent-abandon (needs the run's state version) or agent-worktree-remove ."""
        if action not in ('abandon', 'remove'):
            return False
        with self._command_lock:
            if self._command is not None:
                return False
            body = (json.dumps({"expected_run_version": version, "assertion": "no-agent-process-remains"}).encode("utf-8")
                    if action == 'abandon' else b"")
            self._command = ("agent-" + action, run_id, key, body, key)
            return True

    def request_agent_commit(self, run_id: str, key: str, message: str) -> bool:
        """agent-worktree-commit with {"message": ...}; the text is only held in the pending command until it is sent."""
        with self._command_lock:
            if self._command is not None:
                return False
            self._command = ("agent-commit", run_id, key, json.dumps({"message": message}).encode("utf-8"), key)
            return True

    def prepare_mock_resume(self, run_id: str, generation: int) -> bool:
        with self._command_lock:
            if self._command is not None:
                return False
            self._command = ("prepare", run_id, None, None, generation)
            return True

    def request_mock_resume(self, run_id: str, key: str, body: bytes) -> bool:
        with self._command_lock:
            if self._command is not None:
                return False
            self._command = ("resume", run_id, key, body, None)
            return True

    def request_approval_decision(self, approval_id: str, key: str, correlation: str, body: bytes, token: str) -> bool:
        with self._command_lock:
            if self._command is not None:
                return False
            self._command = ("decide", approval_id, key, body, (correlation, token))
            return True

    def _decide(self, approval_id, key, body, extra, decision):
        """Send one decision off the UI thread; the token lives only in this frame and is dropped after."""
        correlation, token = extra
        try:
            result = self.send(self.state_dir, '/api/v1/approvals/' + quote(approval_id, safe='') + '/decision',
                               body=body, idempotency_key=key, correlation_id=correlation, approver_token=token)
            if (not isinstance(result, dict) or result.get('approval_id') != approval_id
                    or result.get('state') != decision or type(result.get('state_version')) is not int):
                outcome = 'invalid'
            else:
                outcome = 'accepted'
        except DaemonHTTPError as exc:
            outcome = approval_decision.outcome_for_status(exc.status, getattr(exc, 'locked_until', None))
        except Exception:
            outcome = 'failed'  # never format the exception: it could carry request details
        finally:
            token = extra = None
            with self._command_lock:
                self._command = None
        self.approval_decided.emit(approval_id, outcome)

    def _agent_command(self, action, run_id, key, body, correlation):
        """Send agent-run start/cancel off the UI thread. Responses are validated; errors are never formatted."""
        outcome, result = 'failed', None
        try:
            if action == 'agent-start':
                result = self.send(self.state_dir, '/api/v1/agent-runs', body=body, idempotency_key=key, correlation_id=correlation)
                if (not isinstance(result, dict) or not all(isinstance(result.get(f), str) and result.get(f)
                                                            for f in ('run_id', 'task_id', 'state'))
                        or not isinstance(result.get('worktree_path'), (str, type(None)))):
                    outcome, result = 'invalid', None
                else:
                    outcome = 'accepted'
                    profile = result.get('profile')
                    if profile is None:  # the daemon did not echo it: show the account the operator chose (what was sent)
                        try:
                            profile = json.loads(body).get('profile')
                        except ValueError:
                            profile = None
                    result = {'run_id': result['run_id'][:256], 'state': result['state'][:64]}
                    if isinstance(profile, str) and re.fullmatch(r'[A-Za-z0-9._-]{1,64}', profile):
                        result['profile'] = profile
            else:
                result = self.send(self.state_dir, '/api/v1/runs/' + quote(run_id, safe='') + '/agent-cancel',
                                   body=b"", idempotency_key=key, correlation_id=key)  # the daemon refuses any cancel body
                outcome = 'accepted'
                result = None
        except DaemonHTTPError as exc:
            if action == 'agent-start':
                outcome = {404: 'unavailable', 503: 'unavailable', 409: 'active', 422: 'rejected'}.get(exc.status, 'failed')
                if outcome == 'rejected':
                    result = {'reason': getattr(exc, 'reason', None)}  # the daemon's own refusal text (already sanitized)
            else:
                outcome = 'unavailable' if exc.status in UNAVAILABLE_STATUSES else 'failed'
                result = None
        except Exception:
            outcome, result = 'failed', None
        finally:
            with self._command_lock:
                self._command = None
        if action == 'agent-start':
            self.agent_started.emit(outcome, result, key)
        else:
            self.agent_cancel_finished.emit(run_id, outcome)

    def _agent_op(self, action, run_id, key, body):
        """Abandon / remove a worktree off the UI thread; the outcome is a coarse word, never response or exception text."""
        outcome = 'failed'
        try:
            suffix = {'abandon': '/agent-abandon', 'commit': '/agent-worktree-commit'}.get(action, '/agent-worktree-remove')
            path = '/api/v1/runs/' + quote(run_id, safe='') + suffix
            result = self.send(self.state_dir, path, body=body, idempotency_key=key, correlation_id=key)
            body = None  # may hold the commit message: drop it
            outcome = 'accepted' if _op_response_ok(action, run_id, result) else 'invalid'
            if action == 'commit' and outcome == 'accepted':
                self.agent_commit_done.emit(run_id, _commit_tasks(result))
        except DaemonHTTPError as exc:
            outcome = {404: 'unavailable', 503: 'unavailable', 409: 'conflict', 422: 'rejected'}.get(exc.status, 'failed')
        except Exception:
            outcome = 'failed'
        finally:
            with self._command_lock:
                self._command = None
        self.agent_op_finished.emit(action, run_id, outcome)

    def _poll_usage(self, run_id, generation):
        try:
            usage = sanitize_usage(self.fetch(self.state_dir, "/api/v1/runs/" + quote(run_id, safe="") + f"/usage?limit={MAX_ROWS}"))
            if usage is None:
                raise ValueError("invalid usage response")
            self.agent_usage.emit(run_id, generation, usage)
        except DaemonHTTPError as exc:
            self.agent_usage_error.emit(run_id, generation, 'unavailable' if exc.status in UNAVAILABLE_STATUSES else 'failed')
        except Exception:
            self.agent_usage_error.emit(run_id, generation, 'failed')

    def _poll_agent_detail(self, run_id, generation):
        if self._agent_skip > 0:
            self._agent_skip -= 1
            return
        try:
            detail = sanitize_detail(self.fetch(self.state_dir, "/api/v1/runs/" + quote(run_id, safe="") + "/agent"))
            if detail is None:
                raise ValueError("invalid agent response")
            self.agent_detail.emit(run_id, generation, detail)
            self._poll_usage(run_id, generation)
        except DaemonHTTPError as exc:
            unavailable = exc.status in UNAVAILABLE_STATUSES
            self._agent_skip = AGENT_RETRY_POLLS if unavailable else 0
            self.agent_detail_error.emit(run_id, generation, 'unavailable' if unavailable else 'failed')
        except Exception:
            self.agent_detail_error.emit(run_id, generation, 'failed')

    def _poll_profiles(self):
        """Fetch the accounts for the run-start pickers. Called right after connecting and retried every loop until it
        succeeds (or the daemon says 404/503), then refreshed every FEED_EVERY cycles."""
        try:
            answer = self.fetch(self.state_dir, "/api/v1/profiles")
            if not isinstance(answer, dict) or not isinstance(answer.get("profiles"), list):
                raise ValueError("invalid profiles response")
            self.profiles_list.emit(answer)
            self._profiles_loaded = True
        except DaemonHTTPError as exc:
            if exc.status in (404, 503):
                self.profiles_list.emit(None)
                self._profiles_loaded = True
            else:
                self._profiles_loaded = False
                self.profiles_failed.emit(f"HTTP {exc.status}")
        except Exception:
            self._profiles_loaded = False
            self.profiles_failed.emit("unexpected answer or no connection")

    def _poll_control_center(self, run_id, generation, threads):
        """Best-effort reads for the Control Center; any failure is silent (the view keeps its last data)."""
        self._poll_cc_approvals()
        if self._cycle % FEED_EVERY:
            return
        try:
            page = self.fetch(self.state_dir, "/api/v1/runs?limit=25")
            if isinstance(page, dict) and isinstance(page.get("runs"), list):
                self.runs_list.emit(page["runs"][:100])
        except Exception:
            pass
        if self._feed_run != run_id:
            self._feed_run, self._feed_cursors, self._feed_rr = run_id, {}, 0
        ids = [t.get("id") for t in (threads or []) if isinstance(t, dict) and isinstance(t.get("id"), str) and t.get("id")]
        if not run_id or not ids or self.stop_event.is_set():
            return
        start = self._feed_rr % len(ids)
        batch = (ids[start:] + ids[:start])[:FEED_THREADS]
        self._feed_rr = start + len(batch)
        collected = []
        for thread_id in batch:
            key = (run_id, thread_id)
            for _ in range(FEED_PAGES):
                if self.stop_event.is_set():
                    return
                cursor = self._feed_cursors.get(key, 0)
                try:
                    page = self.fetch(self.state_dir, "/api/v1/runs/" + quote(run_id, safe="") + "/threads/" + quote(thread_id, safe="")
                                      + f"/messages?after={cursor}&limit={FEED_MESSAGES}")
                except Exception:
                    break
                messages = [m for m in page["messages"][:FEED_MESSAGES] if isinstance(m, dict)] if isinstance(page, dict) and isinstance(page.get("messages"), list) else []
                collected.extend(messages)
                last = messages[-1].get("cursor") if messages else None
                if type(last) is not int or last <= cursor:
                    break
                self._feed_cursors[key] = last
                if len(messages) < FEED_MESSAGES:
                    break
        if collected:
            self.run_feed.emit(run_id, generation, collected)

    def _poll_cc_approvals(self):
        """The gate needs this run's approvals wherever the Developer tab is paged to, so walk the list separately."""
        rows, cursor = [], ""
        try:
            for _ in range(APPROVAL_PAGES):
                if self.stop_event.is_set():
                    return
                page = self.fetch(self.state_dir, "/api/v1/approvals?" + urlencode({"after": cursor, "limit": 100}))
                if not isinstance(page, dict) or not isinstance(page.get("approvals"), list):
                    return
                batch = [a for a in page["approvals"][:100] if isinstance(a, dict) and isinstance(a.get("id"), str)]
                rows.extend(batch)
                if len(page["approvals"]) < 100 or not batch:
                    break
                cursor = batch[-1]["id"]
        except Exception:
            return
        self.cc_approvals.emit(rows)

    def run(self):
        while not self.stop_event.is_set():
            with self._command_lock:
                command = self._command
            if command is not None:
                action, run_id, key, body, generation = command
                if action == 'decide':
                    self._decide(run_id, key, body, generation, json.loads(body)['decision'])
                    command = generation = None
                elif action in ('agent-abandon', 'agent-remove', 'agent-commit'):
                    self._agent_op(action[6:], run_id, key, body)
                    command = generation = body = None
                elif action in ('agent-start', 'agent-cancel'):
                    self._agent_command(action, run_id, key, body, generation)
                    command = generation = None
                elif action == 'prepare':
                    try:
                        base = '/api/v1/runs/' + quote(run_id, safe='')
                        before = self.fetch(self.state_dir, base)
                        details = fetch_all_tasks(self.fetch, self.state_dir, base)
                        after = self.fetch(self.state_dir, base)
                        prepared = collect_evidence(run_id, before, details, after)
                    except Exception:
                        prepared = None
                    with self._command_lock:
                        self._command = None
                    self.mock_resume_prepared.emit(run_id, generation, prepared)
                else:
                    try:
                        result = self.send(self.state_dir, '/api/v1/runs/' + quote(run_id, safe='') + '/mock-' + action,
                                           body=body if action == 'resume' else b'', idempotency_key=key, correlation_id=key)
                        if (not isinstance(result, dict) or result.get('run_id') != run_id
                                or (action == 'start' and result.get('fixture') != 'fixed-v1')
                                or (action == 'cancel' and result.get('state') != 'cancellation_requested')
                                or (action == 'resume' and (result.get('state') != 'running' or result.get('assertion') != ASSERTION))):
                            raise ValueError('Invalid mock command response')
                        success = True
                        outcome = 'accepted'
                    except Exception as exc:
                        success = False
                        outcome = 'rejected' if isinstance(exc, DaemonHTTPError) and exc.status == 409 else 'failed'
                    with self._command_lock:
                        self._command = None
                    if action == 'start':
                        self.mock_started.emit(run_id, success)
                    elif action == 'cancel':
                        self.mock_cancel_requested.emit(run_id, success)
                    else:
                        self.mock_resume_finished.emit(run_id, outcome)
            try:
                health = self.fetch(self.state_dir, "/api/v1/system/health")
                if not isinstance(health, dict) or health.get("status") != "ok":
                    raise ValueError("invalid health response")
                if not self._cursor_resumed:
                    self._resume_cursor()
                page = self.fetch(self.state_dir, "/api/v1/events?" + urlencode({"after": self.cursor, "limit": 100}))
                if not isinstance(page, dict) or not isinstance(page.get("events"), list):
                    raise ValueError("invalid event response")
                last = None
                try:
                    for event in page["events"]:
                        sequence = event.get("sequence") if isinstance(event, dict) else None
                        if type(sequence) is not int or sequence <= self.cursor:
                            raise ValueError("invalid event sequence")
                        self.event.emit(event)
                        self.cursor = sequence
                        last = event
                finally:
                    if last is not None:
                        self._save_cursor(last)
                self.failures = 0
                version = health.get("version")
                self.health_info.emit({"status": "ok", "version": version[:32] if isinstance(version, str) else None})
                self.health.emit("Connected")
                self.connection.emit("connected", 0, 0.0)
                full_page = len(page["events"]) >= EVENT_PAGE
            except Exception:
                # The cursor only advances after an event was emitted, so a retry resumes exactly after the
                # last delivered event: nothing is skipped and nothing is replayed.
                self.failures += 1
                if self._profiles_loaded:
                    self._profiles_loaded = False
                    self.profiles_failed.emit("the daemon connection was lost")
                delay = backoff_delay(self.interval, self.failures, self.max_backoff)
                self.health.emit(f"Disconnected — reconnecting (attempt {self.failures}, retry in {delay:.0f}s)")
                self.connection.emit("reconnecting", self.failures, delay)
                _, approval_generation = self._selected_approval_page()
                self.approvals_error.emit(approval_generation)
                run_id, generation = self._selected_run()
                self.run_error.emit(run_id, generation)
                run_id, generation, thread_id, thread_generation, thread_cursor, thread_page, message_cursor, message_page = self._selection()
                self.threads_error.emit(run_id, generation, thread_page)
                self.messages_error.emit(run_id, generation, thread_id, thread_generation, message_page)
                self._sleep(delay)
                continue
            if self.stop_event.is_set():
                break
            if not self._profiles_loaded or self._cycle % FEED_EVERY == 0:
                self._poll_profiles()
            approval_cursor, approval_generation = self._selected_approval_page()
            try:
                page = self.fetch(self.state_dir, "/api/v1/approvals?" + urlencode({"after": approval_cursor, "limit": 100}))
                if not isinstance(page, dict) or not isinstance(page.get("approvals"), list) or len(page["approvals"]) > 100:
                    raise ValueError("invalid approvals response")
                self.approvals.emit(page["approvals"], approval_generation)
            except Exception:
                self.approvals_error.emit(approval_generation)
            run_id, generation = self._selected_run()
            if run_id and not self.stop_event.is_set():
                try:
                    snapshot = self.fetch(self.state_dir, "/api/v1/runs/" + quote(run_id, safe=""))
                    if (not isinstance(snapshot, dict) or not isinstance(snapshot.get("run"), dict)
                            or snapshot["run"].get("id") != run_id
                            or not isinstance(snapshot.get("tasks"), list)
                            or not isinstance(snapshot.get("dependencies"), list)):
                        raise ValueError("invalid run snapshot")
                    self.run_snapshot.emit(run_id, generation, snapshot)
                except Exception:
                    self.run_error.emit(run_id, generation)
                else:
                    self._poll_agent_detail(run_id, generation)
            run_id, generation, thread_id, thread_generation, thread_cursor, thread_page, message_cursor, message_page = self._selection()
            self._cycle += 1
            if not run_id:
                self._poll_control_center("", generation, [])
            if run_id and not self.stop_event.is_set():
                base = "/api/v1/runs/" + quote(run_id, safe="") + "/threads"
                try:
                    page = self.fetch(self.state_dir, base + f"?after={thread_cursor}&limit=100")
                    if not isinstance(page, dict) or not isinstance(page.get("threads"), list) or len(page["threads"]) > 100:
                        raise ValueError("invalid thread response")
                    self.threads.emit(run_id, generation, page["threads"], thread_page)
                    self._poll_control_center(run_id, generation, page["threads"])
                except Exception:
                    self.threads_error.emit(run_id, generation, thread_page)
                if thread_id and not self.stop_event.is_set():
                    try:
                        page = self.fetch(self.state_dir, base + "/" + quote(thread_id, safe="") + f"/messages?after={message_cursor}&limit=100")
                        if not isinstance(page, dict) or not isinstance(page.get("messages"), list) or len(page["messages"]) > 100:
                            raise ValueError("invalid message response")
                        self.messages.emit(run_id, generation, thread_id, thread_generation, page["messages"], message_page)
                    except Exception:
                        self.messages_error.emit(run_id, generation, thread_id, thread_generation, message_page)
            # Catching up on a long event backlog: fetch the next page right away (still one page per loop,
            # so commands and the other panes keep being serviced) instead of sleeping a full interval.
            if not full_page:
                self._sleep(self.interval)

    def _resume_cursor(self):
        """Load the saved cursor once and verify it against the daemon's log; any doubt means replay from 0."""
        try:
            saved = read_cursor(self._cursor_path)
        except CursorError as exc:
            self.cursor = 0
            self.cursor_notice.emit(f"Saved event cursor ignored ({exc}); replaying events from the start.")
            self._cursor_resumed = True
            return
        if saved is not None:
            page = self.fetch(self.state_dir, "/api/v1/events?" + urlencode({"after": saved.sequence - 1, "limit": 1}))
            if not isinstance(page, dict) or not isinstance(page.get("events"), list):
                raise ValueError("invalid event response")
            events = page["events"]
            first = events[0] if events and isinstance(events[0], dict) else None
            if first is None:
                self.cursor = 0
                self.cursor_notice.emit("Saved event cursor is ahead of the daemon's event log (database recreated or "
                                        "restored?); replaying events from the start.")
            elif first.get("sequence") != saved.sequence or first.get("id") != saved.event_id:
                self.cursor = 0
                self.cursor_notice.emit("Saved event cursor belongs to a different event log; replaying events from the start.")
            else:
                self.cursor = saved.sequence
                self.cursor_notice.emit(f"Resumed event stream after saved cursor {saved.sequence}.")
        self._cursor_resumed = True

    def _save_cursor(self, event):
        """At-least-once: called after delivery. A failure never stops polling; it is shown once."""
        sequence, event_id = event.get("sequence"), event.get("id")
        try:
            if not valid_event_id(event_id):
                raise ValueError("event has no usable id")
            write_cursor(self._cursor_path, Cursor(sequence, event_id))
        except (OSError, ValueError) as exc:
            if not self._save_failed:
                self._save_failed = True
                self.cursor_notice.emit(f"Event cursor is not being saved ({type(exc).__name__}); a restart will replay events.")
            return
        if self._save_failed:
            self._save_failed = False
            self.cursor_notice.emit("")

    def _sleep(self, seconds: float):
        # Interruptible by stop() and request_refresh(); a refresh also shortens a reconnect backoff.
        self._wake.wait(seconds)
        self._wake.clear()

    def request_refresh(self):
        """Poll again now instead of waiting out the interval (safe from any thread)."""
        self._wake.set()

    def stop(self):
        self.stop_event.set()
        self._wake.set()
