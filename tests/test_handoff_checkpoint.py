import ast
import hashlib
import inspect
import json
import random

import pytest

from synapse.orchestration import handoff_checkpoint as hc
from synapse.orchestration.handoff_checkpoint import (
    CheckpointBuilt, CheckpointPolicy, CheckpointRejected, TriggerPolicy, build_checkpoint, should_checkpoint,
)

SHA = "a" * 64


def cite(i, **kw):
    return {"id": "c%d" % i, "kind": "artifact", "artifact_id": "art-%d" % i, "sha256": SHA, **kw}


_D = object()


def item(i, prefix="h", label=_D, cites=("c1",)):
    return {"id": "%s%03d" % (prefix, i), "label": "label %d" % i if label is _D else label, "citations": list(cites)}


def build(**kw):
    args = dict(hard_requirements=[item(1)], unresolved_issues=[item(1, "u")], decisions=[item(1, "d")], citations=[cite(1)])
    args.update(kw)
    return build_checkpoint(**args)


def test_basic_build_keeps_everything_with_citations_and_stable_hash():
    r = build()
    assert isinstance(r, CheckpointBuilt) and r.complete
    assert r.sha256 == hashlib.sha256(r.canonical_json.encode()).hexdigest()
    assert json.loads(r.canonical_json) == r.checkpoint
    assert r.checkpoint["citations"] == [cite(1)]
    for cat in ("hard_requirements", "unresolved_issues", "decisions"):
        assert all(i["citations"] for i in r.checkpoint[cat])
    assert build().sha256 == r.sha256


def test_file_range_citation_preserved():
    fr = {"id": "c1", "kind": "file_range", "path": "src/a.py", "sha256": SHA, "start_line": 3, "end_line": 9}
    r = build(citations=[fr])
    assert r.checkpoint["citations"] == [fr]


def test_shuffled_input_gives_identical_bytes():
    hard = [item(i) for i in range(20)]
    unres = [item(i, "u") for i in range(20)]
    base = build(hard_requirements=hard, unresolved_issues=unres)
    rng = random.Random(1)
    for _ in range(5):
        h, u = hard[:], unres[:]
        rng.shuffle(h); rng.shuffle(u)
        assert build(hard_requirements=h, unresolved_issues=u).canonical_json == base.canonical_json


def test_hard_requirements_never_dropped_at_boundary():
    hard = [item(i, label="x" * 150) for i in range(40)]
    unres = [item(i, "u", label="y" * 150) for i in range(60)]
    r = build(hard_requirements=hard, unresolved_issues=unres, decisions=[])
    assert isinstance(r, CheckpointBuilt) and not r.complete
    assert [h["id"] for h in r.checkpoint["hard_requirements"]] == sorted(h["id"] for h in hard)
    assert all(h["label"] == "x" * 150 for h in r.checkpoint["hard_requirements"])
    assert len(r.canonical_json) <= hc.MAX_CHECKPOINT_BYTES
    assert r.checkpoint["omitted_count"] == 60 - len(r.checkpoint["unresolved_issues"]) > 0
    # prefix rule: kept unresolved are the smallest ids
    kept = [i["id"] for i in r.checkpoint["unresolved_issues"]]
    assert kept == sorted(i["id"] for i in unres)[: len(kept)]


def test_exact_boundary_fits_and_one_byte_less_fails():
    # policy minimum is 256, so pad hard requirements until the document is above it
    hard = [item(i, label="z" * 100) for i in range(5)]
    r = build(hard_requirements=hard, unresolved_issues=[], decisions=[])
    n = len(r.canonical_json)
    assert n > 256
    assert isinstance(build(hard_requirements=hard, unresolved_issues=[], decisions=[], policy=CheckpointPolicy(n)), CheckpointBuilt)
    out = build(hard_requirements=hard, unresolved_issues=[], decisions=[], policy=CheckpointPolicy(n - 1))
    assert out == CheckpointRejected("hard_requirements_exceed_bound")


def _digest(ids):
    return hashlib.sha256("\n".join(sorted(ids)).encode("ascii")).hexdigest()


