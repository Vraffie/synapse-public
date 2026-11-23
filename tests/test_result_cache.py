"""Exact result cache lookup/record logic against a storage port (M5.4 cache 2/3, ADR 017 D6)."""
from contextlib import nullcontext
from dataclasses import replace
import hashlib
import json
import random
import threading
import time

import pytest

from synapse.application import cache_key as ck
from synapse.application import result_cache as rc
from synapse.application.cache_key import CacheKeyInputs, derive_cache_key
from synapse.application.context_manifest import ManifestEntry, build_manifest
from synapse.application.result_cache import (
    ArtifactFacts, CacheDecision, CacheEntry, CacheOutcome, CacheResult, CacheStore, InMemoryCacheStore, MissReason,
    RecordStatus, entry_digest, lookup, record,
)

H = lambda s: hashlib.sha256(s.encode()).hexdigest()  # noqa: E731
HEAD = "a" * 40
ARTIFACT = H("result bytes")


def manifest(head=HEAD, clean=True, spec="spec", task="t1"):
    return build_manifest(task, repo_head=head, worktree_clean=clean,
                          entries=[ManifestEntry("spec", task, H(spec)), ManifestEntry("artifact", "p1", H("prompt"), 6)])


def request(**over) -> CacheKeyInputs:
    base = CacheKeyInputs(
        task_class="read_only_analysis", operator_marked_cacheable=True, approval_gated=False,
        policy_version="policy-1", approval_policy_sha256=H("approval-policy-1"),
        provider="claude", profile_id="default", cli_version="2.1.0", model="model-a",
        allowed_tools=["Read", "Grep"], permission_mode="plan", settings={"max_turns": 3},
        spec_sha256=H("spec"), manifest=manifest(), validator_sha256s=[H("pytest")])
    return replace(base, **over)


def result(**over) -> CacheResult:
    base = CacheResult(origin_attempt_id="att-1", outcome="succeeded", budget_blocked=False,
                       result_sha256=ARTIFACT, result_size=12, created_marker="seq:1")
    return replace(base, **over)


def seeded(**over) -> InMemoryCacheStore:
    store = InMemoryCacheStore()
    assert rec(store, request(**over), result()).status is RecordStatus.STORED
    return store


def key_of(**over) -> str:
    return derive_cache_key(request(**over)).sha256


def rec(store, req, res):
    """record() with the key the request derives now, as a lookup before the attempt would give."""
    key = derive_cache_key(req)
    return record(store, req, res, expected_key_sha256=getattr(key, "sha256", "0" * 64))


def assert_never_authority(decision: CacheDecision) -> None:
    assert decision.counts_as_verification is False
    assert decision.counts_as_review is False
    assert decision.satisfies_approval is False
    assert decision.counts_as_mandatory_check is False
    assert decision.requires_verification is True
    if decision.hit is not None:
        assert decision.hit.counts_as_verification is False and decision.hit.satisfies_approval is False


# --- basic flow -------------------------------------------------------------------------------

def test_store_satisfies_port():
    assert isinstance(InMemoryCacheStore(), CacheStore)


def test_empty_store_misses_then_record_then_hit_returns_reference_only():
    store = InMemoryCacheStore()
    miss = lookup(store, request())
    assert miss.outcome is CacheOutcome.MISS and miss.reason == MissReason.NOT_FOUND and miss.hit is None
    assert rec(store, request(), result()).status is RecordStatus.STORED
    hit = lookup(store, request())
    assert hit.is_hit and hit.reason is None
    assert hit.key_sha256 == key_of()
    assert (hit.result_sha256, hit.result_size) == (ARTIFACT, 12)
    assert hit.hit.origin_attempt_id == "att-1" and hit.hit.key_sha256 == hit.key_sha256
    assert hit.artifact_hash_checked is False  # no verifier given
    assert_never_authority(hit)


def test_unchanged_inputs_hit_repeatedly_and_with_fresh_equal_objects():
    store = seeded()
    for _ in range(3):
        assert lookup(store, request()).is_hit
    assert lookup(store, request(settings={"max_turns": 3}, manifest=manifest())).is_hit


def test_shuffled_set_order_is_the_same_key_and_hits():
    store = seeded(allowed_tools=["Read", "Grep", "Glob"], validator_sha256s=[H("a"), H("b"), H("c")])
    rng = random.Random(1234)
    for _ in range(20):
        tools = ["Read", "Grep", "Glob", "Grep"]
        validators = [H("a"), H("b"), H("c"), H("a")]
        rng.shuffle(tools)
        rng.shuffle(validators)
        assert lookup(store, request(allowed_tools=tools, validator_sha256s=validators)).is_hit


