"""Deterministic output reducer (M5.3): guarantees, golden corpus of REAL captured tool output, bounds."""
import hashlib
import json
from pathlib import Path
import random
import subprocess
import sys
import time

import pytest

from synapse.application.output_reducer import (
    MAX_FAIL_BLOCKS, MAX_LINE, reduce_output,
)

FIXTURES = Path(__file__).parent / "fixtures" / "output_reducer"


def fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()  # bytes: the progress-bar fixture holds raw carriage returns


def lines(text: str) -> list[str]:
    return text.split("\n") if text else []


# --------------------------------------------------------------------------- validation and shape
def test_arguments_are_validated():
    for bad in ("", "PYTEST", None):
        with pytest.raises(ValueError):
            reduce_output("x", kind=bad)
    for bad in (63, 0, -1, True, 100.0, "100", None):
        with pytest.raises(ValueError):
            reduce_output("x", max_bytes=bad)
    for bad in (None, 5, ["a"], memoryview(b"a")):
        with pytest.raises(TypeError):
            reduce_output(bad)


def test_empty_and_trivial_input():
    r = reduce_output("")
    assert (r.reduced, r.raw_bytes, r.reduced_bytes, r.dropped_lines, r.truncated, r.over_budget) == ("", 0, 0, 0, False, False)
    assert r.raw_sha256 == hashlib.sha256(b"").hexdigest() and r.kept_failures == [] and r.rules_applied == []
    assert reduce_output("hello\n").reduced == "hello"


def test_sha256_and_sizes_describe_the_raw_input_for_str_and_bytes():
    raw = "héllo\nERROR boom\n".encode()
    for data in (raw, bytearray(raw), raw.decode()):
        r = reduce_output(data)
        assert r.raw_sha256 == hashlib.sha256(raw).hexdigest() and r.raw_bytes == len(raw)
        assert r.reduced_bytes == len(r.reduced.encode())


# --------------------------------------------------------------------------- normalisation and collapsing
def test_line_endings_ansi_control_chars_and_trailing_space_are_normalised():
    r = reduce_output("one \r\ntwo\t\r\n\x1b[31mred\x1b[0m\nbel\x07l\x01\n")
    assert r.reduced == "one\ntwo\nred\nbell"
    assert {"ansi", "control_chars"} <= set(r.rules_applied)


def test_progress_bar_carriage_returns_keep_only_the_final_frame():
    r = reduce_output(fixture("progress_carriage_returns.txt"))
    assert r.reduced == "starting\nDownloading [####################] 100%\nfinished ok"
    assert "carriage_return" in r.rules_applied and r.raw_bytes > 3000 and r.reduced_bytes < 100


def test_identical_runs_collapse_with_a_count_only_when_that_is_smaller():
    r = reduce_output("tick\n" * 50 + "done")
    assert r.reduced == "tick (x50)\ndone" and "collapse_repeats" in r.rules_applied and r.dropped_lines == 49
    assert reduce_output("a\na\nb").reduced == "a\na\nb"  # "a (x2)" would not be smaller
    assert reduce_output("x\n\n\n\n\ny").reduced == "x\n\ny" and "blank_runs" in reduce_output("x\n\n\n\n\ny").rules_applied


def test_similar_lines_collapse_to_first_marker_last_but_failures_never_do():
    r = reduce_output("\n".join(f"step {i}/100 ok" for i in range(1, 101)))
    assert r.reduced == "step 1/100 ok\n[... 98 similar lines]\nstep 100/100 ok"
    assert "collapse_similar" in r.rules_applied and r.dropped_lines == 98
    errs = "\n".join(f"ERROR job {i} failed" for i in range(1, 41))
    out = reduce_output(errs).reduced
    assert all(f"ERROR job {i} failed" in out for i in range(1, 41))  # digit-different failures stay distinct


