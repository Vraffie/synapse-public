"""Domain XML dependency index and Catalog Validator command planning (ADR-020). Pure, read-only, not wired anywhere.

UNVERIFIED: attribute names and Lens command shape are placeholders until the catalog layout is inventoried.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Mapping, Optional, Protocol, Sequence, Set, Tuple
from xml.etree import ElementTree as ET

Node = Tuple[str, str]  # (repo_id, element id)


class XmlIndexError(ValueError):
    pass


@dataclass(frozen=True)
class IndexConfig:
    id_attrs: Tuple[str, ...] = ("id", "name")
    ref_attrs: Tuple[str, ...] = ("ref", "href", "refId")
    max_file_bytes: int = 2_000_000
    max_files_per_repo: int = 5_000
    max_nodes: int = 200_000
    max_edges: int = 500_000
    max_depth: int = 64


_FORBIDDEN = re.compile(rb"<!\s*(DOCTYPE|ENTITY)", re.IGNORECASE)


def parse_xml_safely(data: bytes, max_bytes: int = 2_000_000) -> ET.Element:
    if len(data) > max_bytes:
        raise XmlIndexError("xml input exceeds %d bytes" % max_bytes)
    if _FORBIDDEN.search(data):
        raise XmlIndexError("DTD/entity declarations are not allowed")
    try:
        return ET.fromstring(data, parser=ET.XMLParser())
    except ET.ParseError as exc:
        raise XmlIndexError("malformed xml: %s" % exc) from exc
    except RecursionError as exc:
        raise XmlIndexError("xml nesting too deep") from exc


def _extract(root: ET.Element, cfg: IndexConfig) -> Tuple[Set[str], Set[Tuple[str, str]]]:
    defs: Set[str] = set()
    refs: Set[Tuple[str, str]] = set()
    stack: List[Tuple[ET.Element, Optional[str], int]] = [(root, None, 0)]
    while stack:
        el, owner, depth = stack.pop()
        if depth > cfg.max_depth:
            raise XmlIndexError("xml nesting exceeds %d" % cfg.max_depth)
        own_id = next((el.attrib[a] for a in cfg.id_attrs if el.attrib.get(a)), None)
        if own_id:
            defs.add(own_id)
        cur = own_id or owner
        for a in cfg.ref_attrs:
            tgt = el.attrib.get(a)
            if tgt:
                refs.add((cur or "", tgt))
        for child in el:
            stack.append((child, cur, depth + 1))
    return defs, refs


@dataclass
class DependencyIndex:
    nodes: Set[Node] = field(default_factory=set)
    edges: Set[Tuple[Node, Node]] = field(default_factory=set)
    unresolved: Set[Tuple[Node, str]] = field(default_factory=set)
    ambiguous: Set[Tuple[Node, str]] = field(default_factory=set)

    def dependencies(self, node: Node) -> List[Node]:
        return sorted(t for s, t in self.edges if s == node)

    def dependents(self, node: Node) -> List[Node]:
        return sorted(s for s, t in self.edges if t == node)

    def _walk(self, node: Node, forward: bool, max_depth: Optional[int]) -> List[Node]:
        adj: Dict[Node, List[Node]] = {}
        for s, t in self.edges:
            a, b = (s, t) if forward else (t, s)
            adj.setdefault(a, []).append(b)
        seen: Set[Node] = set()
        frontier = [node]
        depth = 0
        while frontier and (max_depth is None or depth < max_depth):
            nxt: List[Node] = []
            for n in frontier:
                for m in adj.get(n, ()):
                    if m not in seen and m != node:
                        seen.add(m)
                        nxt.append(m)
            frontier, depth = nxt, depth + 1
        return sorted(seen)

    def transitive_dependencies(self, node: Node, max_depth: Optional[int] = None) -> List[Node]:
        return self._walk(node, True, max_depth)

    def transitive_dependents(self, node: Node, max_depth: Optional[int] = None) -> List[Node]:
        return self._walk(node, False, max_depth)

    def to_dict(self) -> dict:
        return {
            "nodes": [list(n) for n in sorted(self.nodes)],
            "edges": [[list(s), list(t)] for s, t in sorted(self.edges)],
            "unresolved": [[list(s), t] for s, t in sorted(self.unresolved)],
            "ambiguous": [[list(s), t] for s, t in sorted(self.ambiguous)],
        }

    def sha256(self) -> str:
        blob = json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(blob).hexdigest()


def build_index(repos: Mapping[str, Iterable[Tuple[str, bytes]]], cfg: IndexConfig = IndexConfig()) -> DependencyIndex:
    """repos: repo_id -> iterable of (path, xml bytes). Input order does not affect output."""
    per_repo_defs: Dict[str, Set[str]] = {}
    raw_refs: List[Tuple[str, str, str]] = []
    for repo_id in sorted(repos):
        defs: Set[str] = set()
        count = 0
        for _path, data in sorted(repos[repo_id], key=lambda p: p[0]):
            count += 1
            if count > cfg.max_files_per_repo:
                raise XmlIndexError("repo %s exceeds %d files" % (repo_id, cfg.max_files_per_repo))
            d, r = _extract(parse_xml_safely(data, cfg.max_file_bytes), cfg)
            defs |= d
            raw_refs.extend((repo_id, s, t) for s, t in r)
            if len(defs) > cfg.max_nodes:
                raise XmlIndexError("node limit exceeded")
        per_repo_defs[repo_id] = defs
    idx = DependencyIndex()
    for repo_id, defs in per_repo_defs.items():
        idx.nodes |= {(repo_id, d) for d in defs}
    if len(idx.nodes) > cfg.max_nodes:
        raise XmlIndexError("node limit exceeded")
    for repo_id, src, tgt in raw_refs:
        if not src:
            continue  # reference outside any defining element has no owner node
        s = (repo_id, src)
        if tgt in per_repo_defs[repo_id]:
            idx.edges.add((s, (repo_id, tgt)))
        else:
            others = sorted(r for r, d in per_repo_defs.items() if r != repo_id and tgt in d)
            if len(others) == 1:
                idx.edges.add((s, (others[0], tgt)))
            elif others:
                idx.ambiguous.add((s, tgt))
            else:
                idx.unresolved.add((s, tgt))
        if len(idx.edges) > cfg.max_edges:
            raise XmlIndexError("edge limit exceeded")
    return idx


def read_repo_xml(root: Path, cfg: IndexConfig = IndexConfig()) -> List[Tuple[str, bytes]]:
    """Read-only helper: *.xml under root, skipping symlinks, bounded by cfg."""
    out: List[Tuple[str, bytes]] = []
    for p in sorted(root.rglob("*.xml")):
        if p.is_symlink() or not p.is_file():
            continue
        if len(out) >= cfg.max_files_per_repo:
            raise XmlIndexError("too many xml files under %s" % root)
        if p.stat().st_size > cfg.max_file_bytes:
            raise XmlIndexError("%s exceeds size limit" % p.name)
        out.append((p.relative_to(root).as_posix(), p.read_bytes()))
    return out


# ---- Catalog Validator adapter (plan only; never executes) ----
_DENY = re.compile(r"(deploy|push|publish|write|delete|remove|apply|--force|--fix)", re.IGNORECASE)


@dataclass(frozen=True)
class PlannedLensCommand:
    argv: Tuple[str, ...]
    cwd: str
    read_only: bool = True


class LensAdapter(Protocol):
    def plan(self, repo_root: Path, args: Sequence[str]) -> PlannedLensCommand: ...


@dataclass(frozen=True)
class AllowlistedLensAdapter:
    executable: str  # absolute path supplied by the operator (UNVERIFIED: real Lens command unknown)

    def plan(self, repo_root: Path, args: Sequence[str]) -> PlannedLensCommand:
        if not Path(self.executable).is_absolute():
            raise XmlIndexError("lens executable must be an absolute path")
        for a in args:
            if not isinstance(a, str) or "\x00" in a or "\n" in a:
                raise XmlIndexError("invalid lens argument")
            if _DENY.search(a):
                raise XmlIndexError("lens argument not read-only: %r" % a)
        return PlannedLensCommand(argv=(self.executable, *args), cwd=str(repo_root))