def test_settings_key_order_does_not_matter():
    store = seeded(settings={"a": 1, "b": {"x": [1, 2], "y": None}})
    assert lookup(store, request(settings={"b": {"y": None, "x": [1, 2]}, "a": 1})).is_hit


# --- invalidation: one test per key component ---------------------------------------------------

def test_changed_head_misses():
    store = seeded()
    assert lookup(store, request(manifest=manifest(head="b" * 40))).reason == MissReason.NOT_FOUND


def test_changed_spec_hash_misses():
    store = seeded()
    assert lookup(store, request(spec_sha256=H("spec2"))).reason == MissReason.NOT_FOUND


def test_changed_tool_list_misses():
    store = seeded()
    assert lookup(store, request(allowed_tools=["Read"])).reason == MissReason.NOT_FOUND
    assert lookup(store, request(allowed_tools=["Read", "Grep", "Glob"])).reason == MissReason.NOT_FOUND


def test_changed_policy_version_misses():
    store = seeded()
    assert lookup(store, request(policy_version="policy-2")).reason == MissReason.NOT_FOUND


def test_changed_approval_policy_misses():
    store = seeded()
    assert lookup(store, request(approval_policy_sha256=H("approval-policy-2"))).reason == MissReason.NOT_FOUND


def test_changed_tool_version_misses():
    store = seeded()
    assert lookup(store, request(cli_version="2.1.1")).reason == MissReason.NOT_FOUND


def test_changed_config_settings_misses():
    store = seeded()
    assert lookup(store, request(settings={"max_turns": 4})).reason == MissReason.NOT_FOUND
    assert lookup(store, request(settings={"max_turns": 3, "extra": True})).reason == MissReason.NOT_FOUND


def test_changed_manifest_content_misses():
    store = seeded()
    assert lookup(store, request(manifest=manifest(spec="other spec"))).reason == MissReason.NOT_FOUND
    assert lookup(store, request(manifest=manifest(task="t2"))).reason == MissReason.NOT_FOUND


def test_changed_provider_misses():
    assert lookup(seeded(), request(provider="codex")).reason == MissReason.NOT_FOUND


def test_changed_profile_misses():
    assert lookup(seeded(), request(profile_id="other")).reason == MissReason.NOT_FOUND


def test_changed_model_misses_and_unknown_model_is_distinct():
    store = seeded()
    assert lookup(store, request(model="model-b")).reason == MissReason.NOT_FOUND
    assert lookup(store, request(model=None)).reason == MissReason.NOT_FOUND


def test_changed_permission_mode_misses():
    assert lookup(seeded(), request(permission_mode="dontAsk")).reason == MissReason.NOT_FOUND


def test_changed_task_class_misses(monkeypatch):
    # read_only_analysis is the only cacheable class today; a second approved class must key apart
    store = seeded()
    monkeypatch.setattr(ck, "CACHEABLE_CLASSES", frozenset({"read_only_analysis", "other_analysis"}))
    assert lookup(store, request(task_class="other_analysis")).reason == MissReason.NOT_FOUND
    assert lookup(store, request()).is_hit


def test_changed_validators_misses():
    store = seeded()
    assert lookup(store, request(validator_sha256s=[])).reason == MissReason.NOT_FOUND
    assert lookup(store, request(validator_sha256s=[H("ruff")])).reason == MissReason.NOT_FOUND


def test_cache_schema_version_bump_misses(monkeypatch):
    store = seeded()
    monkeypatch.setattr(ck, "CACHE_SCHEMA_VERSION", 2)
    assert lookup(store, request()).reason == MissReason.NOT_FOUND


def test_case_or_whitespace_change_misses_no_semantic_reuse():
    store = seeded()
    assert lookup(store, request(policy_version="Policy-1")).reason == MissReason.NOT_FOUND
    assert lookup(store, request(cli_version="2.1.0 ")).reason == MissReason.NOT_FOUND


def test_invalidated_entry_misses_and_invalidation_is_one_way():
    store = seeded()
    key = key_of()
    assert store.invalidate(key, "op:1") is True
    assert store.invalidate(key, "op:2") is False  # never rewritten
    assert store.get(key).invalidated_marker == "op:1"
    assert lookup(store, request()).reason == MissReason.INVALIDATED
    # re-recording does not resurrect it: first writer wins
    assert rec(store, request(), result(origin_attempt_id="att-2")).status is RecordStatus.ALREADY_PRESENT
    assert lookup(store, request()).reason == MissReason.INVALIDATED


# --- not cacheable: typed bypass, never stored, never served -----------------------------------