def test_very_long_lines_are_cut_and_marked():
    r = reduce_output("A" * 10_000 + "\nok")
    first = r.reduced.split("\n")[0]
    assert first.startswith("A" * MAX_LINE) and first.endswith("…[+5904 chars]") and "long_line" in r.rules_applied


def test_invalid_utf8_is_replaced_and_binary_input_is_summarised():
    r = reduce_output(b"good\n\xff\xfe bad\nERROR x\n")
    assert "�" in r.reduced and "ERROR x" in r.reduced and "invalid_utf8" in r.rules_applied
    blob = bytes(range(256)) * 40
    b = reduce_output(blob, max_bytes=500)
    assert b.rules_applied == ["binary_summary"] and b.truncated and b.raw_sha256 == hashlib.sha256(blob).hexdigest()
    assert str(len(blob)) in b.reduced and b.reduced_bytes < 200
    assert reduce_output(b"text\x00more").rules_applied == ["binary_summary"]


# --------------------------------------------------------------------------- golden corpus (real tool output)
def test_pytest_failures_survive_a_tight_budget():
    raw = fixture("pytest_plain.txt")
    full = reduce_output(raw, kind="pytest", max_bytes=100_000)
    assert full.truncated is False and lines(full.reduced) == [l.rstrip() for l in raw.decode().rstrip("\n").split("\n")]
    r = reduce_output(raw, kind="pytest", max_bytes=1500)
    assert r.reduced_bytes <= 1500 and r.over_budget is False and r.truncated is True
    for must in ("FAILED test_demo.py::test_assert_diff", "FAILED test_demo.py::test_zero_division - ZeroDivisionError: division by zero",
                 "FAILED test_demo.py::test_raises_keyerror - KeyError: 'missing-key'",
                 "E       ZeroDivisionError: division by zero", "E       KeyError: 'missing-key'",
                 "test_demo.py:16: ZeroDivisionError", "= 3 failed, 40 passed in 0.02s ="):
        assert must in r.reduced, must
    assert r.kept_failures and all(f in raw.decode() for f in r.kept_failures)
    assert "[... " in r.reduced and r.dropped_lines > 0


def test_pytest_ansi_colour_output_reduces_to_the_same_text_as_plain():
    plain = reduce_output(fixture("pytest_plain.txt"), kind="pytest", max_bytes=1500)
    colour = reduce_output(fixture("pytest_color.txt"), kind="pytest", max_bytes=1500)
    assert b"\x1b" in fixture("pytest_color.txt") and "\x1b" not in colour.reduced and "ansi" in colour.rules_applied
    assert colour.truncated == plain.truncated and not colour.over_budget
    for must in ("FAILED test_demo.py::test_zero_division - ZeroDivisionError: division by zero",
                 "E       KeyError: 'missing-key'", "test_demo.py:16: ZeroDivisionError"):
        assert must in colour.reduced and must in plain.reduced
    # pytest itself abbreviates the first summary line differently when colouring; everything else is identical
    drop = lambda text: [l for l in lines(text) if "test_assert_diff - AssertionError" not in l and "assert {'a': 1" not in l
                and " passed in " not in l]
    assert drop(colour.reduced) == drop(plain.reduced)


def test_chained_python_traceback_keeps_both_exceptions_and_the_link_line():
    raw = fixture("python_traceback_chained.txt")
    r = reduce_output(raw, max_bytes=330)
    assert r.reduced_bytes <= 330 or r.over_budget
    for must in ("Traceback (most recent call last):", "KeyError: 'k'", "RuntimeError: config failed",
                 "The above exception was the direct cause of the following exception:",
                 '  File "<string>", line 2, in load'):
        assert must in r.reduced, must
    assert reduce_output(raw, max_bytes=10_000).reduced == raw.decode().rstrip("\n")


