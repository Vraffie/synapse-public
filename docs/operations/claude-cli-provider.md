# Claude Code CLI provider (prototype, disabled by default)

`synapse.providers.claude_cli.ClaudeCliProvider` is a `ProviderAdapter` that runs
`claude -p --output-format stream-json` as a supervised subprocess in a
caller-supplied git worktree. It has no API or CLI surface; the daemon's
`AgentRuntime` (see `agent-runtime.md`) is its only constructor, and only with an
operator `--claude-cli-config` that sets `enabled: true`.

## Trust model

Same OS user, one trust domain. By default the child runs as the daemon's user and is
NOT sandboxed (no namespace/Landlock/uid separation); with the opt-in `sandbox` option
(next section) it runs inside a bubblewrap sandbox, which is still the same uid.
`allowed_tools`, the permission
mode and the Synapse-owned settings below are the guardrails. A `Bash` tool
allowance can still read anything the user can; the deny rules only cover the
file tools (`Read`/`Edit`).

## Isolation of the child (security review F3/F7/F12)

- Repo and user config do not load. The argv has `--setting-sources user` plus a
  Synapse-written `--settings <scratch>/settings.json`, and `HOME` and
  `CLAUDE_CONFIG_DIR` point at a per-run scratch directory, so the repo's
  `.claude/settings.json`/`settings.local.json` (hooks, `env`, `apiKeyHelper`,
  `permissions.allow`) and the operator's `~/.claude` settings, plugins and hooks
  are never read. The Synapse settings set `disableAllHooks` and deny `Read`/`Edit`
  on the state dir, the scratch dir, the real config dir and `~/.ssh`, `.gnupg`,
  `.aws`, `.config/gcloud`, `.kube`, `.docker`, `.claude`, `.config/gh`.
- Scratch dir: `<tmp>/synapse-agent-<uid>/<uuid>/{home,config,tmp}` (`<tmp>` is
  `tempfile.gettempdir()`; it is deliberately OUTSIDE the state dir so a sandbox can hide
  the state dir without hiding the child's HOME; the base dir must be a real directory
  owned by the daemon uid or the run is refused), mode 0700, created
  per run and removed when the run ends (success, failure, cancel, timeout, spawn
  failure). `--setting-sources`/`--settings` stay forbidden to the operator
  (`extra_args`); only the adapter's own fixed use is present.
- Authentication into the scratch dir: with a profile `api_key` nothing is copied
  (`ANTHROPIC_API_KEY` in the child env). Otherwise the OAuth `.credentials.json` is
  vetted and copied (0600, `O_EXCL`) from `credentials_file`, else
  `$CLAUDE_CONFIG_DIR/.credentials.json`, else `$HOME/.claude/.credentials.json`,
  and deleted with the scratch dir. The source is vetted exactly like the Codex adapter's:
  opened with `O_NOFOLLOW` (a symlink is refused), a regular file, owned by the daemon's
  user, mode 0600 or stricter, at most 1 MiB, and not located inside the worktree or the
  state directory. A failure refuses the run at `start()` with a `ValueError`; there is no
  silent fallback. (Behaviour change: group/world-readable or symlinked credential files
  used to be copied.) `CLAUDE_CODE_OAUTH_TOKEN` (from
  `claude setup-token`) is passed through if set in the daemon env and needs no
  copy; it is the preferred headless credential, an API key the next best. Caveat:
  a copied OAuth credential that the CLI refreshes inside the scratch dir is
  discarded, so the original keeps its old refresh token (not written back, since
  the agent could tamper with the copy); if the provider rotates refresh tokens the
  operator login may need `claude auth login` again. Prefer a token or API key.
- Refusal: `start()` (and again at spawn) raises if the worktree contains a hard entry of the
  shared list in `providers/repo_config_guard.py` (see "Repo-config refusal list" in
  `codex-cli-provider.md`): `.claude`, `CLAUDE.md`, `CLAUDE.local.md` (any depth, case-insensitive,
  symlinks count, `.git` is skipped) or a root `.mcp.json`, unless the profile sets
  `allow_repo_claude_config=True`. The opt-in only waives the refusal (CLAUDE.md
  stays prompt-injection surface); settings and hooks still do not load. Other agents' files
  (`AGENTS.md`, `.agents/`, `.cursor*`, ...) only log a warning for Claude. Ancestors of the
  worktree are not scanned.
- Prompt on stdin: `-p` reads the prompt from stdin (verified), so it is written to a
  0600 file in the scratch dir and used as the child's stdin, never argv or
  `/proc/<pid>/cmdline`. The API key still sits in the child's environment.
