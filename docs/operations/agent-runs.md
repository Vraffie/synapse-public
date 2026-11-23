# Real agent runs (local API and agentctl)

Starts a REAL Claude Code CLI task in its own git worktree through the operator API. It builds on
[agent-runtime](agent-runtime.md) (execution), [worktrees](worktrees.md) (isolation) and
[local-api](local-api.md) (transport). Same trust model as the rest of the operator surface: the
bearer token holds the operator identity; a same-user process that can read it can start agents.

## Enabling agent runs (same-user acknowledgement)

Agents run as the daemon's user, so they can read the approver and operator tokens and write the database;
approvals are therefore **not** a security boundary against them ([security review](../security/review-2026-11-20.md), F1/F2).
`agentd` refuses to start with an enabled `--claude-cli-config` (exit status 2, nothing touched) unless you pass
`--allow-same-user-agents` (or set `SYNAPSE_ALLOW_SAME_USER_AGENTS=1`). The acknowledgement is logged at WARNING on
every start (the message states whether a sandbox is configured; a sandbox reduces but does not remove the exposure and never replaces the flag) and recorded as an `agent_runtime.same_user_acknowledged` audit entry (principal `daemon:startup`).
Example: `agentd --state-dir D --claude-cli-config FILE --allow-same-user-agents`. The demo script passes it for you.

## Operator configuration

`agentd --claude-cli-config FILE` (strict JSON, same file rules as in agent-runtime.md) gains two
optional keys that must be given together:

```json
{"executable": "/usr/bin/claude", "allowed_tools": ["Read", "Glob", "Grep", "Edit"], "enabled": true,
 "repos": ["/home/me/src/project"], "worktree_root": "/home/me/.local/share/synapse/worktrees"}
```