def test_python_syntax_error_and_git_output():
    syn = reduce_output(fixture("python_syntax_error.txt"), max_bytes=10_000)
    assert "SyntaxError" in syn.reduced and "SyntaxError" in " ".join(syn.kept_failures)
    merge = fixture("git_merge_conflict.txt")
    r = reduce_output(merge, kind="git", max_bytes=200)
    for must in ("CONFLICT (content): Merge conflict in f.txt", "<<<<<<< HEAD", "=======", ">>>>>>> side"):
        assert must in r.reduced, must
    fatal = reduce_output(fixture("git_fatal.txt"), kind="git", max_bytes=10_000)
    assert any(f.startswith("fatal:") for f in fatal.kept_failures)


def test_compiler_diagnostics_keep_their_source_context():
    raw = fixture("gcc_errors.txt")
    r = reduce_output(raw, kind="compiler", max_bytes=520)
    assert r.over_budget is False and r.reduced_bytes <= 520
    for must in ("bad.c:3:11: error: initialization of 'int'", "bad.c:4:3: error: implicit declaration of function 'undefined_fn'",
                 "bad.c:5:10: error: 'y' undeclared", "    4 |   undefined_fn(x);", "      |   ^~~~~~~~~~~~"):
        assert must in r.reduced, must


# --------------------------------------------------------------------------- budget behaviour
WORDS = ["alpha", "beta", "gamma", "delta", "kappa", "sigma", "omega", "theta", "lambda", "zeta", "iota", "nu", "mu"]


def noise(n: int, start: int = 0) -> list[str]:
    """Distinct, non-collapsible lines (the letters differ between neighbours, not only the digits)."""
    return [f"{WORDS[(start + i) % 7]} {WORDS[((start + i) // 7) % 11]} job {start + i} {WORDS[(start + i) * 5 % 13]}" for i in range(n)]


def test_budget_keeps_head_tail_and_failures_and_marks_gaps():
    body = noise(3000) + ["ERROR disk full on /dev/sda1"] + noise(3000, 3000)
    r = reduce_output("\n".join(body), max_bytes=1200)
    out = lines(r.reduced)
    assert r.reduced_bytes <= 1200 and r.truncated and not r.over_budget and "window" in r.rules_applied
    assert "ERROR disk full on /dev/sda1" in out
    assert out[-1] == body[-1]                      # the very last line is always kept first
    assert body[0] in out                           # then the head, while the budget allows
    assert any(l.startswith("[... ") and l.endswith(" lines omitted ...]") for l in out)
    assert r.dropped_lines == len(body) - sum(1 for l in out if not l.startswith("[... "))


def test_everything_is_kept_when_it_fits_even_with_many_lines():
    body = noise(300)
    r = reduce_output("\n".join(body), max_bytes=1_000_000)
    assert lines(r.reduced) == body and not r.truncated and r.dropped_lines == 0 and r.rules_applied == []


def test_failures_beyond_the_budget_are_kept_and_reported_over_budget():
    errs = [f"ERROR worker {i} crashed with code {i * 13}" for i in range(100)]
    r = reduce_output("\n".join(errs), max_bytes=200)
    assert all(e in r.reduced for e in errs) and r.over_budget is True and r.truncated is False
    assert r.reduced_bytes > 200


def test_repeated_identical_failure_blocks_are_kept_once_with_a_repeat_note():
    tb = ["Traceback (most recent call last):", '  File "x.py", line 3, in f', "    boom()", "ValueError: bad input"]
    body = []
    for i in range(30):
        body += noise(200, i * 200) + tb
    body += noise(200, 99_000)  # so the last block is not inside the tail window
    r = reduce_output("\n".join(body), max_bytes=900)
    assert r.reduced.count("ValueError: bad input") == 1 and "failure above repeated 29x" in r.reduced
    assert "dedupe_failure_blocks" in r.rules_applied and r.reduced_bytes <= 900


def test_failure_storage_is_capped_and_says_so():
    body = []
    for i in range(MAX_FAIL_BLOCKS + 500):
        body += [f"ERROR unique failure number {i}", "plain noise line to separate the blocks"]
    r = reduce_output("\n".join(body), max_bytes=4096)
    assert r.truncated and "failure_cap" in r.rules_applied
    assert r.kept_failures[0] == "ERROR unique failure number 0" and len(r.kept_failures) <= 200