- rlimits (set in the child before exec, never raised above the inherited hard
  limit): `RLIMIT_CPU` = timeout + grace + 60 s, `NOFILE` 4096, `FSIZE` 1 GiB,
  `CORE` 0, `NPROC` = the uid's current task count + 1024 (it counts all of the
  uid's threads). `RLIMIT_AS` is opt-in via `memory_limit_bytes` (>= 1 GiB) because
  the JS runtime reserves large address space. Limits are verified with the real
  CLI only for the default set.
- `extra_args` is an allowlist (F12), `--flag=value` spelling only, no duplicates:
  `--model`, `--fallback-model` (up to 4 names), `--effort` (low..max),
  `--max-budget-usd`, `--exclude-dynamic-system-prompt-sections`. Everything else is
  refused, including `--permission-prompt-tool`, `--tools`, `--disallowedTools`,
  system-prompt flags, `--ide`, combined short flags and settings/MCP/session flags.
- Still open without `sandbox`: process containment is a process group (a `setsid`
  descendant escapes cancel) and the real UID can read the state dir through `Bash`.
  The opt-in sandbox below addresses both for an agent that stays inside it.

## Opt-in bubblewrap sandbox (`sandbox`, default off; ADR 019 option C2)

Enable with `"sandbox": true` in the `--claude-cli-config` file (or
`ClaudeCliProfile(sandbox=True)`). Launcher: `synapse/providers/sandbox_launcher.py`.

What the child sees (`bwrap --die-with-parent --new-session --unshare-pid --unshare-ipc
--unshare-uts --unshare-cgroup-try --cap-drop ALL`; `--new-session` blocks terminal
injection through the controlling tty):

- Git secrets are masked: `.git/config`, `.git/config.worktree`, `.git/credentials` and
  `.git/worktrees/*/config.worktree` (for both the worktree's own `.git` directory and the
  mounted common git dir) are overlaid with `/dev/null`, so remote URLs that embed tokens
  read as empty. `git log`, `git diff` and `git status` keep working (git treats a missing
  config as defaults). The mask is a `--dev-bind /dev/null` (a `--ro-bind` or
  `--remount-ro` mount is `nodev` and makes the device unopenable), so writes are
  discarded and the file cannot be unlinked or replaced. Only existing, non-symlink
  files are masked. Residual: the rest of `.git` (objects, hooks, packed refs, a
  `config` include from elsewhere) is still readable, and the agent runs with the
  network allowed, so a token configured outside these files (for example in a
  credential helper reachable from the sandbox) is not covered.

- Mounted read-only: `/usr`, `/etc` (and the `/bin /sbin /lib /lib64` links), the
  resolved `claude` executable file, and, for a linked worktree, the owning
  repository's common `.git` directory. That directory is derived from the operator's
  `repos` allowlist (`ClaudeCliProfile.git_repos`), never from the worktree's `.git` file,
  which sits inside the writable mount and is agent-controlled: the file's `gitdir:` must
  resolve to `<allowlisted repo>/.git/worktrees/<name>`, otherwise the run is refused
  (`SandboxUnavailable`, fail closed). A worktree with a real `.git` directory needs no
  extra mount.
  So repository writes inside the sandbox (commit, index refresh) fail; the daemon
  performs those operations host-side.
- Mounted read-write: the run's worktree and the per-run scratch dir, both at their host
  paths (so the worktree's absolute `.git` pointer and the settings paths stay valid; the
  ADR sketch used `/work`). Nothing else from the host filesystem is mounted: the state
  dir (SQLite, `approver.token`, `operator.token`, `agentd.sock`) and the real `HOME`
  do not exist inside. `/tmp` is a fresh tmpfs, `/proc` is the new PID namespace's.
- `HOME`, `CLAUDE_CONFIG_DIR` and `TMPDIR` are the scratch paths; the env allowlist is
  unchanged and still flows through the process environment, never the bwrap argv
  (argv is world-readable in `/proc`).
- New PID namespace: cancel and timeout kill the group, the namespace init dies, and the
  kernel kills every process in the namespace, including `setsid` grandchildren.
  `--die-with-parent` also tears the sandbox down if the daemon dies.
- The network namespace is shared (owner decision 2026-11-20: agents may use the
  network).

