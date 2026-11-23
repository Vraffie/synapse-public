# Plugin framework (M6.3, ADR-011)

`src/synapse/plugins/` loads versioned plugin packages. It is **not wired into the daemon**: nothing calls
`load_plugin` yet, so no plugin runs in a real daemon today. The existing Domain draft roles still load
through `synapse.agents.catalog`; moving them onto this framework is a separate step.

## Trust model, in one paragraph (read this first)

**A plugin is trusted code that runs inside the daemon process, as the same OS user.** There is no
isolation. Nothing in this framework can stop an enabled plugin that wants to misbehave: it can import any
module, read and write any file the user can (including the database and `plugins.json`), patch other
modules after its own load, and reach the policy gate's internals. The checks below catch mistakes,
accidental changes and naive misbehaviour, and they make some tampering visible. They are **not a security
boundary**. Enable only code you have read and would run as yourself.

## Package layout

```
plugins/<id>/manifest.json   # exactly: manifest_version=1, id, version (x.y.z), api_version=1,
                             #          entrypoint (*.py in the package root), description, capabilities
plugins/<id>/<entrypoint>    # defines register(ctx) -> PluginRegistration
```

The directory name must equal the manifest `id`. `core` and `synapse` are reserved. The package walk refuses
symlinks, special files (the opened file descriptor is checked with `fstat`, so a file swapped for a FIFO
after the scan is refused, as is a file whose inode changed), more than 500 files, 2 MiB per file, 16 MiB in
total, 64 directories, nesting deeper than 8, and more than 2000 directory entries (counted while iterating,
so a huge directory is refused without being listed in full).

## Enablement (operator decision)

`<state>/plugins.json`, user-owned, mode 0600, not a symlink, otherwise refused. Each entry pins `id`,
`version`, the package SHA-256 and the `granted` capabilities. A missing file means nothing is enabled.

```
python -m synapse.plugins hash   plugins/<id>                              # review the code, note package_sha256
python -m synapse.plugins enable <state>/plugins.json plugins/<id> <package_sha256> register.roles ...
python -m synapse.plugins digest <state>/plugins.json                       # later: compare with the recorded digest
```

`enable` (`enable_package` in code) hashes the directory again and records nothing unless it still matches
the hash you reviewed. `enable_plugin` is the low-level call that trusts the hash its caller passes; prefer
`enable_package`. Granting more than the manifest requested is refused.

### What the hash pin does and does not protect

- It protects against **on-disk changes**: any byte change to any file in the package (or a version bump)
  fails the hash check and the plugin does not load until re-enabled. The package is read once into memory
  and the entrypoint is executed from those hashed bytes, so there is no gap between "verified" and "run".
- It is **only as strong as every other enabled plugin.** Verification runs in the same process as plugins
  that already loaded. `package.py` binds SHA-256 and the `os`/`stat` primitives it uses once, at import
  time, so a later `hashlib.sha256 = Fake` by another plugin does not change how packages are hashed, and
  the loader's tripwire watches `hashlib`, `json`, `os` and `stat` during `register()` (a patch there is
  undone and refused as `core_tampering`). But a plugin can still patch `synapse.plugins.package` itself,
  or the loader, from a thread or a validator after its own load, outside the tripwire's window. Then the
  next plugin's pin means nothing.
- It says **which** code runs, not that the code is harmless.

### `plugins.json` can be rewritten by any enabled plugin

Because daemon, operator and plugins share one uid, an enabled plugin can rewrite `plugins.json` (for
example to raise its own grants for the next start). File mode and ownership checks cannot prevent that.
Decision: **detect, do not pretend to prevent.**