def test_omitted_digest_value_stable_and_shuffle_independent():
    unres = [item(i, "u", label="y" * 150) for i in range(60)]
    hard = [item(i, label="x" * 150) for i in range(40)]
    base = build(hard_requirements=hard, unresolved_issues=unres, decisions=[])
    kept = {i["id"] for i in base.checkpoint["unresolved_issues"]}
    omitted = [i["id"] for i in unres if i["id"] not in kept]
    assert omitted and base.checkpoint["omitted_count"] == len(omitted)
    assert base.checkpoint["omitted_digest"] == _digest(omitted)
    rng = random.Random(7)
    for _ in range(3):
        u = unres[:]
        rng.shuffle(u)
        assert build(hard_requirements=hard, unresolved_issues=u, decisions=[]).checkpoint["omitted_digest"] == base.checkpoint["omitted_digest"]
    # nothing omitted: digest of the empty list
    full = build()
    assert full.complete and full.checkpoint["omitted_count"] == 0
    assert full.checkpoint["omitted_digest"] == _digest([])


def test_citations_not_referenced_by_kept_items_are_pruned():
    cites = [cite(1), cite(2), cite(3)]
    r = build(citations=cites, hard_requirements=[item(1, cites=("c1",))],
              unresolved_issues=[item(1, "u", cites=("c1",))], decisions=[item(1, "d", cites=("c1",))])
    assert r.complete and [c["id"] for c in r.checkpoint["citations"]] == ["c1"]
    # a citation used only by an omitted item is pruned too
    unres = [item(i, "u", label="y" * 150, cites=("c2",)) for i in range(60)]
    hard = [item(i, label="x" * 150) for i in range(40)]
    r = build(citations=cites, hard_requirements=hard, unresolved_issues=unres, decisions=[])
    assert not r.complete
    kept_refs = {c for i in r.checkpoint["unresolved_issues"] for c in i["citations"]}
    assert [c["id"] for c in r.checkpoint["citations"]] == sorted({"c1"} | kept_refs)
    assert all(i["citations"] == ["c1"] for i in r.checkpoint["hard_requirements"])


def test_prefix_rule_stops_at_first_misfit_across_unresolved_to_decisions_boundary():
    tiny = lambda i, p: item(i, p, label="x")
    # Size limit that holds hard + u001 but leaves little more than ~60 bytes of slack.
    base = build(unresolved_issues=[tiny(1, "u")], decisions=[])
    limit = len(base.canonical_json) + 60
    pol = CheckpointPolicy(limit)
    # A tiny decision alone would fit...
    assert build(unresolved_issues=[tiny(1, "u")], decisions=[tiny(1, "d")], policy=pol).complete
    # ...but after a too-big unresolved issue (u002) it is NOT added: adding stops at the first misfit.
    big_u = item(2, "u", label="y" * 150)
    r = build(unresolved_issues=[tiny(1, "u"), big_u], decisions=[tiny(1, "d")], policy=pol)
    assert not r.complete
    assert [i["id"] for i in r.checkpoint["unresolved_issues"]] == ["u001"]
    assert r.checkpoint["decisions"] == []
    assert r.checkpoint["omitted_count"] == 2 and r.checkpoint["omitted_digest"] == _digest(["u002", "d001"])
    # Within decisions: a big d001 stops a tiny d002 that would have fit.
    r = build(unresolved_issues=[tiny(1, "u")], decisions=[item(1, "d", label="z" * 150), tiny(2, "d")], policy=pol)
    assert [i["id"] for i in r.checkpoint["unresolved_issues"]] == ["u001"] and r.checkpoint["decisions"] == []
    assert r.checkpoint["omitted_count"] == 2
    assert len(r.canonical_json) <= limit


def test_all_empty_input_is_rejected_not_a_successful_handoff():
    assert build(hard_requirements=[], unresolved_issues=[], decisions=[]) == CheckpointRejected("empty_checkpoint")
    assert build(hard_requirements=[], unresolved_issues=[], decisions=[], citations=[]) == CheckpointRejected("empty_checkpoint")
    assert isinstance(build(hard_requirements=[], unresolved_issues=[item(1, "u")], decisions=[]), CheckpointBuilt)