Layout under the real `$HOME` (typical `~/Projects/...`) works. Only the explicit
allowlist above is bound, at exact paths, so the rest of `$HOME` (sibling projects, the
state dir if it lives there, `~/.claude`, `~/.ssh`, ...) stays invisible even though the
worktree, repo `.git` and `claude` executable are below it. The run is refused
(`SandboxUnavailable`) when a bound path (worktree, scratch, `.git` dir, executable) is a
home directory or an ancestor of it, is or contains the state dir, or is, lies under or
contains a protected home path: `.ssh`, `.gnupg`, `.aws`, `.kube`, `.docker`, `.claude`,
`.codex`, `.config` (whole dir), `.netrc`, `.local/share`, `.password-store`, `.npmrc`,
`.pypirc`, `.git-credentials`, `.mozilla`, `.bash_history`. Each entry is checked both
lexically and after `realpath` (dotfiles-repo symlinks). Both `$HOME` and the passwd home
(`pwd.getpwuid(uid).pw_dir`) are checked; if neither can be determined the run is refused.
The executable must be an existing regular file (after `realpath`) and, unlike earlier
revisions, must NOT live under a protected path: a `claude` installed under
`~/.local/share` (the usual native-install target) or `~/.claude` is refused; point
`executable` at a copy or symlink target outside those directories.

Broad worktrees: the worktree is mounted read-write with everything below it. A worktree
that is itself a broad directory such as `~/Projects` (with its own `.git`) mounts every
sibling repository writable and readable. There is deliberately no automatic check for
nested repositories (submodules and vendored repos are legitimate); keep worktrees
dedicated, per-task directories under `worktree_root`.

Fail closed: when `sandbox` is on and `bwrap` (looked up only in `/usr/bin`,
`/usr/local/bin`, `/bin`, never via `PATH`) or unprivileged user namespaces are missing,
`start()` raises `SandboxUnavailable` and no child is started, `probe()` reports
`unavailable`, and daemon startup rejects the config when bwrap is not installed. There
is no unsandboxed fallback. If bwrap fails while spawning, the run fails (`spawn_failed`
or `exit_nonzero`), it is not retried unsandboxed.

Honest limits. This is a same-uid mitigation, not a boundary equal to a separate uid:

- Any sandbox escape (kernel or user-namespace bug, a mounting mistake) lands as the
  daemon's uid with full access to the tokens and the database. Other processes of the
  same uid are not contained at all.
- The agent needs the model credential, and the network is open, so a network-capable
  agent can exfiltrate anything it can read: the worktree, the scratch dir (including
  the copied OAuth credential or the env API key), `/etc` and `/usr`.
- It does not stop approvals or SQLite writes by any same-uid process outside the
  sandbox, and it does not provide cgroup resource limits (rlimits still apply).
- Real `claude` compatibility is only partly verified: the executable is bound as one
  file, so an install that needs other files under the real `HOME` (for example a
  `node` script shim) will not run sandboxed. The tests use a fake agent.
- A packaged daemon unit with `RestrictNamespaces=yes` blocks bubblewrap's namespace
  creation; the sandbox then fails closed rather than falling back.

Tests: `tests/test_claude_cli_sandbox.py` (argv construction, config, fail-closed
refusal run everywhere; the namespace tests use a real bwrap with a fake agent script
and skip with a reason if bwrap or unprivileged user namespaces are unavailable).

## Safety properties

- Disabled unless `ClaudeCliProfile(enabled=True)`; `start` raises otherwise and
  `probe` reports `unavailable`. There is no environment-variable switch.
- Profile is validated and immutable: absolute executable, non-empty tool
  allowlist, permission mode from a fixed set (`bypassPermissions` also needs
  `allow_bypass_permissions=True`), `extra_args` from the allowlist above.
- `acceptEdits` is refused for any profile whose allowlist contains a `Bash` tool (bare or patterned):
  whether `acceptEdits` also auto-approves Bash filesystem commands (mkdir/touch/rm/mv/cp/sed) is
  unverified, so the conservative rule applies. Use `dontAsk` with an exact pattern, or set
  `allow_bash_with_accept_edits=True` (default off). When that opt-in is in effect, each run emits an
  `audit` provider event (`allow_bash_with_accept_edits: ...`) right after `started`.
- Argv carries only fixed flags (no prompt). The API key, if configured, is passed
  only via the child environment (`ANTHROPIC_API_KEY`), never argv, never logged,
  and hidden from `repr`.
- Child environment is scrubbed to `PATH`, `LANG`, optional
  `CLAUDE_CODE_OAUTH_TOKEN`, the scratch `HOME`/`CLAUDE_CONFIG_DIR`/`TMPDIR` (plus the
  API key). stdin is the prompt file; stderr is discarded.
