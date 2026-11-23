"""Load one enabled plugin, failing closed at every step.

Order: read the package once -> parse manifest -> require an operator enablement that pins this
exact id, version and package hash -> check grants against requests -> execute the entrypoint
*from the hashed bytes* -> call ``register(ctx)`` -> validate what came back. Any failure raises
``PluginLoadError`` with a stable reason code; nothing is partially registered.

Limits (see docs/operations/plugin-framework.md): the entrypoint is trusted Python executed in the
daemon process. The hash pin proves WHICH code runs, not that the code is harmless, and an
in-process plugin can still import modules. The tamper check on core modules is best effort. Real
isolation needs a subprocess and is not built.

Error details never carry raw plugin-chosen text: ``PluginLoadError`` caps and escapes every detail,
and a plugin's own exception class is reported only as ``plugin-defined exception``.
"""
from __future__ import annotations

import builtins
import dataclasses
import hashlib
import inspect
import json
import json.decoder
import json.encoder
import os
import stat
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
import types

from synapse.agents import roles as roles_module
from synapse.agents.roles import AgentRole, RoleValidationError, validate_role
from synapse.plugins import api as api_module
from synapse.plugins import enablement as enablement_module
from synapse.plugins import manifest as manifest_module
from synapse.plugins import package as package_module
from synapse.plugins import policy as policy_module
from synapse.plugins import safe_text as safe_text_module
from synapse.plugins.api import PluginContext, PluginRegistration
from synapse.plugins.enablement import Enabled
from synapse.plugins.manifest import MANIFEST_NAME, ManifestError, PluginManifest, parse_manifest
from synapse.plugins.package import PackageError, snapshot_package
from synapse.plugins.policy import PluginPolicyGate, PolicyPort
from synapse.plugins.safe_text import bounded

MAX_VALIDATORS = 32
import sys as _sys

# Framework modules plus the standard-library modules the verification path relies on. package.py also
# binds its primitives at import time, so a later patch of e.g. hashlib.sha256 does not reach it; this
# list makes such a patch *during* register() visible (core_tampering) and undoes it.
_GUARDED = (roles_module, api_module, policy_module, manifest_module, package_module, enablement_module,
            safe_text_module, hashlib, json, json.decoder, json.encoder, os, stat, _sys.modules[__name__])

# Exception classes whose names are safe to report (identity check only: a plugin class is never asked
# for its name, hash or repr).
_BUILTIN_EXCEPTIONS = tuple((cls, cls.__name__) for cls in vars(builtins).values()
                            if isinstance(cls, type) and issubclass(cls, BaseException))


LOAD_ERROR_CODES = frozenset({
    "bad_registration", "capability_not_granted", "core_tampering", "directory_name_mismatch",
    "entrypoint_missing", "grant_exceeds_request", "hash_mismatch", "manifest_invalid", "no_register_function",
    "not_enabled", "package_unreadable", "plugin_raised", "reserved_id", "role_duplicate", "role_invalid",
    "role_namespace"})


class PluginLoadError(Exception):
    def __init__(self, code: str, detail: str = "") -> None:
        code = code if type(code) is str and code in LOAD_ERROR_CODES else "invalid_code"
        self.code = code
        detail = bounded(detail, 200) if detail else ""
        super().__init__(f"{code}: {detail}" if detail else code)


def _exception_name(exc: BaseException) -> str:
    cls = type(exc)
    for known, name in _BUILTIN_EXCEPTIONS:
        if cls is known:
            return name
    return "plugin-defined exception"


@dataclass(frozen=True, slots=True)
class LoadedPlugin:
    manifest: PluginManifest
    package_sha256: str
    granted: frozenset[str]
    registration: PluginRegistration


def _code_state(obj: object) -> tuple:
    return (obj.__code__, obj.__defaults__, obj.__kwdefaults__)