`repos`: 1 to 32 absolute, normalized, symlink-free repositories with a real `.git` directory.
`worktree_root`: absolute, outside the state directory, not overlapping any repo (created `0700`).
Optional approval policy keys (F6): `"require_approval": true` makes every run need an
`approval_step`; `"require_approval_for_modes": ["acceptEdits"]` and
`"require_approval_for_tools": ["Edit"]` require it only for those permission modes / tools. A request
that matches but has no `approval_step` is **refused with 422** ("operator policy requires an
approval_step for this run"; no run or worktree is created). The server never adds an approval step on
its own, because the operator must name the operation and target the human is shown. The default policy
requires nothing.

Invalid values abort startup before any state is touched. Without `repos`/`worktree_root` (or with
`enabled: false`, or no config) `POST /api/v1/agent-runs` returns **503** "Agent runs are not
configured"; read and cancel endpoints still work for existing runs.

## `POST /api/v1/agent-runs`

Operator Bearer, one `Idempotency-Key`, one `X-Correlation-ID`, `application/json` (strict, no
unknown fields, no duplicate keys, 1 MiB cap).

| field | rule |
| --- | --- |
| `repo` | exact string from the `repos` allowlist |
| `base_ref` | `[A-Za-z0-9][A-Za-z0-9._/@+~^-]*`, at most 256, no `..`, `@{`, `//`, trailing `/` `.` `.lock`; must resolve to a commit |
| `prompt` | 1 to 32768 characters and at most 32 KiB UTF-8, no NUL |
| `allowed_tools` | 1 to 50 names, subset of the operator `allowed_tools` |
| `permission_mode` | default `acceptEdits`; `plan`, `dontAsk`, `manual` also allowed; `bypassPermissions` and `auto` are never accepted |
| `timeout_seconds` | default 600, `0 < t <= min(3600, operator max_timeout_seconds)` |
| `approval_step` | optional `{operation, target[, ttl_seconds]}` (each 1-128 chars of `[A-Za-z0-9._:/@+-]`, ttl 1-86400, default 3600). `policy_version` is fixed `1`; the approval is bound to the hash of the whole task spec (prompt, repo, base_ref, worktree path and HEAD, tools, permission mode, timeout) |

### Multi-task runs

Instead of `prompt`/`allowed_tools`/`approval_step`, send `tasks`: 1 to 6 entries of `{id, prompt, allowed_tools,
[permission_mode], [timeout_seconds], [depends_on], [approval_step]}` (`id`: 1-64 characters of `[A-Za-z0-9._-]`;
`permission_mode` and `timeout_seconds` default to the request-level values; `depends_on` lists other task ids).
The shapes are mutually exclusive, ids are unique, and the graph must be acyclic; every rule of the single-task body
applies to each task. Each task gets its **own worktree** branched from the same `base_ref`; a dependent task starts
only after its prerequisites complete but does **not** inherit their changes (combining results is the later
integration step, not this one). Independent tasks run in parallel up to the daemon's concurrency cap (default 2).
What happens when a prerequisite does not complete:

- **Failed or cancelled** (unambiguous: a dependent needs every prerequisite `completed`). The task's transitive
  dependents are cancelled, nothing is re-spawned, tasks that did complete stay `completed`, and the run ends
  `failed`.
- **No established outcome** (`unknown`, or any attempt still unresolved). The dependents stay waiting and the run
  goes to `recovering` for an operator, exactly like a single task; nothing is auto-resolved (end it with
  `agent-abandon`). If one run has both a failed branch and an `unknown` attempt, `recovering` wins.

Replaying an idempotency key with the same tasks listed in a different order is a different request and gets
409; send the tasks in the same order when retrying. Response **201**/**200**: `{run_id, state, repo, base_ref, tasks: [{id, task_id, worktree_path}]}`
(the single-task response keeps its shape). `agentctl agent-start --tasks-file FILE` (`-` for stdin) sends the
list from a JSON file; prompts never travel in argv.

Response **201** (new) or **200** (idempotent replay, header `Idempotency-Replayed: true`):
`{run_id, task_id, state: "running", worktree_path, repo, base_ref}`. `worktree_path` is for display;
clients never supply it. `state` is the state at start; poll the run for progress. Errors: 400
(headers), 401, 409 (another run is active, or idempotency key reused for a different request), 413,
415, 422 (any validation failure; messages are fixed text and never echo input), 503 (not configured).

Server-side sequence: validate the body, look up the idempotency receipt, run the side-effect-free git
pre-flight (`WorktreeManager.check_base`: allowlist, ref resolves, no dangerous repo config), check no
other run is active; only then publish the fixed single-node workflow `synapse.agent.single`, queue a
run, create the worktree and call `AgentRuntime.start`. The idempotency receipt is written last.
Idempotency compares everything except the prompt text, plus the prompt SHA-256.

Failure after the run exists (worktree creation, runtime refusal, crash): the new worktree and its
branch are force-removed (idempotent; skipped if the run already started) and the still-queued run is
cancelled with its task, so no queued or running run remains. The cancelled run is a visible audit
trace (runs cannot be deleted); a retry with the same key creates a fresh run.

## `GET /api/v1/runs/{run_id}/agent`

Read-only; 404 for an unknown or non-agent run. Top level: `run_id`, `run_state`,
`cancellation_requested`, plus a flattened view of the (first) task: `outcome`, `summary_text`
(at most 8 KiB, assistant text only), `tool_names` (names only, never inputs) and `worktree`
(`null` or `{status: clean|dirty, head, diff_stat, diff, truncated}`, each bounded to 16 KiB). `tasks`
carries the same per task with `spec` (permission mode, tools, timeout, prompt SHA-256 and size,
approval step; never the prompt), `attempt` and a detailed `worktree`. The diff is repository content:
it can contain secrets that were in the repo, so treat the response as sensitive.

## `POST /api/v1/runs/{run_id}/agent-cancel`

No body; `Idempotency-Key` and `X-Correlation-ID` required. 202 `{run_id, state:
"cancellation_requested"}` records intent and terminates the process group; the run becomes
`cancelled` only once no attempt is unresolved (else `recovering`, see agent-runtime.md). 409 for a
run that is not a running agent run or already cancelling. The worktree is kept for inspection.

## agentctl

```
agentctl --state-dir D agent-start --repo /abs/repo --base-ref main --prompt-file prompt.txt \
    --tool Read --tool Grep [--permission-mode plan] [--timeout-seconds 300] \
    [--approval-operation agent.run --approval-target repo/main [--approval-ttl-seconds 600]] \
    --idempotency-key K --correlation-id C
agentctl --state-dir D agent-show RUN        # JSON
agentctl --state-dir D agent-cancel RUN [--idempotency-key K --correlation-id C]
```

The prompt is read from `--prompt-file FILE` (regular file, no symlink, at most 32 KiB) or `-` for stdin,
never from argv (argparse would otherwise accept `--prompt TEXT` as an abbreviation of `--prompt-file`;
that value is treated as a path and never echoed). Approving a gated run is the existing human flow:
`agentctl approval-decide ...` with the approver token (see approval docs); the operator token cannot.

## Approval subject, worktree recheck

The start records the new worktree's HEAD (the tree must be clean). Before spawning, after approval, the
daemon rechecks HEAD and cleanliness; any change fails the task with a clear detail and spawns nothing
(agent-runtime.md). `GET /api/v1/runs/{id}/agent` shows `spec.repo`, `base_ref`, `worktree_head` and
`spec.spec_sha256` (the approval's bound hash). What the approver sees is on `GET /api/v1/approvals`
(approver-identity.md).

Fail-closed details: a spec with an `approval_step` must carry `repo`, `base_ref` and `worktree_head`
(rejected otherwise, so a gated task can never skip the recheck); the recheck inspects exactly the worktree
path the task spawns in and refuses on any mismatch; at most 50 `allowed_tools` are accepted, and the
approval subject carries `allowed_tools_truncated` should a list ever exceed that bound. The approver's
`prompt_preview` shows control characters and bidi override/isolate characters as visible `\uXXXX` escapes,
and `prompt_preview_truncated` stays true when the preview was cut (before or after escaping).

### Upgrading: pre-binding agent runs are not resumable

Agent runs recorded before the full-spec approval binding (their `agent.tasks_configured` event has no
`repo`, `base_ref` or `worktree_head`, and a prompt-digest approval candidate) are not trusted by the new
code: `GET /api/v1/runs/{id}/agent` reports them as not found, they cannot be resumed or approved, and no
approval subject is shown. The hash verification is deliberately not relaxed for them. Finish or cancel such
runs before upgrading, then start new ones; their events and artifacts stay in the database untouched.

## Security notes

- The prompt exists only in the request, the private artifact blob (and the spawned process's argv, as
  in claude-cli-provider.md); not in events, audit rows, idempotency rows, logs or errors.
- The agent runs with the daemon user's rights inside the worktree; a worktree is not a trust boundary
  (agent-runtime.md, worktrees.md). Allowlist only repositories and tools you trust that far.
- Git work (`check_base`, `create`, `status`, `diff`, `remove`) runs in worker threads via `asyncio.to_thread`, so
  a large repository slows that request but does not stall the daemon event loop.

## Abandoning a recovering run

A run left `recovering` (an `unknown` attempt after a daemon crash or an unestablished process outcome) stays
there: nothing resumes it and nothing ends it. It does **not** by itself stop a new run from starting: the
one-run-at-a-time check only looks at an active in-process run and at `running` runs, and a `recovering` run is
neither. (It does leave a possibly still-running child process and an edited worktree behind, which is why it
needs an operator.) `POST /api/v1/runs/{run_id}/agent-abandon` or `agentctl agent-abandon RUN
--expected-run-version N --assert-no-agent-process-remains` ends it.

Body: `{"expected_run_version": N, "assertion": "no-agent-process-remains"}` plus the usual `Idempotency-Key` and
`X-Correlation-ID`. **The assertion is yours:** the child runs in its own session and can outlive a daemon
crash, and the daemon does not persist process identity, so it cannot verify this. Check (`pgrep`, `ps`) that
no agent process for the run is still running or editing its worktree before asserting.

Effects, in one transaction: the run becomes `failed`, its unfinished tasks `cancelled`, and an
`agent.run_abandoned` event is written. A task parked on an approval gate gets its still-requested approval
**revoked** and its gate consumed (the task fails), so no live, decidable approval outlives the run; the revoked
approval ids are listed in the event. Attempts stay `unknown` (the outcome really was never established),
the worktree is kept for inspection (remove it later with `agent-worktree-remove` if wanted) and nothing is
re-spawned. Only a `recovering` run at exactly `expected_run_version` qualifies (409 otherwise; 422 for a wrong
assertion). Replays with the same key return the original response. This is not a resume: the work is not
continued.

## Removing a finished run's worktree

### Getting the edits out: `agent-worktree-commit`

A sandboxed agent cannot write to the repository's git metadata, so its edits stay as uncommitted changes in
the run's worktree. `agentctl agent-worktree-commit RUN -m "message"` (operator Bearer; body `{"message"}`, 1-2000
characters) has the daemon commit them host-side onto each task's own branch `synapse/<run>/<task>`:

- Only for a FINISHED run; tasks whose tree is clean or already removed are skipped, and if nothing at all is
  left to commit the call is refused with 409.
- It never touches your checkout or any other branch and never pushes. To use the work, review the diff
  (`agent-show`) and merge or cherry-pick the branch yourself.
- Same hardened git as every host-side operation (hooks, filters, attributes and config guard as for `status`).
  Refused with 409 for a nested repository (gitlink) or more than 1000 changed paths; a refusal leaves the index
  as the agent left it. The commit author is `synapse <synapse@localhost>`; signing is off.
- Audited as an `agent.worktree_committed` event carrying the task ids and commit ids, never the message.


`POST /api/v1/runs/{run_id}/agent-worktree-remove[?force=true]` (operator Bearer, `Idempotency-Key` and
`X-Correlation-ID` headers, no body) or `agentctl agent-worktree-remove RUN [--force]`. Removal is always
operator-requested; nothing deletes worktrees automatically, because a worktree may hold work nobody has
reviewed yet.

- Only a `succeeded`, `failed` or `cancelled` agent run qualifies; an active, queued or `recovering` run is
  refused with 409 (`force` does not override this).
- Without `force`, every task's tree is checked **before any is deleted**; if any has uncommitted changes
  (including untracked files) the whole request is refused with 409 and nothing is removed. With `force` the
  trees and their branches are discarded, including uncommitted work and unmerged commits.
- Response `200 {"run_id", "removed", "tasks": [{"task_id", "removed"}]}`; `removed` is true if any tree was
  removed, `false` means everything was already gone (idempotent). Every removal writes an
  `agent.worktree_removed` event with a `forced` flag and the `task_ids` actually removed, including when a
  later task could not be removed (that request answers 409 but the earlier removals stay audited).
  Unknown or non-agent runs give 404.

## Verification and caveats

Tests use a fake executable. The opt-in `SYNAPSE_REAL_CLAUDE=1 python scripts/demo_agent.py` runs a
read-only prompt (tools Read/Glob/Grep) and an approval-gated variant against a throwaway repo with the
real `claude`; it consumes a small amount of Claude CLI subscription usage. Default `python scripts/demo_agent.py` uses the fake.
Only one run at a time (shared with mock runs). No resume of `recovering` agent runs exists yet. Finished worktrees are removed on request (above), never
automatically.