@pytest.mark.parametrize("change,reason", [
    ({"task_class": "edit"}, "edit_task"),
    ({"task_class": "review"}, "review_task"),
    ({"task_class": "verification"}, "verification_task"),
    ({"task_class": "validation"}, "validation_task"),
    ({"task_class": "independent_check"}, "independent_check_task"),
    ({"task_class": "mystery"}, "unknown_task_class"),
    ({"operator_marked_cacheable": False}, "not_marked_cacheable"),
    ({"task_class": "validation", "operator_marked_cacheable": True}, "validation_task"),
    ({"approval_gated": True}, "approval_gated"),
    ({"approval_gated": None}, "approval_gated"),
    ({"allowed_tools": ["Read", "Edit"]}, "write_capable_tool"),
    ({"allowed_tools": ["Bash(ls)"]}, "bash_tool"),
    ({"allowed_tools": ["Read", "Frobnicate"]}, "unknown_tool"),
    ({"permission_mode": "acceptEdits"}, "write_capable_mode"),
    ({"permission_mode": "weird"}, "unknown_mode"),
    ({"manifest": manifest(head=None)}, "unknown_head"),
    ({"manifest": manifest(clean=None)}, "unknown_cleanliness"),
    ({"manifest": manifest(clean=False)}, "dirty_worktree"),
    ({"cli_version": None}, "unknown_tool_version"),
    ({"settings": {"api_token": "x"}}, "credential_in_settings"),
])
def test_not_cacheable_requests_bypass_on_lookup_and_are_refused_on_record(change, reason):
    store = InMemoryCacheStore()
    out = rec(store, request(**change), result())
    assert out.status is RecordStatus.REFUSED and out.reason == reason
    assert len(store) == 0
    decision = lookup(store, request(**change))
    assert decision.outcome is CacheOutcome.BYPASS and decision.reason == reason and decision.hit is None
    assert_never_authority(decision)


def test_write_capable_task_never_hits_even_if_an_entry_exists_under_its_would_be_key():
    store = seeded()
    for change in ({"allowed_tools": ["Read", "Grep", "Write"]}, {"permission_mode": "bypassPermissions"},
                   {"task_class": "edit"}, {"approval_gated": True}):
        decision = lookup(store, request(**change))
        assert decision.outcome is CacheOutcome.BYPASS and not decision.is_hit


@pytest.mark.parametrize("outcome,blocked,reason", [
    ("failed", False, "outcome_failed"),
    ("unknown", False, "outcome_unknown"),
    ("cancelled", False, "outcome_cancelled"),
    ("timed_out", False, "outcome_timed_out"),
    ("weird", False, "outcome_unrecognised"),
    ("succeeded", True, "budget_blocked"),
])
def test_only_succeeded_unblocked_results_are_recorded(outcome, blocked, reason):
    store = InMemoryCacheStore()
    out = rec(store, request(), result(outcome=outcome, budget_blocked=blocked))
    assert out.status is RecordStatus.REFUSED and out.reason == reason and len(store) == 0


@pytest.mark.parametrize("change,reason", [
    ({"origin_attempt_id": ""}, "invalid_result:origin_attempt_id"),
    ({"origin_attempt_id": "a\x00b"}, "invalid_result:origin_attempt_id"),
    ({"origin_attempt_id": "x" * 10_000}, "invalid_result:origin_attempt_id"),
    ({"origin_attempt_id": 7}, "invalid_result:origin_attempt_id"),
    ({"outcome": None}, "invalid_result:outcome"),
    ({"budget_blocked": 0}, "invalid_result:budget_blocked"),
    ({"result_sha256": "ABC"}, "invalid_result:result_sha256"),
    ({"result_sha256": ARTIFACT.upper()}, "invalid_result:result_sha256"),
    ({"result_size": -1}, "invalid_result:result_size"),
    ({"result_size": True}, "invalid_result:result_size"),
    ({"result_size": 1.5}, "invalid_result:result_size"),
    ({"result_size": rc.MAX_RESULT_BYTES + 1}, "result_too_large"),
    ({"created_marker": "\n"}, "invalid_result:created_marker"),
])
def test_invalid_results_are_refused_with_typed_reasons(change, reason):
    store = InMemoryCacheStore()
    out = rec(store, request(), result(**change))
    assert out.status is RecordStatus.REFUSED and out.reason == reason and len(store) == 0


def test_result_at_size_bound_is_recorded():
    store = InMemoryCacheStore()
    assert rec(store, request(), result(result_size=rc.MAX_RESULT_BYTES)).status is RecordStatus.STORED


def test_non_result_object_refused():
    assert rec(InMemoryCacheStore(), request(), object()).reason == "invalid_result"
    assert rec(InMemoryCacheStore(), request(), None).reason == "invalid_result"


# --- first writer wins --------------------------------------------------------------------------