def _snapshot() -> tuple[dict, list[dict], list[tuple]]:
    """Builtins, guarded module dicts, class attribute dicts and function code of guarded modules."""
    deep: list[tuple] = []
    for module in _GUARDED:
        for value in list(vars(module).values()):
            if inspect.isclass(value) and value.__module__ == module.__name__:
                deep.append((value, dict(vars(value))))
            elif inspect.isfunction(value) and value.__module__ == module.__name__:
                deep.append((value, _code_state(value)))
    return dict(vars(builtins)), [dict(vars(m)) for m in _GUARDED], deep


def _restore(target: object, saved: dict) -> bool:
    """Put ``saved`` back into a module/namespace dict; return whether anything had changed."""
    live = vars(target)
    changed = False
    for key in [k for k in live if k not in saved]:
        del live[key]
        changed = True
    for key, value in saved.items():
        if key not in live or live[key] is not value:
            live[key] = value
            changed = True
    return changed


def _restore_deep(item: tuple) -> bool:
    obj, saved = item
    changed = False
    if inspect.isclass(obj):
        for key in [k for k in vars(obj) if k not in saved]:
            try:
                delattr(obj, key)
                changed = True
            except (AttributeError, TypeError):
                pass
        for key, value in saved.items():
            if key in ("__dict__", "__weakref__"):
                continue
            if vars(obj).get(key, _MISSING) is not value:
                try:
                    setattr(obj, key, value)
                    changed = True
                except (AttributeError, TypeError):
                    pass
    elif _code_state(obj) != saved:
        obj.__code__, obj.__defaults__, obj.__kwdefaults__ = saved
        changed = True
    return changed


_MISSING = object()


def _undo_tampering(before: tuple, _restore=_restore, _restore_deep=_restore_deep, _builtins=builtins,
                    _guarded=None) -> bool:
    """Best effort tripwire: undo changes to builtins and guarded modules, report whether there were any."""
    changed = _restore(_builtins, before[0])
    for module, saved in zip(_guarded or _GUARDED, before[1]):
        changed = _restore(module, saved) or changed
    for item in before[2]:
        changed = _restore_deep(item) or changed
    return changed


def load_plugin(directory: Path, enabled: Mapping[str, Enabled], policy: PolicyPort,
                audit: Callable[[dict], None]) -> LoadedPlugin:
    try:
        package = snapshot_package(Path(directory))
    except PackageError as exc:
        raise PluginLoadError("package_unreadable", str(exc)) from exc
    except OSError as exc:  # never the file name: it is chosen by whoever wrote the package
        errno = exc.errno if type(exc.errno) is int else None
        raise PluginLoadError("package_unreadable", f"os error (errno {errno})") from exc
    try:
        manifest = parse_manifest(package.files.get(MANIFEST_NAME, b""))
    except ManifestError as exc:
        raise PluginLoadError("manifest_invalid", str(exc)) from exc
    if Path(directory).name != manifest.id:
        raise PluginLoadError("directory_name_mismatch", "the package directory must be named after the plugin id")
    if manifest.id in roles_module.PLUGIN_RESERVED_NAMESPACES:
        raise PluginLoadError("reserved_id")
    entry = enabled.get(manifest.id)
    if entry is None:
        raise PluginLoadError("not_enabled")
    if entry.version != manifest.version or entry.package_sha256 != package.sha256:
        raise PluginLoadError("hash_mismatch", "enabled version or package hash differs from the package on disk")
    if not entry.granted <= manifest.capabilities:
        raise PluginLoadError("grant_exceeds_request")
    source = package.files.get(manifest.entrypoint)
    if source is None:
        raise PluginLoadError("entrypoint_missing")

    gate = PluginPolicyGate(manifest.id, entry.granted, policy, audit, version=manifest.version,
                            package_sha256=package.sha256)
    ctx = PluginContext(manifest.id, manifest.version, entry.granted, gate, package.files)
    namespace: dict = {"__name__": f"synapse_plugin_{manifest.id}", "__builtins__": builtins}
    # Bind everything the check needs locally so the plugin cannot swap it out through module globals.
    undo, validate, snapshot, guarded = _undo_tampering, _validated, _snapshot, _GUARDED
    before = snapshot()
    registration = None
    failure: PluginLoadError | None = None
    # Anything raised by plugin code, including a PluginLoadError (or subclass) the plugin raised itself,
    # becomes plugin_raised: only the framework's own checks below produce other codes.
    result = None
    try:
        code = compile(source, f"<plugin {manifest.id}/{manifest.entrypoint}>", "exec", dont_inherit=True)
        exec(code, namespace)  # noqa: S102 - trusted, hash-pinned code; see module docstring
        register = dict.get(namespace, "register")
        if callable(register):
            result = register(ctx)
        else:
            failure = PluginLoadError("no_register_function")
    except BaseException as exc:  # SystemExit, KeyboardInterrupt and GeneratorExit included
        failure = PluginLoadError("plugin_raised", _exception_name(exc))
    if failure is None:
        try:
            # Copy what came back to exact builtin values while still inside the guarded window.
            registration = validate(manifest, entry.granted, result)
        except BaseException as exc:
            failure = exc if type(exc) is PluginLoadError else PluginLoadError("plugin_raised", _exception_name(exc))
    tampered = undo(before, _guarded=guarded)
    if tampered:
        raise PluginLoadError("core_tampering", "the plugin changed builtins or a guarded core module (restored)")
    if failure is not None:
        raise failure
    return LoadedPlugin(manifest, package.sha256, entry.granted, registration)


