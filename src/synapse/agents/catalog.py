"""Explicit loading of plugin role sets into a :class:`RoleCatalog` (still gated, definitions only).

Nothing here is imported by the engine, the API or the daemon spawn path, and nothing imports plugins
implicitly (ADR-011): a caller names a plugin role file and it is executed, then every role passes through
:func:`validate_role` via :class:`RoleCatalog`. Draft roles stay un-enableable, and plugin roles can never
be enabled at all.

TRUST MODEL: a plugin roles file is TRUSTED CODE, reviewed like any source file in this repository. It is
executed (not parsed as inert data) and is NOT sandboxed. The tamper check below is a best-effort detector of
accidental or naive tampering only; it is NOT a security boundary. Import-time side effects (writing files,
network, patching things the snapshot does not cover) and cleverer in-place mutation are NOT detected, and a
malicious file that was merged without review is not stopped by it. Real isolation would need a subprocess.
Other guards: only the exact path shape ``<plugins_root>/<name>/agents/draft_roles.py`` is loaded; the file is
compiled from source text (no ``__pycache__`` is read or written); a namespace can be claimed once; role ids
must live in the plugin's namespace.
"""
from __future__ import annotations

import builtins
from pathlib import Path
import re
import sys
import types

from synapse.agents import core_roles as _core_roles
from synapse.agents import roles as _roles
from synapse.agents.core_roles import core_catalog
from synapse.agents.roles import (
    PLUGIN_RESERVED_NAMESPACES, AgentRole, RoleCatalog, RoleValidationError,
)

DOMAIN_DRAFT_ROLES_RELPATH = Path("plugins") / "domain_catalog" / "agents" / "draft_roles.py"
_PLUGIN_DIR_RE = re.compile(r"[a-z][a-z0-9_]{0,31}")
_ROLES_FILENAME = "draft_roles.py"


def plugin_namespace(plugin_dir: str) -> str:
    """Role-id namespace of a plugin directory: its name without a trailing ``_catalog`` (``domain_catalog`` -> ``domain``)."""
    return plugin_dir.removesuffix("_catalog")


def _guarded_modules() -> tuple[types.ModuleType, ...]:
    return (_roles, _core_roles, sys.modules[__name__])


_WATCHED_BUILTINS = ("isinstance", "len", "getattr", "str", "tuple", "frozenset", "set")
_FUNC_ATTRS = ("__code__", "__defaults__", "__kwdefaults__")


def _functions(mod: types.ModuleType):
    """(label, function) for module-level functions and functions/property accessors of the role classes."""
    for key, val in list(vars(mod).items()):
        if isinstance(val, types.FunctionType):
            yield "%s.%s" % (mod.__name__, key), val
    for cname in ("AgentRole", "RoleCatalog"):
        cls = getattr(mod, cname, None)
        if isinstance(cls, type) and cls.__module__ == _roles.__name__:
            for key, val in list(cls.__dict__.items()):
                val = getattr(val, "fget", val)
                if isinstance(val, types.FunctionType):
                    yield "%s.%s.%s" % (mod.__name__, cname, key), val


def _constant_state(val):
    """Comparable state of a module constant, or ``_MISSING`` when it is not a watched kind (patterns, frozensets, ...)."""
    if isinstance(val, re.Pattern):
        return ("re", val.pattern, val.flags)
    if isinstance(val, (frozenset, set)):
        return ("set", frozenset(val))
    if isinstance(val, (list, tuple)):
        return ("seq", tuple(val))
    if isinstance(val, dict):
        return ("dict", dict(val))
    return _MISSING


def _snapshot() -> dict:
    mods = []
    for mod in _guarded_modules():
        cls_dicts = {}
        for name in ("AgentRole", "RoleCatalog"):
            cls = getattr(mod, name, None)
            if isinstance(cls, type) and cls.__module__ == _roles.__name__:
                cls_dicts[name] = dict(cls.__dict__)
        consts = {k: (v, _constant_state(v)) for k, v in vars(mod).items() if _constant_state(v) is not _MISSING}
        mods.append((mod, dict(vars(mod)), cls_dicts, consts))
    funcs = [(label, fn, tuple(getattr(fn, a) for a in _FUNC_ATTRS)) for m in _guarded_modules() for label, fn in _functions(m)]
    builtins_ = {n: getattr(builtins, n) for n in _WATCHED_BUILTINS}
    return {"modules": mods, "funcs": funcs, "builtins": builtins_}