- Worktree must be an absolute, normalized, symlink-free directory owned by the
  current user with a real `.git`, and must not overlap the state directory.
- Spawn, cancel and timeout use a new session/process group with the existing
  `mock_process_identity` helpers (TERM, bounded grace, KILL). Leftover
  descendants are killed when the leader exits. Process outcome that cannot be
  established is reported as `unknown`, never silently retried.
- Output is bounded per line (`max_line_bytes`) and in total
  (`max_output_bytes`); violations kill the group and fail with `output_limit`.
  Malformed stream-json fails closed (`protocol_error`). Tool-use events expose
  the tool name only, never inputs.

## Events

`started`, `output` (assistant text), `tool_use` (name), then one terminal event:
`succeeded`, `failed` (detail: `spawn_failed`, `exit_nonzero`, `result_error`,
`output_limit`, `protocol_error`), `timed_out`, `cancelled` or `unknown`.

## Tests

`tests/test_claude_cli_provider.py`, `tests/test_claude_cli_hardening.py` and
`tests/test_claude_cli_sandbox.py` use a fake executable script; the real `claude` binary and the network are never used.

## Verified against claude 2.1.329 on 2026-11-20

- Permission modes: the CLI accepts `acceptEdits`, `auto`, `bypassPermissions`,
  `manual`, `dontAsk`, `plan`. The adapter allows `acceptEdits`, `plan`,
  `dontAsk`, `manual` and `bypassPermissions` (the last with its extra opt-in).
  `default` is not a CLI choice and was removed. `auto` is deliberately not
  allowed: it hands approval decisions to the CLI's own automatic judgement, so
  the allowlist and mode would stop being a fixed guardrail owned by Synapse.
- The adapter's full argv (including `--permission-prompts none`) was accepted
  by the real CLI.
- stream-json output contains system hook/init events, assistant messages with
  `text` and `tool_use` blocks, user `tool_result` events, a `rate_limit_event`,
  and a final `result` event (`subtype: success`, `is_error: false`,
  `permission_denials` list, `usage`, `total_cost_usd`), all matching the parser
  (unknown event types are ignored).
- Opt-in real-binary test: `tests/test_claude_cli_real.py`, skipped unless
  `SYNAPSE_REAL_CLAUDE=1` and `claude` is on `PATH`; never run in default CI.
  It uses a small amount of your Claude CLI (subscription) usage.

## F3 experiment (claude 2.1.329, 2026-11-20, adapter argv before the fix)

Throwaway repo with a committed `.claude/settings.json`, run in `-p` mode with
`--permission-mode plan --allowedTools Read` and the old env (`PATH`, `HOME`, `LANG`):

- `SessionStart` hook: RAN (markers created both outside and inside the repo).
- `PreToolUse` hook: RAN (markers created outside and inside the repo).
- Project `env`: HONOURED (`echo $F3_ENV_MARK` printed the repo-defined value).
- Project `apiKeyHelper`: EXECUTED (marker created) and its key took auth
  precedence, causing 401 retries; a malicious helper is arbitrary code execution.
