# Plugin framework (M6.3, ADR-011)

`src/synapse/plugins/` loads trusted, versioned plugin packages. It is **not wired into the daemon**:
nothing calls `load_plugin` yet, so no plugin runs in a real daemon today. The existing Domain draft roles
still load through `synapse.agents.catalog`; moving them onto this framework is a separate step.

## Package layout

```
plugins/<id>/manifest.json   # exactly: manifest_version=1, id, version (x.y.z), api_version=1,
                             #          entrypoint (*.py in the package root), description, capabilities
plugins/<id>/<entrypoint>    # defines register(ctx) -> PluginRegistration
```

The directory name must equal the manifest `id`. `core` and `synapse` are reserved.

## Enablement (operator decision)

`<state>/plugins.json`, user-owned, mode 0600, not a symlink, otherwise refused. Each entry pins `id`,
`version`, the package SHA-256 and the `granted` capabilities. A missing file means nothing is enabled.
Print the pin for a package: `python -m synapse.plugins hash plugins/<id>`. The operator reviews the code,
then records the decision with `enable_plugin(...)`. Granting more than the manifest requested is refused.
Any byte change to any file in the package (or a version bump) fails the hash check and the plugin does not
load until re-enabled. The package is read once into memory and the entrypoint is executed from those hashed
bytes, so there is no gap between "verified" and "run".

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
4 KiB resource, a policy that raises, any answer that is not exactly `Decision(allowed=True, ...)`, and a
failed audit write. Each decision is audited with principal `plugin:<id>`. The default is `DenyAllPolicy`.

At load time `register()` results are copied to exact builtin values inside the guarded window: roles are
rebuilt field by field (lying `str` subclasses are refused), validators are copied from a plain `dict`.
The stored roles are therefore separate objects that the plugin cannot edit afterwards.

## What this does NOT protect against (read before enabling anything)

A plugin runs in the daemon process. The gate and the checks below stop mistakes and casual misbehaviour;
they are **not a security boundary against hostile code that is enabled**.

- **The gate is reachable.** A plugin can walk `ctx._gate._policy` and `ctx._gate._audit` (and
  `gc.get_objects()`, closures, `sys.modules`), call the policy directly, rewrite its own grants or
  principal, and skip the audit. A security review proved this against the loader. Python offers no way to
  hide an object graph in-process. When PolicyPort is bound to the real approval service, that service
  (and its credentials) must not live in a process that runs plugin code.
- **The hash pin proves which code runs, not that it is harmless.** An enabled plugin can `import sqlite3`
  or `os`. "No database handle" describes the API, not a sandbox. Read plugin code before enabling it.
- **The tamper check is a tripwire.** Around `register()` it snapshots builtins, the framework and roles
  modules (including this loader), their class attributes and function code, and undoes and refuses
  changes it finds (`core_tampering`). It cannot see changes made later (for example from a thread the
  plugin starts, or from a validator it registered and the host later calls), and it can be raced.
- **Registered validators are plugin code that runs later,** outside any of these checks. A `Decision`
  the host receives from a plugin (for instance a validator's return value) must never be treated as a
  policy decision; only a `Decision` the host got from its own gate call counts.
- **Real isolation needs a separate process** with its own credentials. Not built; proposed as the ADR
  follow-up for this milestone.
- `PolicyPort` is an interface here. Connecting it to the approval service is daemon work (A's area).
- Not hardened: package walk races (a directory swapped for a symlink mid-walk; the pin still catches it for
  enabled plugins, not for `hash` output), `enable_plugin` is not safe against two concurrent operators,
  and a symlinked enablement file raises a raw `OSError` (it still refuses).

## Tests

`tests/test_plugin_framework.py`: pinning and tamper refusal, grant ceilings, enablement file safety,
manifest strictness, symlink/FIFO/non-UTF-8 packages, and hostile plugins that are refused: edits to core modules,
classes, function code or builtins (undone), switching the check off, role namespace escape, lying `str`
subclasses and mappings, `SystemExit`/`KeyboardInterrupt`, raising or returning garbage. Not tested, because
it is not prevented: a plugin reaching the gate's policy or audit objects (see above).