def test_first_writer_wins_and_never_overwrites():
    store = InMemoryCacheStore()
    assert rec(store, request(), result()).status is RecordStatus.STORED
    second = rec(store, request(), result(origin_attempt_id="att-2", result_sha256=H("other")))
    assert second.status is RecordStatus.ALREADY_PRESENT and second.reason == "first_writer_wins"
    hit = lookup(store, request())
    assert hit.hit.origin_attempt_id == "att-1" and hit.result_sha256 == ARTIFACT
    assert len(store) == 1


def test_in_memory_put_is_first_writer_wins_directly():
    store = InMemoryCacheStore()
    a = CacheEntry("k", ARTIFACT, 1, "a1", "m", "{}", H("d1"))
    b = CacheEntry("k", H("b"), 2, "a2", "m", "{}", H("d2"))
    assert store.put("k", a) is True and store.put("k", b) is False
    assert store.get("k") is a


def test_concurrent_puts_have_exactly_one_winner_and_a_stable_result():
    for round_ in range(20):
        store = InMemoryCacheStore(race_hook=lambda: time.sleep(0.0005))
        n = 16
        barrier = threading.Barrier(n)
        outcomes: dict[int, RecordStatus] = {}

        def worker(i: int) -> None:
            barrier.wait()
            outcomes[i] = rec(store, request(), result(origin_attempt_id=f"att-{i}",
                                                          result_sha256=H(f"r{i}"))).status

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        winners = [i for i, s in outcomes.items() if s is RecordStatus.STORED]
        assert len(winners) == 1, (round_, outcomes)
        assert sum(s is RecordStatus.ALREADY_PRESENT for s in outcomes.values()) == n - 1
        hit = lookup(store, request())
        assert hit.hit.origin_attempt_id == f"att-{winners[0]}" and hit.result_sha256 == H(f"r{winners[0]}")
        assert all(lookup(store, request()).hit.origin_attempt_id == f"att-{winners[0]}" for _ in range(5))


def racing_puts(store, n=16):
    barrier = threading.Barrier(n)
    results: list[bool] = []

    def worker(i: int) -> None:
        barrier.wait()
        results.append(store.put("k", CacheEntry("k", H(f"r{i}"), 1, f"a{i}", "m", "{}", H(f"d{i}"))))

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return results


def test_race_harness_detects_a_lockless_store():
    # mutation check: with the lock removed the same harness sees several "winners"
    lockless = InMemoryCacheStore(race_hook=lambda: time.sleep(0.005))
    lockless._lock = nullcontext()
    assert sum(racing_puts(lockless)) > 1
    locked = InMemoryCacheStore(race_hook=lambda: time.sleep(0.005))
    assert sum(racing_puts(locked)) == 1


def test_sequential_order_decides_the_winner_deterministically():
    for order in ([0, 1, 2], [2, 0, 1], [1, 2, 0]):
        store = InMemoryCacheStore()
        for i in order:
            rec(store, request(), result(origin_attempt_id=f"att-{i}"))
        assert lookup(store, request()).hit.origin_attempt_id == f"att-{order[0]}"


# --- store failures fail to a miss --------------------------------------------------------------

class RaisingStore:
    def __init__(self, exc: BaseException):
        self.exc = exc

    def get(self, key_sha256):
        raise self.exc

    def put(self, key_sha256, entry):
        raise self.exc


class FixedStore:
    """Returns whatever it was given from get; put returns a configured value."""

    def __init__(self, entry=None, put_result=True):
        self.entry = entry
        self.put_result = put_result

    def get(self, key_sha256):
        return self.entry

    def put(self, key_sha256, entry):
        return self.put_result


@pytest.mark.parametrize("exc", [RuntimeError("disk"), OSError("io"), ValueError("x"), RecursionError(), MemoryError()])
def test_store_raising_is_a_miss_or_store_error_never_raised(exc):
    store = RaisingStore(exc)
    decision = lookup(store, request())
    assert decision.outcome is CacheOutcome.MISS and decision.reason == MissReason.STORE_ERROR
    out = rec(store, request(), result())
    assert out.status is RecordStatus.STORE_ERROR and out.reason == MissReason.STORE_ERROR


def test_non_store_object_is_a_miss():
    assert lookup(object(), request()).reason == MissReason.STORE_ERROR
    assert rec(object(), request(), result()).status is RecordStatus.STORE_ERROR


@pytest.mark.parametrize("put_result", [None, 1, "yes", 0])
def test_put_returning_non_bool_is_a_store_error(put_result):
    out = rec(FixedStore(put_result=put_result), request(), result())
    assert out.status is RecordStatus.STORE_ERROR