# --------------------------------------------------------------------------- json
def test_json_is_compacted_pruned_and_keeps_failure_keys():
    data = {"status": "failed", "error": "E" * 3000, "items": [{"i": i, "name": "n" * 40} for i in range(2000)]}
    raw = json.dumps(data, indent=2)
    small = reduce_output(json.dumps({"a": [1, 2], "b": "x"}, indent=2), kind="json", max_bytes=1000)
    assert small.reduced == '{"a":[1,2],"b":"x"}' and small.rules_applied == ["json_compact"] and not small.truncated
    r = reduce_output(raw, kind="json", max_bytes=2000)
    parsed = json.loads(r.reduced)  # the reduced form is still valid JSON
    assert r.truncated and "json_prune" in r.rules_applied and r.reduced_bytes <= 2000 + 4096
    assert parsed["error"] == "E" * 3000                   # failure-keyed strings are not shortened
    assert any(isinstance(x, str) and "items omitted" in x for x in parsed["items"])
    assert r.kept_failures == ["E" * 300]


def test_invalid_or_pathological_json_falls_back_to_text_reduction():
    r = reduce_output("{not json\nERROR inside\n", kind="json")
    assert "json_invalid" in r.rules_applied and "ERROR inside" in r.reduced
    deep = reduce_output("[" * 200_000, kind="json", max_bytes=500)
    assert "json_invalid" in deep.rules_applied and deep.reduced_bytes < 5000


# --------------------------------------------------------------------------- determinism / properties
VOCAB = ["alpha beta", "gamma delta 12", "compiling module", "ok", "", "   indented text", "step done", "x" * 70,
         "reading file", "tick", "tock", "100% done"]
FAIL_STYLES = ("ERROR job {n} aborted", "FAILED check_{n} - assertion", "fatal: bad object {n}", "panic: index {n} out of range")


def random_case(rng: random.Random):
    parts, failures = [], []
    for k in range(rng.randint(0, 40)):
        parts += [rng.choice(VOCAB) for _ in range(rng.randint(0, 60))]
        if rng.random() < 0.7:
            line = rng.choice(FAIL_STYLES).format(n=f"{k}-{rng.randint(0, 10**6)}")
            parts.append(line)
            failures.append(line)
            if rng.random() < 0.3:
                parts += ["Traceback (most recent call last):", '  File "m.py", line 1, in f', f"KeyError: 'k{k}'"]
                failures += ["Traceback (most recent call last):", f"KeyError: 'k{k}'"]
    parts += [rng.choice(VOCAB) for _ in range(rng.randint(0, 30))]
    return "\n".join(parts) + rng.choice(["", "\n"]), sorted(set(failures)), rng.randint(64, 6000)


def test_properties_over_random_inputs():
    rng = random.Random(20261120)
    for _ in range(300):
        text, failures, max_bytes = random_case(rng)
        r = reduce_output(text, kind=rng.choice(["generic", "pytest", "compiler", "git"]), max_bytes=max_bytes)
        assert r.raw_sha256 == hashlib.sha256(text.encode()).hexdigest() and r.reduced_bytes == len(r.reduced.encode())
        for line in failures:                                   # every distinct failure line survives
            assert line in r.reduced, (line, max_bytes)
        if not r.over_budget:
            assert r.reduced_bytes <= max_bytes
            assert r.reduced_bytes <= r.raw_bytes


def test_reduction_is_deterministic_and_idempotent_for_every_kind():
    rng = random.Random(7)
    for kind in ("generic", "pytest", "compiler", "git"):
        for _ in range(60):
            text, _, max_bytes = random_case(rng)
            a = reduce_output(text, kind=kind, max_bytes=max_bytes)
            assert a == reduce_output(text, kind=kind, max_bytes=max_bytes)
            if not a.over_budget:
                b = reduce_output(a.reduced, kind=kind, max_bytes=max_bytes)
                assert b.reduced == a.reduced, (kind, max_bytes)