- `enable` prints the SHA-256 of the exact file it wrote; `digest` (`read_enablement` in code) prints the
  SHA-256 of the exact bytes currently on disk. Record the digest **somewhere plugins cannot write** (not
  in the same user's files: a password manager, a note on another machine). A different digest at the next
  check means the file changed since your last decision.
- We deliberately did **not** add a checksum or "grants digest" field inside the file: a plugin can
  recompute it as easily as the host can, so it would only look like protection. A keyed MAC has the same
  problem while its key is readable by the same uid.
- The host should read `plugins.json` once, before any plugin code runs, so a rewrite takes effect at the
  next start at the earliest. (Not enforced yet: the loader is not wired into the daemon.)

## What a plugin can do

| Capability | Meaning |
| --- | --- |
| `register.roles` | return `AgentRole`s; each is checked by `validate_role`, must be `kind="plugin"`, id `<plugin>.<name>`, still `draft` until a stable approval marker exists |
| `register.validators` | return named callables `<plugin>.<name>` (not invoked by the framework yet) |
| `repo.write`, `git.push`, `git.merge`, `deploy.staging`, `approval.request` | privileged: only via `ctx.request(action, resource)` |

`deploy.production` does not exist in the vocabulary, so it cannot be requested, granted or authorised.

`PluginContext` exposes only `request(action, resource)` and `read_file(relpath)` (the plugin's own hashed
bytes); no database, repository, path or approval object is part of its API. `request` returns a `Decision`
and **executes nothing**: it is permission only, and nothing binds the host's approval to it yet. For an
honest caller it denies on: unknown action, not granted (the policy is not even asked), non-JSON or over
4 KiB resource, a policy that raises, any answer that is not exactly `Decision(allowed=True, ...)`, a failed
audit write, and more than 256 requests from one loaded plugin (`request_limit`; only the first
over-budget denial is audited, so a loop cannot flood the audit). The default policy is `DenyAllPolicy`.

**Identity passed to the policy.** `PolicyPort.authorize(principal, action, resource, *, version,
package_sha256)`: principal is `plugin:<id>`, and `version` and `package_sha256` are the exact values
verified at load. An implementation that stores approvals must key them on the package hash, so an
approval for v1 does not cover v2. Each audit row carries `principal`, `version`, `package_sha256`,
`action`, `allowed` and `reason`.

**Plugin-chosen text is bounded.** A `PluginLoadError` code must be one of the fixed reason codes (anything
else becomes `invalid_code`), and any exception raised by plugin code, including a `PluginLoadError` or a
subclass the plugin raised itself, is reported as `plugin_raised`. Every detail is capped and escaped (control characters,
newlines, ANSI sequences and non-ASCII become `\x..` escapes; non-`str` values become `<non-text>`). A plugin's
own exception class is reported only as `plugin-defined exception` (built-in exception names are reported
as is). Package file names and manifest values in errors are escaped the same way. The audit row's `action`
is the action name only if it is exactly one of the allowlisted names; anything else is recorded as
`action: null` with reason `unknown_action`. A policy's allow reason is capped at 200 characters and escaped.

At load time `register()` results are copied to exact builtin values inside the guarded window: roles are
rebuilt field by field (lying `str` subclasses are refused), validators are copied from a plain `dict`.
The stored roles are therefore separate objects that the plugin cannot edit afterwards.

## What this does NOT protect against

- **The gate is reachable.** A plugin can walk `ctx._gate._policy` and `ctx._gate._audit` (and
  `gc.get_objects()`, closures, `sys.modules`), call the policy directly, rewrite its own grants, principal,
  identity or request counter, and skip the audit. Python offers no way to hide an object graph in-process.
  When PolicyPort is bound to the real approval service, that service (and its credentials) must not live
  in a process that runs plugin code.
- **Same uid.** A plugin can open the SQLite database, `plugins.json`, other plugins' packages and the
  user's files directly. "No database handle" describes the API, not a sandbox.
- **The tamper check is a tripwire.** Around `register()` it snapshots builtins, the framework and roles
  modules (including this loader), `hashlib`, `json` (with `json.decoder`/`json.encoder`), `os` and `stat`,
  their class attributes and function code, and undoes and refuses changes it finds (`core_tampering`). It
  does not see instance state (for example `json._default_decoder`'s attributes), changes made later (from
  a thread the plugin starts or from a validator the host later calls), and it can be raced. A legitimate
  plugin that imports a new submodule of a watched package (e.g. `import json.tool`) is refused too, and
  so is any load during which *another* daemon thread adds an attribute to a watched module (the undo then
  also removes that thread's attribute): load plugins before starting other threads.
  **Known gaps, both verified by probe and not detected:** (1) the check's own function objects can be
  mutated in place, for example `sys.modules['synapse.plugins.loader']._undo_tampering.__code__ = ...` makes
  it report nothing (binding the names locally only defeats replacing the module attribute, not changing the
  function object); (2) code state is recorded only for module-level functions, so changing a class method's
  `__code__` (for example `PluginPolicyGate._decide.__code__`) is not seen, and a later plugin's gate can
  then allow an action it was never granted. Neither is a bug to be fixed here: the tripwire is for
  accidental or naive changes, not a boundary, and in-process plugins are trusted code. Two tests document
  these gaps; if one starts failing, a change closed it and that should be a deliberate decision.
- **Registered validators are plugin code that runs later,** outside any of these checks. A `Decision`
  the host receives from a plugin (for instance a validator's return value) must never be treated as a
  policy decision; only a `Decision` the host got from its own gate call counts.
- **Real isolation needs a separate process** with its own uid and credentials. Not built; proposed as the
  ADR follow-up for this milestone.
- `PolicyPort` is an interface here. Connecting it to the approval service is daemon work (A's area).
- Not hardened: `enable_plugin` is not safe against two concurrent operators; the package walk is not
  `openat`-based, so a directory swapped mid-walk is caught by the pin for enabled plugins but not for `hash`
  output.

## Tests

`tests/test_plugin_framework.py`: pinning and tamper refusal, grant ceilings, enablement file safety and
digest, `enable_package` refusing a package changed since review, manifest strictness, symlink/FIFO/non-UTF-8
packages, walk limits (depth, directory count, entries), the fd check (FIFO or inode swap after the scan), and
hostile plugins that are refused: edits to core modules, `hashlib`/`os`/`json`/`stat`, classes, function code
or builtins (undone), replacing the check's module attribute, role namespace escape, lying `str` subclasses and mappings,
`SystemExit`/`KeyboardInterrupt`, raising or returning garbage. Regression for the review finding: plugin F
patching `hashlib.sha256` is refused and undone, and plugin E's changed bytes still fail `hash_mismatch`, also
when the patch is applied outside the tripwire's window. Also: version and package hash reach the policy and
the audit row; a 100 KB exception class name with newline/ANSI, hostile file names and manifest values, and a
10 MB action string never reach errors or audit rows raw; the request budget. Not tested, because it is not
prevented: a plugin reaching the gate's policy or audit objects, a plugin rewriting `plugins.json`, a plugin
patching `synapse.plugins.package` after its own load (see above).