def good_entry(**over) -> CacheEntry:
    key = derive_cache_key(request())
    entry = CacheEntry(key.sha256, ARTIFACT, 12, "att-1", "seq:1",
                       json.dumps(key.material, sort_keys=True, separators=(",", ":")),
                       entry_digest(key_sha256=key.sha256, result_sha256=ARTIFACT, result_size=12,
                                    origin_attempt_id="att-1", created_marker="seq:1"))
    return replace(entry, **over)


def test_good_entry_from_a_foreign_store_hits():
    assert lookup(FixedStore(good_entry()), request()).is_hit


def tampered_material(**changes) -> str:
    material = dict(derive_cache_key(request()).material)
    material.update(changes)
    return json.dumps(material, sort_keys=True)


@pytest.mark.parametrize("entry,reason", [
    ("not an entry", MissReason.MALFORMED_ENTRY),
    ({"key_sha256": "x"}, MissReason.MALFORMED_ENTRY),
    (12, MissReason.MALFORMED_ENTRY),
    (good_entry(key_sha256=H("other key")), MissReason.KEY_MISMATCH),
    (good_entry(key_sha256=None), MissReason.KEY_MISMATCH),
    (good_entry(entry_schema_version=2), MissReason.MALFORMED_ENTRY),
    (good_entry(entry_schema_version=True), MissReason.MALFORMED_ENTRY),
    (good_entry(result_sha256="nothex"), MissReason.MALFORMED_ENTRY),
    (good_entry(result_sha256=None), MissReason.MALFORMED_ENTRY),
    (good_entry(result_size=-1), MissReason.MALFORMED_ENTRY),
    (good_entry(result_size="12"), MissReason.MALFORMED_ENTRY),
    (good_entry(result_size=rc.MAX_RESULT_BYTES + 1), MissReason.ENTRY_TOO_LARGE),
    (good_entry(origin_attempt_id=""), MissReason.MALFORMED_ENTRY),
    (good_entry(origin_attempt_id="a\x1b[31m"), MissReason.MALFORMED_ENTRY),
    (good_entry(created_marker=None), MissReason.MALFORMED_ENTRY),
    (good_entry(key_material_json=None), MissReason.MALFORMED_ENTRY),
    (good_entry(key_material_json="{not json"), MissReason.MALFORMED_ENTRY),
    (good_entry(key_material_json="[]"), MissReason.MALFORMED_ENTRY),
    (good_entry(key_material_json="[" * 5000 + "]" * 5000), MissReason.MALFORMED_ENTRY),
    (good_entry(key_material_json=" " * (rc.MAX_MATERIAL_BYTES + 1)), MissReason.ENTRY_TOO_LARGE),
    (good_entry(key_material_json='{"a":1}'), MissReason.MALFORMED_ENTRY),
    (good_entry(key_material_json=tampered_material(policy_version="policy-0")), MissReason.MATERIAL_MISMATCH),
    (good_entry(key_material_json=tampered_material(worktree_clean=False)), MissReason.MATERIAL_MISMATCH),
    (good_entry(invalidated_marker="op:1"), MissReason.INVALIDATED),
    (good_entry(entry_digest="nothex"), MissReason.MALFORMED_ENTRY),
    (good_entry(entry_digest=H("other")), MissReason.ENTRY_DIGEST_MISMATCH),
    (good_entry(result_sha256=H("another blob")), MissReason.ENTRY_DIGEST_MISMATCH),
    (good_entry(result_size=13), MissReason.ENTRY_DIGEST_MISMATCH),
    (good_entry(origin_attempt_id="att-evil"), MissReason.ENTRY_DIGEST_MISMATCH),
    (good_entry(created_marker="seq:2"), MissReason.ENTRY_DIGEST_MISMATCH),
])
def test_malformed_or_tampered_entries_are_misses(entry, reason):
    decision = lookup(FixedStore(entry), request())
    assert decision.outcome is CacheOutcome.MISS and decision.reason == reason and decision.hit is None
    assert decision.result_sha256 is None


def test_entry_tampered_after_storing_is_a_miss():
    store = seeded()
    key = key_of()
    original = store.get(key)
    store._entries[key] = replace(original, key_material_json=tampered_material(spec_sha256=H("x")))
    assert lookup(store, request()).reason == MissReason.MATERIAL_MISMATCH


def test_material_json_that_hashes_to_key_but_differs_from_request_cannot_occur_silently():
    # a material whose own hash is the key yet whose content differs would need a SHA-256 collision;
    # we check both properties so a store that rewrites key_sha256 to match tampered material still misses
    material = json.loads(tampered_material(provider="codex"))
    forged_key = ck.canonical_request_hash(material)
    entry = good_entry(key_sha256=forged_key, key_material_json=json.dumps(material))
    assert lookup(FixedStore(entry), request()).reason == MissReason.KEY_MISMATCH