def test_the_last_lines_survive_whenever_the_budget_allows():
    rng = random.Random(3)
    for _ in range(100):
        body = noise(rng.randint(60, 400))
        r = reduce_output("\n".join(body), max_bytes=rng.randint(600, 3000))
        assert lines(r.reduced)[-1] == body[-1]
        assert r.truncated == ("[... " in r.reduced) and (r.truncated or lines(r.reduced) == body)
        if r.truncated:
            assert lines(r.reduced)[-5:] == body[-5:]  # the budget is always large enough for the last lines here


# --------------------------------------------------------------------------- bounded time and memory
CHILD = """
import sys, time, tracemalloc
from synapse.application.output_reducer import reduce_output
data = open(sys.argv[1], "rb").read()
tracemalloc.start()
base, _ = tracemalloc.get_traced_memory()   # the 20 MB input itself was allocated before tracing began
t = time.monotonic()
r = reduce_output(data, max_bytes=8192)
elapsed = time.monotonic() - t
_, peak = tracemalloc.get_traced_memory()
print(elapsed, (peak - base) / 1048576, len(data) >> 20, r.reduced_bytes, r.truncated,
      int("ERROR batch 123456 failed" in r.reduced), int("ERROR batch 250000 failed" in r.reduced))
"""


def test_twenty_megabytes_reduce_quickly_and_in_bounded_memory(tmp_path):
    path = tmp_path / "big.log"
    w = WORDS
    size = i = 0
    with open(path, "w") as f:
        while size < 20 * 1024 * 1024:
            line = "%s %s worker %d %s batch %d\n" % (w[i % 7], w[(i // 7) % 11], i, w[(i * 5) % 13], i % 977)
            if i in (123456, 250000):
                line = "ERROR batch %d failed: connection reset\n" % i
            f.write(line)
            size += len(line)
            i += 1
    done = subprocess.run([sys.executable, "-c", CHILD, str(path)], capture_output=True, text=True, timeout=300)
    assert done.returncode == 0, done.stderr
    elapsed, extra_mib, size_mib, reduced, truncated, e1, e2 = done.stdout.split()
    assert int(size_mib) >= 20 and int(reduced) <= 8192 and truncated == "True" and (e1, e2) == ("1", "1")
    assert float(elapsed) < 120, elapsed  # tracemalloc slows the child several-fold
    # one decoded copy of the input (~20) plus bounded windows; not proportional to the number of lines
    assert float(extra_mib) < 45, extra_mib


@pytest.mark.parametrize("name,data", [
    ("one_5mb_line", "A" * 5_000_000),
    ("word_soup", ("Error" * 1000 + " ") * 3000),
    ("ansi_csi_bomb", "\x1b[" * 1_000_000),
    ("ansi_osc_unterminated", "\x1b]0;" + "x" * 3_000_000),
    ("pytest_header_underscores", "_" * 3_000_000),
    ("pytest_header_spaces", "_" + " " * 3_000_000 + "_"),
    ("section_equals", "=" * 2_000_000 + " " + "=" * 2_000_000),
    ("compiler_colons", ":" * 2_000_000 + "1: error"),
    ("compiler_long_path", "a/" * 1_000_000 + ":1:1: error: x"),
    ("python_loc_dots", ("a." * 500_000) + "py:1: "),
    ("many_cr", "\r" * 2_000_000 + "end"),
    ("digits", ("1234567890" * 100 + "\n") * 2000),
])
def test_adversarial_inputs_stay_linear(name, data):
    for kind in ("generic", "pytest", "compiler", "git"):
        start = time.monotonic()
        r = reduce_output(data, kind=kind, max_bytes=4096)
        assert time.monotonic() - start < 10, (name, kind)
        assert r.raw_bytes == len(data.encode()) and r.reduced_bytes <= max(4096, r.raw_bytes)