- Project `permissions.allow`: IGNORED in that run ("this workspace has not been
  trusted"), but the trust state is read from the real `~/.claude.json`, so a
  worktree under an already-trusted directory may honour it. Not tested.
- Real `~/.claude` hooks also ran, and plan files were written under the real
  `~/.claude/plans` (F7).

After the fix (`--setting-sources user`, `--settings`, scratch `HOME`/
`CLAUDE_CONFIG_DIR`, credentials copied in) none of the above fired, the run
authenticated and succeeded, a stdin prompt worked, and a `//abs/dir/**` deny rule
made `Read` refuse a file in that directory while an allowed file was readable.
Covered by the opt-in tests in `tests/test_claude_cli_real.py`.

## Version probe

`probe()` runs `<executable> --version` (scrubbed environment, no stdin, 5 s timeout) and reports
`healthy` only for a version in `VERIFIED_CLI_VERSIONS` (currently `2.1.329`), `unverified_version` for any
other parseable version, and `unavailable` when the profile is disabled or the binary is missing, fails or
prints something unparseable. The result is advisory: it does not block execution, and unknown stream-json
shapes still fail closed in the parser. Add a version to the tuple only after re-running the opt-in real tests.

## Real-binary results, claude 2.1.329 (2026-11-20, Linux)

Run with `SYNAPSE_REAL_CLAUDE=1`; a small amount of Claude CLI subscription usage.

- `tests/test_claude_cli_real.py`: 5 passed (read-only run succeeds; cancel mid-run and a short timeout leave no
  child processes; repo hooks and settings do not run and the run still succeeds; the default refuses a worktree
  containing `.claude`).
- `scripts/demo_agent.py` in real mode: all 8 checks true. A plain read-only run (`Read` tool) succeeds with a
  clean worktree; an approval-gated run parks before any process exists, the operator token cannot approve, and
  the run succeeds after a human approval; no token appears in CLI output, the daemon log or the database, and
  the prompt text is not in any database table.
- Not yet exercised against the real binary at that point: an edit run, a failing task, other permission modes
  and tool allowlists (the edit run and the `Read`-only allowlist are covered in the next section), and any other
  CLI version.

## Tool restriction (found and fixed 2026-11-20)

`--allowedTools` only *pre-approves* tools; it does not remove the others, and permission modes such as
`acceptEdits` auto-approve file edits regardless. The real-binary test `test_real_tool_outside_allowlist_cannot_write`
showed a `Read`-only allowlist still creating a file. The adapter now also passes `--tools <base names>` (the
allowlist with patterns such as `Bash(git status)` reduced to `Bash`, de-duplicated), which restricts the built-in
tool set; `--allowedTools` still carries the exact patterns. `--tools` is not an allowed `extra_args` flag, so a profile cannot widen it. Real
runs now cover an edit run (`Read,Write,Edit`, file created in the worktree only) and the Read-only case (no write).

`default` and the empty string are reserved `--tools` values (the whole default tool set / no tools) and are rejected as
tool names, case-insensitively, by `ClaudeCliProfile` (and therefore by the operator config loader and, through its
subset check, per-task specs). Read-only profiles (none of `Write`, `Edit`, `Bash`, `WebFetch`, `NotebookEdit`
allowed) also get `--disallowedTools Write,Edit,MultiEdit,Bash,WebFetch,NotebookEdit` as a second layer. `--tools Bash` cannot
be narrowed to a pattern, so pattern-scoped Bash profiles should not be combined with `acceptEdits`.

## Permission modes, failure and Bash patterns (claude 2.1.329, 2026-11-20, real binary)

Opt-in tests in `tests/test_claude_cli_real.py` (`SYNAPSE_REAL_CLAUDE=1`), each asserting on files and outcome kinds,
not model prose; every run also checks that no child process is left in the worktree.

| Case | Observed |
| --- | --- |
| `plan`, allowlist `Read,Write,Edit`, prompt asks to create a file | Outcome `succeeded`; a tool call was made but **no file was created** (plan mode did not edit). |
| `dontAsk`, allowlist `Read`, prompt asks to create a file | Outcome `succeeded`; no tool call, **no file created**. |
| `dontAsk`, allowlist `Read,Write`, same prompt | Outcome `succeeded`; the file **was created**. |
| `manual` with `--permission-prompts none`, allowlist `Read,Write` | Did **not** hang (about 6 s); outcome `succeeded` and the file **was created**: an allowlisted tool is pre-approved, `manual` does not stop it. |
| Failing task: `extra_args=("--model=synapse-no-such-model",)` | The CLI exits non-zero; the adapter reports `failed` (detail `exit_nonzero`), not `succeeded` or `unknown`. |
| Bash pattern: allowlist `Read,Bash(touch allowed.txt)`, `dontAsk`, prompt runs `touch allowed.txt` then `touch denied.txt` | `allowed.txt` was created; `denied.txt` was **not**: a Bash command outside the pattern did not run. |
| Usage | A real run returned `input_tokens=2, output_tokens=73` through the adapter. A direct `claude -p --output-format stream-json` run showed the final `result` also carries `usage.cache_creation_input_tokens`, `usage.cache_read_input_tokens` and a top-level `total_cost_usd`; the adapter reads these (cache tokens and `total_cost_usd`), bounds-checked as unknown when out of range. |

Eight real runs were made for these cases. Behaviour depends on the model following the prompt, so a future model
change could alter what is attempted; the file-existence assertions are about what the CLI actually permitted.

## Still unverified

- Other CLI versions (the stream-json schema may change between releases) and other tool allowlists beyond the
  cases above (for example `Edit` patterns and `WebFetch`/`WebSearch`).
- Whether `manual` mode stops a tool that is *not* allowlisted but present in `--tools`: with the current argv
  `--tools` equals the allowlist's base names, so such a tool cannot occur and this case was not exercised.
- Authentication methods other than whatever the scrubbed environment
  (`PATH`, `HOME`, `LANG`, optional `ANTHROPIC_API_KEY`) supports.