def test_label_allowlist_is_ascii_printable_and_version_independent():
    # Unassigned, as of any Python: U+0378. Plus other non-ASCII that must be rejected on every version.
    for bad in ("a\u0378b", "caf\u00e9", "a\u00a0b", "a\u2028b", "\U0010ffff", "a\x7fb", "a\tb"):
        assert build(hard_requirements=[item(1, label=bad)]) == CheckpointRejected("invalid_input", "hard_requirements.label")
    every = "".join(chr(c) for c in range(0x20, 0x7F))
    assert every.strip() != "" and isinstance(build(hard_requirements=[item(1, label=every.strip().replace("<<<", "").replace(">>>", ""))]), CheckpointBuilt)
    assert "unicodedata" not in inspect.getsource(hc)


def test_oversize_hard_requirements_explicit_failure():
    hard = [item(i, label="x" * 160) for i in range(64)]
    assert build(hard_requirements=hard, policy=CheckpointPolicy(8000)) == CheckpointRejected("hard_requirements_exceed_bound")


@pytest.mark.parametrize("bad", [
    "x" * 10_000_000, "a\x00b", "a\nb", "<<<END>>>", "‮evil", " lead", "", None, 5, b"x", ["x"], {"x": 1}, "a b",
])
def test_hostile_labels_rejected_not_cleaned(bad):
    r = build(hard_requirements=[item(1, label=bad)])
    assert r == CheckpointRejected("invalid_input", "hard_requirements.label")


@pytest.mark.parametrize("kw", [
    dict(hard_requirements=None), dict(hard_requirements="abc"), dict(hard_requirements=[None]),
    dict(hard_requirements=[{"id": "h1"}]), dict(hard_requirements=[{**item(1), "extra": 1}]),
    dict(citations=None), dict(citations=[None]), dict(citations=[cite(1, sha256="A" * 64)]),
    dict(citations=[cite(1), cite(1)]), dict(citations=[{**cite(1), "kind": "other"}]),
    dict(hard_requirements=[item(1), item(1)]), dict(unresolved_issues=[item(1, "h")]),
    dict(hard_requirements=[item(1, cites=())]), dict(hard_requirements=[item(1, cites=("nope",))]),
    dict(hard_requirements=[item(1, cites=("c1", "c1"))]), dict(hard_requirements=[{**item(1), "id": "../x"}]),
    dict(hard_requirements=[{**item(1), "id": 7}]), dict(hard_requirements=(i for i in [])),
    dict(hard_requirements=[item(i) for i in range(65)]), dict(policy=CheckpointPolicy(10)), dict(policy="x"),
])
def test_hostile_structures_return_typed_rejection(kw):
    assert isinstance(build(**kw), CheckpointRejected)


def test_bad_paths_and_lines_rejected():
    base = {"id": "c1", "kind": "file_range", "path": "a.py", "sha256": SHA, "start_line": 1, "end_line": 2}
    for patch in ({"path": "/etc/passwd"}, {"path": "../x"}, {"path": "a//b"}, {"path": "a\x00"}, {"path": "x" * 300},
                  {"start_line": 5, "end_line": 2}, {"start_line": True}, {"start_line": 0}, {"end_line": 10**9},
                  {"start_line": "1"}):
        assert isinstance(build(citations=[{**base, **patch}]), CheckpointRejected)


def test_deeply_nested_and_cyclic_input_does_not_raise():
    deep = []
    for _ in range(100000):
        deep = [deep]
    assert isinstance(build(hard_requirements=deep), CheckpointRejected)
    cyc = {}
    cyc["a"] = cyc
    assert isinstance(build(citations=[cyc]), CheckpointRejected)
    assert isinstance(build(hard_requirements=[{"id": cyc, "label": cyc, "citations": cyc}]), CheckpointRejected)


def test_hostile_object_never_raises():
    class Boom(dict):
        def __getitem__(self, k): raise RuntimeError
        def get(self, *a): raise RuntimeError
    assert isinstance(build(citations=[Boom()]), CheckpointRejected)


