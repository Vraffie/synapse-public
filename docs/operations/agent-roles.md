# Agent roles (definitions only)

`synapse.agents` holds the reviewed, versioned **roles** an agent run can be built from: prompt template, tool
allowlist, permission mode, workspace kind, review and approval rules. Roles are plain Python data (no dynamic
loading; ADR-011) validated by code, pinned by a catalog hash, and listable without their prompt text.

**Status: definitions, validator, renderer and catalog only.** Nothing in the engine, the API, the CLI or the
desktop uses them yet (see [What is not done](#what-is-not-done)).

- `src/synapse/agents/roles.py`: `AgentRole`, `validate_role`, `render_prompt`, `RoleCatalog`
- `src/synapse/agents/core_roles.py`: the six core roles and `make_tester_role`
- `src/synapse/agents/planner_output.py`: strict pre-check of a planner's JSON output
- `src/synapse/agents/catalog.py`: explicit loader (`load_plugin_roles`, `load_bss_drafts`, `full_catalog`); nothing in the runtime imports it
- `plugins/domain_catalog/agents/draft_roles.py`: Domain **unverified drafts**, loaded only on explicit import

## Roles

All tool lists are exact; `Read, Glob, Grep` is the read set. No role gets `bypassPermissions`, `auto` or
`manual`, an unpatterned `Bash`, network tools or credentials.

| Role | Kind / status | Workspace | Mode | Tools | Independent review of its work | May request approval |
| --- | --- | --- | --- | --- | --- | --- |
| `planner` | core / stable | read_only | plan | Read, Glob, Grep | no | none |
| `implementer` | core / stable | edit_worktree | acceptEdits | Read, Glob, Grep, Edit, Write | **required** | `agent.run` |
| `reviewer` | core / stable | read_only | plan | Read, Glob, Grep | no (it *is* the independent check) | none |
| `tester` | core / stable | edit_worktree | dontAsk | Read, Glob, Grep, `Bash(uv run --extra test --extra desktop python -m pytest)` (one exact command; `make_tester_role(cmd)` for yours) | no (cannot edit files) | none |
| `verifier` | core / stable | read_only | plan | Read, Glob, Grep | no | none |
| `handoff` | core / stable | read_only | plan | Read, Glob, Grep | no | none |
| `domain.architect` | plugin / **draft** | read_only | plan | Read, Glob, Grep | no | none |
| `domain.requirement_analyst` | plugin / **draft** | read_only | plan | Read, Glob, Grep | no | none |
| `domain.xml_editor` | plugin / **draft** | edit_worktree | acceptEdits | Read, Glob, Grep, Edit, Write | **required** | none |
| `domain.schema_expert` | plugin / **draft** | read_only | plan | Read, Glob, Grep | no | none |
| `domain.lens_runner` | plugin / **draft** | edit_worktree | dontAsk | Read, Glob, Grep (no Bash until the real Lens command is known) | no | none |
| `domain.qa_verifier` | plugin / **draft** | read_only | plan | Read, Glob, Grep | no | none |
| `domain.deployment_verifier` | plugin / **draft** | read_only | plan | Read, Glob, Grep | no | none (never deploys) |

The tester and `domain.lens_runner` are `edit_worktree` because running a command executes repository code, which
can write inside the role's own worktree even though they carry no write tools. Their command is a single exact
pattern. The default tester command is `uv run --extra test --extra desktop python -m pytest` (the repository's full-suite command); build the role for the project's
declared test command with `make_tester_role`.

The Domain roles are **UNVERIFIED DRAFTS**: the catalog layout, Lens commands and STAGING interface were not
provided. They exist to exercise the validator and the API shape. A draft role can never be enabled
(`RoleCatalog` raises), and its summary and prompt must carry the draft banner (also enforced). Deployment is
never done by an agent: it is approval-gated and executed outside agents, with the human approval bound to the
candidate manifest.

## Catalog loading and queries (M6, still gated)

`full_catalog(repo_root)` merges the core catalog (stable roles enabled) with the Domain drafts loaded by
`load_bss_drafts(repo_root)`. Every role passes through `validate_role` via `RoleCatalog`; loading never enables
a role. `RoleCatalog.find(kind=, status=, workspace=, enabled=, latest_only=)` and `ids(...)` query the catalog
(`latest_only` picks the newest version per id over the whole catalog first, then filters, so a newer draft hides
an older stable); `to_api()` lists metadata without prompt text. None of this is imported by the daemon, API,
CLI or desktop, so Domain roles are **not** on the spawn path, and `manual` mode stays refused.

### Plugin files are trusted code, not sandboxed data

A plugin roles file is **executed Python, reviewed like any other source file**. The loader is not a sandbox and
does not make an unreviewed file safe. It only narrows the obvious accidents and the import-time attack on the
validator: it loads exactly `plugins/<name>/agents/draft_roles.py` (`<name>` matches `[a-z][a-z0-9_]{0,31}`; a
helper `.py`, a deeper path or a symlink leaving the root is refused); compiles the source text itself, so no
`__pycache__` is read or written under `plugins/`; takes a best-effort snapshot of the validator modules
(`roles`, `core_roles`, `catalog`: bindings, `AgentRole`/`RoleCatalog` attributes, every function's `__code__`,
`__defaults__` and `__kwdefaults__`, module constants such as tool/deny sets and regex patterns, and a few
builtins) before the exec and refuses the plugin, restoring the snapshot, if any of it changed; refuses a plugin
directory whose namespace another plugin directory already claims (`domain` and `domain_catalog`); and requires every
role id to start with the plugin's namespace (the directory name without a trailing `_catalog`: `domain_catalog`
owns `domain.*`; `synapse` and `core` are reserved). A `*_ROLES` value that is not a tuple raises.

This is detection of accidental or naive tampering only, **not a security boundary**. A plugin file is trusted
code reviewed like source. Import-time side effects (writing files, network, other process state) and cleverer
mutation are not detected; real isolation would need running the file in a subprocess. Review plugin changes as
code.

### Plugin ceiling

Plugin roles are held to **at most what a core role may hold, and in two places less**:

- tools: only `Read`, `Glob`, `Grep`, `Edit`, `Write` and `Bash` (core also allows `MultiEdit`, `NotebookEdit`);
  no `Bash` when read_only; `Bash` only in `dontAsk`; no approval requests; timeout at most 1800 s (core 3600);
  id namespaced (`<plugin>.<name>`);
- `Bash` patterns come from an explicit approved set only: the exact tester command
  `uv run --extra test --extra desktop python -m pytest`, the read-only git commands (`git status`, `git diff`,
  `git diff --stat`, `git log --oneline`, `git show --stat`, `git rev-parse HEAD`, `git ls-files`) (no placeholder
  commands). Core roles may use any exact pattern that passes the deny list;
  plugin roles may not;
- status may only be `draft` for now (no approval marker for stable plugin roles exists), and a plugin role can
  never be enabled in a catalog (`RoleCatalog` and `with_enabled` refuse it).

Core ids must not contain a dot, and a catalog refuses one id used by two kinds, so a plugin cannot shadow a core
role. For every role, write tools combined with `Bash` are refused (write a file, then run it).

## What the validator enforces

`validate_role` refuses a role (listing every problem) unless:

- `permission_mode` is `acceptEdits`, `plan` or `dontAsk`. `bypassPermissions` and `auto` are never allowed.
  `manual` is refused too: with prompts disabled it does not gate allowlisted tools (verified against claude
  2.1.329, see [claude-cli-provider](claude-cli-provider.md)), so it would look like a safeguard without being one.
- Tools come from a closed set: `Read`, `Glob`, `Grep`, `Edit`, `Write`, `MultiEdit`, `NotebookEdit` and
  *patterned* `Bash`. `WebFetch`, `WebSearch`, `Task`, `mcp__*` and anything else are refused. Names follow the
  same grammar as the Claude adapter (pinned by a test).
- A **read_only** role has no write tools, uses `plan` or `dontAsk`, requests no approvals, and may run `Bash`
  only as one of a fixed set of exact non-writing commands (`git status`, `git diff`, `git diff --stat`,
  `git log --oneline`, `git show --stat`, `git rev-parse HEAD`, `git ls-files`). Core roles carry none: the
  reviewer gets the diff through `{candidate}`, because `git diff` can run a repo-configured diff driver.
- An **edit_worktree** role never has unpatterned `Bash`; a `Bash` pattern is an exact command (no wildcard,
  quote, pipe, redirect, `;`, `&`, `$` or backtick), never starts with a shell, interpreter, file mutator,
  network tool or any `git` command, never uses inline-code flags (`-c`, `-e`, `--eval`, `--exec`), and never
  contains package installers or publishers (`pip`, `npm`, `pnpm`, `yarn`, `npx`, `uv add|pip|tool|sync`), a deploy
  verb (`deploy`, `publish`, `release`, `make deploy|release`) or `http.server`. `Bash` is
  **never combined with `acceptEdits`** (an independent review suspects that mode auto-approves common
  filesystem commands even when not allowlisted; unverified, so the combination is refused).
- A role that can write files sets `requires_independent_review` and has the `edit_worktree` workspace.
- The prompt contains the untrusted-data guard (`UNTRUSTED-DATA-GUARD-v1`) before its first placeholder, at least
  one placeholder, and only closed-set placeholders (`task`, `requirements`, `context_manifest`, `candidate`,
  `findings`, `checkpoint_source`), written plainly (no format spec, conversion, attribute or index).
- A `codex` provider hint keeps to Codex's limits from ADR 016: tools within `Read, Glob, Grep, Edit, Write`,
  mode `plan` or `acceptEdits`.
- Sizes are bounded: title 80 B, summary 400 B, template 16 KiB, output contract 2 KiB, 16 tools, timeout
  1 to 3600 s. Strict types (a bool must be a bool).

## Rendering prompts

`render_prompt(role, values)` validates the role again, then substitutes **only** the template's own
placeholders, in a single pass over the template's literal text, so a value is never re-parsed:
`{task}` or `{{` inside a value stays literal. Missing, extra, non-string, NUL-containing or oversized
(16 KiB) values raise; the rendered prompt may not exceed the 32 KiB agent prompt limit.

Each value is cleaned (LF newlines, control and format/bidi characters removed, `<<<` and `>>>` replaced by
look-alike characters) and fenced:

```
<<<UNTRUSTED-DATA candidate 3f2a...>>>
...value...
<<<END-UNTRUSTED-DATA candidate 3f2a...>>>
```

The digest is a SHA-256 prefix of the cleaned value, so the closing line cannot be forged from inside the
value (it would have to contain its own hash). Fencing and the guard text reduce prompt-injection risk; they do
not remove it. Repository text and tool output remain untrusted whatever the prompt says, which is why the
write-capable roles are always independently reviewed and the dangerous capabilities are not in any role.

## Independent review

Invariant: an agent's work is not accepted on its own say-so. `implementer` and `domain.xml_editor` carry
`requires_independent_review`. `reviewer_is_independent(author_task_id, reviewer_task_id, reviewer_role)` is the
pure rule the scheduler must call before accepting a review: a **different task** (hence a different agent
session and worktree) **and** a reviewer role that is read-only and valid. Reviewers, verifiers and the
deployment verifier cannot approve anything: agents never approve, and approval decisions stay with a human
through the separate approver token (see [approver-identity](approver-identity.md)).

The engine-side enforcement lives in `synapse.orchestration.review_independence` (ADR 025): review nodes,
identity checks (task, attempt, session, optionally profile), a verdict bound to the exact candidate SHA-256,
and fail-closed blocking with an audit trail. It does not import this catalog. `reviewer_is_independent` stays
as the pure catalog-level rule. A same-model reviewer is **not** model-independent; see the ADR.

## Catalog and API shape

`RoleCatalog(roles, enabled)` validates every role, rejects duplicate `(id, version)` and refuses to enable an
unknown or draft role. `get(id, version=None)` returns the exact or highest version. `catalog_sha256()` hashes
every field of every role (including prompt text) plus the enabled set, so an operator can pin a catalog.
`to_api()` is what a future `GET /api/v1/agents` returns: metadata, tools, mode, review and approval rules,
`enabled`, and a `prompt_sha256`, **never** the prompt text. `core_catalog()` enables all stable core roles;
`merged()` combines a core catalog with an explicitly imported plugin catalog.

## Planner output and `POST /agent-runs` `tasks`

`parse_planner_output(text, tool_ceiling=...)` accepts only a single JSON array (empty means "could not plan
safely"): at most 6 tasks, each `{id, prompt, allowed_tools, depends_on}` with unique valid ids, tools within the
caller's ceiling (normally the operator allowlist, so a planner cannot widen tools), known non-self
dependencies and no cycles. Duplicate keys, unknown keys, NaN, oversize and trailing text are refused, and
error messages never echo the output. A valid result is exactly the `tasks` list of the multi-task
`POST /api/v1/agent-runs` body (PR #88); the API re-validates it against the operator profile.

## What is not done

- No engine, API, CLI or desktop wiring: roles are not yet turned into task specs, `GET /api/v1/agents` does
  not exist, there is no desktop role picker, and the scheduler does not call `reviewer_is_independent` (the engine has its own ADR 025 review gate, which is not yet wired into the daemon).
- The Domain roles are drafts: nothing about the real catalog layout, Catalog Validator or STAGING has been verified.
- Prompts are untested against real models; their output contracts are guidance, not enforced schemas (only the
  planner has a strict parser).
- The Bash-with-`acceptEdits` refusal and the `git diff` driver concern are based on an unverified review
  suspicion and a known git behaviour; nothing here was demonstrated against the real CLI.
