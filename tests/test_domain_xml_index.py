import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("domain_xml_index", ROOT / "plugins/domain_catalog/integrations/xml_index.py")
x = importlib.util.module_from_spec(_spec)
sys.modules["domain_xml_index"] = x
_flag, sys.dont_write_bytecode = sys.dont_write_bytecode, True  # never write __pycache__ under plugins/
try:
    _spec.loader.exec_module(x)
finally:
    sys.dont_write_bytecode = _flag

A = b'<c><item id="a"><dep ref="b"/></item><item id="b"><dep ref="c"/></item><item id="c"/></c>'
B = b'<c><item id="z"><dep ref="a"/><dep ref="nope"/></item></c>'


def idx():
    return x.build_index({"r1": [("a.xml", A)], "r2": [("z.xml", B)]})


def test_xxe_rejected():
    xxe = b'<?xml version="1.0"?><!DOCTYPE d [<!ENTITY e SYSTEM "file:///etc/passwd">]><d>&e;</d>'
    with pytest.raises(x.XmlIndexError):
        x.parse_xml_safely(xxe)


def test_billion_laughs_rejected():
    bl = b'<!DOCTYPE l [<!ENTITY a "aaaa"><!ENTITY b "&a;&a;&a;&a;">]><l>&b;</l>'
    with pytest.raises(x.XmlIndexError):
        x.parse_xml_safely(bl)


def test_size_depth_and_malformed():
    with pytest.raises(x.XmlIndexError):
        x.parse_xml_safely(b"<a>" + b"x" * 100 + b"</a>", max_bytes=10)
    with pytest.raises(x.XmlIndexError):
        x.parse_xml_safely(b"<a>")
    deep = b"<a>" * 100 + b"</a>" * 100
    with pytest.raises(x.XmlIndexError):
        x.build_index({"r": [("d.xml", deep)]}, x.IndexConfig(max_depth=10))


def test_queries():
    i = idx()
    assert i.dependencies(("r1", "a")) == [("r1", "b")]
    assert i.dependents(("r1", "a")) == [("r2", "z")]
    assert i.transitive_dependencies(("r2", "z")) == [("r1", "a"), ("r1", "b"), ("r1", "c")]
    assert i.transitive_dependencies(("r2", "z"), max_depth=1) == [("r1", "a")]
    assert i.transitive_dependents(("r1", "c")) == [("r1", "a"), ("r1", "b"), ("r2", "z")]
    assert i.unresolved == {(("r2", "z"), "nope")}


def test_cycle_and_ambiguity():
    cyc = b'<c><i id="p"><d ref="q"/></i><i id="q"><d ref="p"/></i></c>'
    i = x.build_index({"r": [("c.xml", cyc)]})
    assert i.transitive_dependencies(("r", "p")) == [("r", "q")]
    dup = b'<c><i id="s"/></c>'
    user = b'<c><i id="u"><d ref="s"/></i></c>'
    i = x.build_index({"a": [("1.xml", dup)], "b": [("1.xml", dup)], "c": [("1.xml", user)]})
    assert i.ambiguous == {(("c", "u"), "s")} and not i.edges


def test_deterministic():
    a = idx()
    b = x.build_index({"r2": [("z.xml", B)], "r1": [("a.xml", A)]})
    assert a.sha256() == b.sha256() and a.to_dict() == b.to_dict()


def test_limits():
    with pytest.raises(x.XmlIndexError):
        x.build_index({"r": [("a.xml", A)]}, x.IndexConfig(max_nodes=1))
    with pytest.raises(x.XmlIndexError):
        x.build_index({"r": [("a.xml", A), ("b.xml", A)]}, x.IndexConfig(max_files_per_repo=1))


def test_read_repo_xml_skips_symlinks(tmp_path):
    (tmp_path / "a.xml").write_bytes(A)
    (tmp_path / "l.xml").symlink_to(tmp_path / "a.xml")
    assert [p for p, _ in x.read_repo_xml(tmp_path)] == ["a.xml"]


def test_lens_plan_is_read_only(tmp_path):
    ad = x.AllowlistedLensAdapter("/opt/lens/lens")
    p = ad.plan(tmp_path, ["validate", "cat.xml"])
    assert p.argv == ("/opt/lens/lens", "validate", "cat.xml") and p.read_only
    for bad in (["deploy"], ["--force"], ["a\nb"]):
        with pytest.raises(x.XmlIndexError):
            ad.plan(tmp_path, bad)
    with pytest.raises(x.XmlIndexError):
        x.AllowlistedLensAdapter("lens").plan(tmp_path, [])


def test_core_does_not_import_index():
    code = "import synapse.daemon, sys; assert not [m for m in sys.modules if 'xml_index' in m]"
    subprocess.run([sys.executable, "-c", code], check=True)