def test_rewritten_result_reference_is_not_served_even_with_an_agreeing_verifier():
    # reviewer repro: point the entry at another blob and attempt; a verifier for that blob agrees
    store = seeded()
    key = key_of()
    other = H("another blob")
    store._entries[key] = replace(store.get(key), result_sha256=other, origin_attempt_id="att-evil")
    decision = lookup(store, request(), verifier=lambda sha, size: ArtifactFacts(sha, size))
    assert decision.outcome is CacheOutcome.MISS and decision.reason == MissReason.ENTRY_DIGEST_MISMATCH


class EvilEntry(CacheEntry):
    __slots__ = ()

    def _boom(self):
        raise RuntimeError("boom")

    result_sha256 = property(_boom, lambda self, value: None)  # type: ignore[assignment]


def test_entry_whose_attributes_raise_is_a_miss():
    base = good_entry()
    evil = EvilEntry(base.key_sha256, ARTIFACT, 12, "att-1", "seq:1", base.key_material_json, base.entry_digest)
    assert lookup(FixedStore(evil), request()).reason == MissReason.MALFORMED_ENTRY


class ShiftyEntry(CacheEntry):
    """Returns the recorded hash for the first reads (validation), then a different one (TOCTOU)."""

    __slots__ = ("_reads",)

    def _get(self):
        reads = object.__getattribute__(self, "_reads")
        reads[0] += 1
        return ARTIFACT if reads[0] <= 4 else H("swapped")

    result_sha256 = property(_get, lambda self, value: None)  # type: ignore[assignment]


def test_subclass_with_shifting_property_is_never_served():
    base = good_entry()
    shifty = ShiftyEntry(base.key_sha256, ARTIFACT, 12, "att-1", "seq:1", base.key_material_json, base.entry_digest)
    object.__setattr__(shifty, "_reads", [0])
    seen = []
    decision = lookup(FixedStore(shifty), request(), verifier=lambda sha, size: seen.append(sha) or ArtifactFacts(sha))
    assert decision.outcome is CacheOutcome.MISS and decision.reason == MissReason.MALFORMED_ENTRY
    assert seen == [] and decision.result_sha256 is None


def test_verifier_and_decision_get_the_same_values_that_were_validated():
    seen = []
    decision = lookup(seeded(), request(), verifier=lambda sha, size: seen.append((sha, size)) or ArtifactFacts(sha, size))
    assert seen == [(ARTIFACT, 12)] and (decision.result_sha256, decision.result_size) == (ARTIFACT, 12)


# --- verifier -----------------------------------------------------------------------------------

def test_verifier_confirming_hash_marks_hit_checked():
    seen = []
    decision = lookup(seeded(), request(), verifier=lambda ref, size: seen.append(ref) or ArtifactFacts(ARTIFACT, 12))
    assert decision.is_hit and decision.artifact_hash_checked is True and seen == [ARTIFACT]
    assert lookup(seeded(), request(), verifier=lambda ref, size: ArtifactFacts(ARTIFACT)).is_hit  # size unknown
    assert_never_authority(decision)


@pytest.mark.parametrize("verifier,reason", [
    (lambda ref, size: ArtifactFacts(H("different bytes"), size), MissReason.CORRUPT_ENTRY),
    (lambda ref, size: ArtifactFacts(ref.upper(), size), MissReason.CORRUPT_ENTRY),
    (lambda ref, size: ArtifactFacts(42, size), MissReason.CORRUPT_ENTRY),
    (lambda ref, size: ArtifactFacts(ref, size + 1), MissReason.SIZE_MISMATCH),
    (lambda ref, size: ArtifactFacts(ref, "12"), MissReason.SIZE_MISMATCH),
    (lambda ref, size: ArtifactFacts(ref, True), MissReason.SIZE_MISMATCH),
    (lambda ref, size: ref, MissReason.VERIFIER_ERROR),  # a bare hash is not ArtifactFacts
    (lambda ref, size: 42, MissReason.VERIFIER_ERROR),
    (lambda ref, size: None, MissReason.ARTIFACT_MISSING),
    (lambda ref, size: (_ for _ in ()).throw(OSError("blob gone")), MissReason.VERIFIER_ERROR),
    (lambda ref: ArtifactFacts(ref), MissReason.VERIFIER_ERROR),  # wrong arity raises TypeError
])
def test_verifier_disagreement_is_a_typed_miss(verifier, reason):
    decision = lookup(seeded(), request(), verifier=verifier)
    assert decision.outcome is CacheOutcome.MISS and decision.reason == reason and decision.result_sha256 is None