_STR_FIELDS = ("id", "version", "title", "summary", "kind", "status", "prompt_template", "permission_mode",
               "workspace", "output_contract")


def _exact_role(role: AgentRole) -> AgentRole:
    """Rebuild a role from exact builtin values so a plugin keeps no live reference or lying subclass."""
    values = {}
    for f in dataclasses.fields(AgentRole):
        value = object.__getattribute__(role, f.name)
        if f.name in _STR_FIELDS:
            ok = type(value) is str
        elif f.name == "requires_independent_review":
            ok = type(value) is bool
        elif f.name == "max_timeout_seconds":
            ok = type(value) is int
        elif f.name == "provider_hint":
            ok = value is None or type(value) is str
        else:  # allowed_tools, may_request_approval
            ok = type(value) is tuple and all(type(v) is str for v in value)
        if not ok:
            raise PluginLoadError("role_invalid", f"field {f.name} has a non-builtin type")
        values[f.name] = value
    return AgentRole(**values)


def _validated(manifest: PluginManifest, granted: frozenset[str], result: object) -> PluginRegistration:
    if type(result) is not PluginRegistration:
        raise PluginLoadError("bad_registration", "register() must return a PluginRegistration")
    raw_roles = object.__getattribute__(result, "roles")
    raw_validators = object.__getattribute__(result, "validators")
    if type(raw_roles) is not tuple or not all(type(r) is AgentRole for r in raw_roles):
        raise PluginLoadError("bad_registration", "roles must be a tuple of AgentRole")
    if raw_roles and "register.roles" not in granted:
        raise PluginLoadError("capability_not_granted", "register.roles")
    if type(raw_validators) is not dict:
        raise PluginLoadError("bad_registration", "validators must be a plain dict of callables")
    if raw_validators and "register.validators" not in granted:
        raise PluginLoadError("capability_not_granted", "register.validators")
    prefix = manifest.id + "."
    checked = []
    for role in raw_roles:
        role = _exact_role(role)
        try:
            validate_role(role)
        except RoleValidationError as exc:
            raise PluginLoadError("role_invalid", f"{role.id}: {exc}") from exc
        if role.kind != "plugin" or not role.id.startswith(prefix):
            raise PluginLoadError("role_namespace", f"role {role.id!r} must be kind 'plugin' and start with {prefix}")
        checked.append(role)
    if len({r.id for r in checked}) != len(checked):
        raise PluginLoadError("role_duplicate")
    pairs = list(dict.items(raw_validators))
    if len(pairs) > MAX_VALIDATORS or not all(type(k) is str and k.startswith(prefix) and callable(v) for k, v in pairs):
        raise PluginLoadError("bad_registration", f"validators must be a dict of callables named {prefix}<name>")
    return PluginRegistration(tuple(checked), types.MappingProxyType(dict(pairs)))