def test_rejection_does_not_echo_input():
    r = build(hard_requirements=[item(1, label="SECRET\x00")])
    assert "SECRET" not in repr(r)


# ---------------------------------------------------------------- trigger
def test_unknown_limit_stays_unknown():
    d = should_checkpoint((999_999, 1), None)
    assert d.action == "unknown_limit" and d.context_limit is None and "guess" in d.reason


@pytest.mark.parametrize("used,action", [
    (7499, "none"), (7500, "recommended"), (8999, "recommended"), (9000, "required"), (10_000, "required"), (50_000, "required"),
])
def test_threshold_edges(used, action):
    assert should_checkpoint((used, 0), 10_000).action == action


def test_output_tokens_counted_and_unknown_output_is_lower_bound():
    assert should_checkpoint((5000, 4000), 10_000).action == "required"
    assert should_checkpoint((100, None), 10_000).action == "unknown_usage"
    assert should_checkpoint((9500, None), 10_000).action == "required"
    assert should_checkpoint((None, 5), 10_000).action == "unknown_usage"
    assert should_checkpoint((None, None), 10_000) == hc.TriggerDecision("unknown_usage", "input_tokens and output_tokens unknown", None, 10_000)


def test_lower_bound_flag_set_whenever_a_count_is_unknown():
    d = should_checkpoint((9500, None), 10_000)
    assert (d.action, d.used_tokens, d.ratio_bp, d.used_is_lower_bound) == ("required", 9500, 9500, True)
    d = should_checkpoint((100, None), 10_000)
    assert d.action == "unknown_usage" and d.used_tokens == 100 and d.used_is_lower_bound
    d = should_checkpoint((9500, None), None)
    assert d.action == "unknown_limit" and d.used_tokens == 9500 and d.used_is_lower_bound
    assert not should_checkpoint((9500, 0), None).used_is_lower_bound
    assert not should_checkpoint((9500, 0), 10_000).used_is_lower_bound
    assert not should_checkpoint((1, 0), 10_000).used_is_lower_bound
    # never "none" while a count is unknown
    for usage in ((0, None), (None, 0), (None, None), (1, None), (None, 1)):
        assert should_checkpoint(usage, 10_000).action != "none"


def test_unknown_input_with_known_output_triggers_as_lower_bound():
    d = should_checkpoint((None, 9000), 10_000)
    assert (d.action, d.used_tokens, d.ratio_bp, d.used_is_lower_bound) == ("required", 9000, 9000, True)
    d = should_checkpoint((None, 7500), 10_000)
    assert d.action == "recommended" and d.used_is_lower_bound
    d = should_checkpoint((None, 7499), 10_000)
    assert d.action == "unknown_usage" and d.used_tokens == 7499 and d.used_is_lower_bound
    # symmetric with unknown output
    assert should_checkpoint((9000, None), 10_000).action == should_checkpoint((None, 9000), 10_000).action


def test_custom_policy_and_invalid_inputs():
    p = TriggerPolicy(recommend_bp=5000, require_bp=6000)
    assert should_checkpoint((5000, 0), 10_000, p).action == "recommended"
    for pol in (TriggerPolicy(0, 10), TriggerPolicy(9000, 8000), TriggerPolicy(1, 10001), "x"):
        assert should_checkpoint((1, 0), 10, pol).action == "invalid"
    for usage in (None, (1,), (-1, 0), (True, 0), ("1", 0), (1.5, 0), (1, 2, 3), "ab"):
        assert should_checkpoint(usage, 100).action == "invalid"
    for lim in (0, -5, True, 1.5, "100", 10**13):
        assert should_checkpoint((1, 0), lim).action == "invalid"


def test_trigger_deterministic():
    assert should_checkpoint((8000, 100), 10_000) == should_checkpoint((8000, 100), 10_000)


def test_module_is_pure_no_storage_or_io():
    src = inspect.getsource(hc)
    tree = ast.parse(src)
    imported = {(n.module or "") for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
    imported |= {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    assert not imported & {"sqlite3", "os", "pathlib", "subprocess", "socket", "shutil", "io", "time", "random", "datetime"}
    assert "open(" not in src