def test_verifier_not_called_for_bypass_or_not_found():
    calls = []
    lookup(InMemoryCacheStore(), request(), verifier=lambda *a: calls.append(a))
    lookup(seeded(), request(task_class="edit"), verifier=lambda *a: calls.append(a))
    assert calls == []


# --- invariant 10: a hit is never a check -------------------------------------------------------

def test_hit_flags_are_class_constants_and_cannot_be_flipped_on_an_instance():
    decision = lookup(seeded(), request())
    for name in ("counts_as_verification", "counts_as_review", "satisfies_approval", "counts_as_mandatory_check"):
        assert getattr(CacheDecision, name) is False
        with pytest.raises((AttributeError, TypeError)):
            setattr(decision, name, True)
        with pytest.raises((AttributeError, TypeError)):
            object.__setattr__(decision, name, True)
        assert getattr(decision, name) is False
    with pytest.raises(TypeError):
        CacheDecision(CacheOutcome.HIT, counts_as_verification=True)  # not a constructor field


def test_no_code_path_assigns_the_authority_flags():
    import ast
    import inspect
    tree = ast.parse(inspect.getsource(rc))
    flags = {"counts_as_verification", "counts_as_review", "satisfies_approval", "counts_as_mandatory_check",
             "requires_verification"}
    for node in ast.walk(tree):
        if isinstance(node, ast.keyword):
            assert node.arg not in flags
        if isinstance(node, (ast.Attribute,)) and node.attr in flags:
            assert isinstance(node.ctx, ast.Load)
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            assert node.value not in flags  # no setattr-by-name either
    # the only definitions are the ClassVar annotations, each False except requires_verification
    defs = {n.target.id: n.value.value for n in ast.walk(tree)
            if isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name) and n.target.id in flags}
    assert defs == {"counts_as_verification": False, "counts_as_review": False, "satisfies_approval": False,
                    "counts_as_mandatory_check": False, "requires_verification": True}


def test_every_decision_on_every_path_is_never_authority():
    decisions = [
        lookup(InMemoryCacheStore(), request()),
        lookup(seeded(), request()),
        lookup(seeded(), request(), verifier=lambda r, n: ArtifactFacts(r, n)),
        lookup(seeded(), request(), verifier=lambda r, n: None),
        lookup(seeded(), request(task_class="review")),
        lookup(RaisingStore(RuntimeError()), request()),
        lookup(FixedStore("junk"), request()),
        lookup(seeded(), "not a request"),
    ]
    for d in decisions:
        assert_never_authority(d)


# --- hostile requests ---------------------------------------------------------------------------

def cyclic():
    a: dict = {}
    a["self"] = a
    return a


def cyclic_list():
    a: list = []
    a.append(a)
    return {"x": a}


@pytest.mark.parametrize("req,reason", [
    ("not a request", "invalid_input:inputs"),
    (None, "invalid_input:inputs"),
    (42, "invalid_input:inputs"),
    (request(task_class=123), "invalid_input:task_class"),
    (request(task_class="x" * 1_000_000), "invalid_input:task_class"),
    (request(task_class="valid\x00ation"), "invalid_input:task_class"),
    (request(policy_version=None), "invalid_input:policy_version"),
    (request(policy_version="p" * 257), "invalid_input:policy_version"),
    (request(policy_version="p\nq"), "invalid_input:policy_version"),
    (request(provider=b"claude"), "invalid_input:provider"),
    (request(allowed_tools="Read"), "invalid_input:allowed_tools"),
    (request(allowed_tools=["Read"] * 65), "invalid_input:allowed_tools"),
    (request(allowed_tools=[None]), "invalid_input:allowed_tools"),
    (request(spec_sha256="z" * 64), "invalid_input:spec_sha256"),
    (request(settings=cyclic()), "invalid_input:settings"),
    (request(settings=cyclic_list()), "invalid_input:settings"),
    (request(settings={1: "x"}), "invalid_input:settings"),
    (request(settings={"x": float("nan")}), "invalid_input:settings"),
    (request(settings={f"k{i}": i for i in range(100_000)}), "invalid_input:settings_nodes"),
    (request(settings={"x": "y" * (ck.MAX_SETTINGS_BYTES + 1)}), "invalid_input:settings"),
    (request(manifest="not a manifest"), "invalid_input:manifest"),
    (request(validator_sha256s="abc"), "invalid_input:validator_sha256s"),
])
def test_hostile_requests_return_typed_refusals_and_never_raise(req, reason):
    store = seeded()
    decision = lookup(store, req)
    assert decision.outcome is CacheOutcome.BYPASS and decision.reason == reason
    out = rec(store, req, result())
    assert out.status is RecordStatus.REFUSED and out.reason == reason
    assert len(store) == 1