def _tampering(snap) -> list[str]:
    """Names whose binding, class attribute, function code/defaults, constant value or watched builtin changed; restores them."""
    changed: list[str] = []
    for name, orig in snap["builtins"].items():  # first: the checks below use these builtins
        if getattr(builtins, name, _MISSING) is not orig:
            changed.append("builtins.%s" % name)
            setattr(builtins, name, orig)
    for mod, before, cls_dicts, consts in snap["modules"]:
        now = vars(mod)
        for key in set(before) | set(now):
            if key == "__builtins__":
                continue
            if key not in now or key not in before or now[key] is not before[key]:
                changed.append("%s.%s" % (mod.__name__, key))
                if key in before:
                    now[key] = before[key]
                else:
                    now.pop(key, None)
        for key, (obj, state) in consts.items():
            if _constant_state(obj) != state:
                changed.append("%s.%s" % (mod.__name__, key))
                if isinstance(obj, list):
                    obj[:] = state[1]
                elif isinstance(obj, (set, dict)):
                    obj.clear()
                    obj.update(state[1])
        for cname, cdict in cls_dicts.items():
            cls = getattr(mod, cname)
            for key in set(cdict) | set(cls.__dict__):
                if key in ("__dict__", "__weakref__"):
                    continue
                if cls.__dict__.get(key, _MISSING) is not cdict.get(key, _MISSING):
                    changed.append("%s.%s.%s" % (mod.__name__, cname, key))
                    try:
                        if key in cdict:
                            setattr(cls, key, cdict[key])
                        else:
                            delattr(cls, key)
                    except (AttributeError, TypeError):
                        pass
    for label, fn, originals in snap["funcs"]:
        for attr, orig in zip(_FUNC_ATTRS, originals):
            if getattr(fn, attr) is not orig:
                changed.append("%s.%s" % (label, attr))
                try:
                    setattr(fn, attr, orig)
                except (AttributeError, TypeError):
                    pass
    return sorted(set(changed))


_MISSING = object()


def _plugin_dir_name(path: Path, root: Path) -> str:
    rel = path.relative_to(root)
    if len(rel.parts) != 3 or rel.parts[1] != "agents" or rel.parts[2] != _ROLES_FILENAME:
        raise RoleValidationError(["plugin roles file must be exactly plugins/<name>/agents/%s" % _ROLES_FILENAME])
    if not _PLUGIN_DIR_RE.fullmatch(rel.parts[0]):
        raise RoleValidationError(["plugin directory name must match [a-z][a-z0-9_]{0,31}"])
    return rel.parts[0]


def _claim_namespace(plugins_root: Path, plugin_dir: str, namespace: str) -> None:
    """Refuse a plugin whose namespace another plugin directory already claims (``domain`` and ``domain_catalog``)."""
    for sibling in sorted(p.name for p in plugins_root.iterdir() if p.is_dir()):
        if sibling != plugin_dir and _PLUGIN_DIR_RE.fullmatch(sibling) and plugin_namespace(sibling) == namespace \
                and (plugins_root / sibling / "agents" / _ROLES_FILENAME).is_file():
            raise RoleValidationError(["namespace %r is already claimed by plugin %r; %r cannot claim it too" % (namespace, sibling, plugin_dir)])


def load_plugin_roles(roles_file: Path, *, plugins_root: Path) -> RoleCatalog:
    """Load ``<NAME>_ROLES`` tuples from ``<plugins_root>/<name>/agents/draft_roles.py`` (trusted code, see module doc)."""
    root = Path(plugins_root).resolve()
    path = Path(roles_file).resolve()
    if not path.is_relative_to(root) or path.suffix != ".py" or not path.is_file():
        raise RoleValidationError(["plugin roles file must be an existing .py file under the plugins root"])
    plugin_dir = _plugin_dir_name(path, root)
    namespace = plugin_namespace(plugin_dir)
    if namespace in PLUGIN_RESERVED_NAMESPACES:
        raise RoleValidationError(["plugin namespace %r is reserved" % namespace])
    _claim_namespace(root, plugin_dir, namespace)
    source = path.read_text(encoding="utf-8")
    module = types.ModuleType("synapse_plugin_roles_" + plugin_dir)
    module.__file__ = str(path)
    snap = _snapshot()
    prior_flag = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        exec(compile(source, str(path), "exec", dont_inherit=True), module.__dict__)  # noqa: S102 - trusted, reviewed code
    finally:
        sys.dont_write_bytecode = prior_flag
        changed = _tampering(snap)
    if changed:
        raise RoleValidationError(["plugin roles file modified the role validator or builtins at import time (best-effort detection): " + ", ".join(changed[:8])])
    roles: list[AgentRole] = []
    for name, value in vars(module).items():
        if name.endswith("_ROLES"):
            if not isinstance(value, tuple):
                raise RoleValidationError(["%s must be a tuple of AgentRole, not %s" % (name, type(value).__name__)])
            roles.extend(value)
    if not roles or not all(isinstance(r, AgentRole) for r in roles):
        raise RoleValidationError(["plugin roles file must define a non-empty <NAME>_ROLES tuple of AgentRole"])
    wrong = [r.id for r in roles if r.kind != "plugin"]
    if wrong:
        raise RoleValidationError(["plugin roles file may define only kind='plugin' roles: " + ", ".join(wrong)])
    foreign = [r.id for r in roles if not (isinstance(r.id, str) and r.id.startswith(namespace + "."))]
    if foreign:
        raise RoleValidationError(["role ids must start with %r (the plugin's namespace): %s" % (namespace + ".", ", ".join(map(str, foreign)))])
    return RoleCatalog(tuple(roles), frozenset())  # plugin roles are never enabled by loading


def load_bss_drafts(repo_root: Path) -> RoleCatalog:
    root = Path(repo_root)
    return load_plugin_roles(root / DOMAIN_DRAFT_ROLES_RELPATH, plugins_root=root / "plugins")


def full_catalog(repo_root: Path | None = None) -> RoleCatalog:
    """Core roles (stable ones enabled) merged with the Domain drafts when ``repo_root`` is given (none enabled)."""
    core = core_catalog()
    return core if repo_root is None else core.merged(load_bss_drafts(repo_root))