def test_material_never_contains_settings_values():
    store = InMemoryCacheStore()
    rec(store, request(settings={"note": "SECRET-VALUE-123"}), result())
    (entry,) = store._entries.values()
    assert "SECRET-VALUE-123" not in entry.entry_json()


def test_stored_entry_document_is_bounded_and_round_trips():
    store = seeded()
    entry = store.get(key_of())
    encoded = entry.entry_json()
    doc = json.loads(encoded)
    assert len(encoded.encode()) <= rc.MAX_ENTRY_BYTES
    assert set(doc) == {"entry_schema_version", "key_sha256", "result_sha256", "result_size",
                        "origin_attempt_id", "created_marker", "entry_digest", "key_material"}
    assert ck.canonical_request_hash(doc["key_material"]) == doc["key_sha256"]
    assert doc["entry_digest"] == entry_digest(key_sha256=doc["key_sha256"], result_sha256=doc["result_sha256"],
                                               result_size=doc["result_size"],
                                               origin_attempt_id=doc["origin_attempt_id"],
                                               created_marker=doc["created_marker"])


def test_worst_case_entry_fits_the_entry_bound():
    # material at its cap, longest text fields of non-BMP characters (12 bytes each once ASCII-escaped)
    wide = "\U0001F600" * rc.MAX_TEXT
    filler = "x" * (rc.MAX_MATERIAL_BYTES - len('{"a":""}'))
    material = json.dumps({"a": filler}, separators=(",", ":"))
    assert len(material.encode()) == rc.MAX_MATERIAL_BYTES
    entry = CacheEntry(H("k"), H("r"), rc.MAX_RESULT_BYTES, wide, wide, material, H("d"), 10 ** 6)
    assert len(entry.entry_json().encode()) <= rc.MAX_ENTRY_BYTES


def test_material_over_its_cap_is_refused_and_never_stored():
    wide = "\U0001F600" * ck.MAX_TEXT
    req = request(policy_version=wide, provider=wide, profile_id=wide, cli_version=wide, model=wide,
                  validator_sha256s=[H(str(i)) for i in range(ck.MAX_VALIDATORS)])
    store = InMemoryCacheStore()
    out = rec(store, req, result())
    assert out.status is RecordStatus.REFUSED and out.reason == "entry_too_large" and len(store) == 0


def test_long_but_bounded_entry_is_stored_and_hits():
    wide = "\U0001F600" * rc.MAX_TEXT
    store = InMemoryCacheStore()
    out = rec(store, request(), result(origin_attempt_id=wide, created_marker=wide, result_size=rc.MAX_RESULT_BYTES))
    assert out.status is RecordStatus.STORED
    assert len(store.get(key_of()).entry_json().encode()) <= rc.MAX_ENTRY_BYTES
    assert lookup(store, request()).hit.origin_attempt_id == wide


# --- record is tied to the looked-up key ---------------------------------------------------------

def test_record_refuses_when_the_request_moved_since_lookup():
    store = InMemoryCacheStore()
    looked_up = lookup(store, request(manifest=manifest(head="a" * 40)))
    assert looked_up.reason == MissReason.NOT_FOUND
    out = record(store, request(manifest=manifest(head="b" * 40)), result(),
                 expected_key_sha256=looked_up.key_sha256)
    assert out.status is RecordStatus.REFUSED and out.reason == "key_changed" and len(store) == 0


def test_record_with_the_looked_up_key_stores():
    store = InMemoryCacheStore()
    looked_up = lookup(store, request())
    out = record(store, request(), result(), expected_key_sha256=looked_up.key_sha256)
    assert out.status is RecordStatus.STORED and out.key_sha256 == looked_up.key_sha256


@pytest.mark.parametrize("bad", ["", "XYZ", "A" * 64, 42, b"a" * 64])
def test_record_refuses_a_malformed_expected_key(bad):
    out = record(InMemoryCacheStore(), request(), result(), expected_key_sha256=bad)
    assert out.status is RecordStatus.REFUSED and out.reason == "invalid_input:expected_key_sha256"


# --- purity -------------------------------------------------------------------------------------

def test_module_is_pure_no_storage_or_io():
    import ast
    import inspect
    source = inspect.getsource(rc)
    tree = ast.parse(source)
    imported = {(n.module or "") for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
    imported |= {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    assert not imported & {"sqlite3", "os", "pathlib", "subprocess", "socket", "shutil", "io", "time",
                           "datetime", "random", "asyncio"}
    assert not any(m.startswith(("synapse.daemon", "synapse.api", "synapse.providers", "synapse.desktop",
                                 "synapse.infrastructure", "synapse.orchestration")) for m in imported)
    assert "open(" not in source


def test_record_requires_the_expected_key():
    with pytest.raises(TypeError):
        record(InMemoryCacheStore(), request(), result())
