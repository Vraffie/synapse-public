# Output reducer (M5.3)

`synapse.application.output_reducer.reduce_output(data, *, kind="generic", max_bytes=16384)` makes a bounded,
readable version of one tool or command output. It is **pure and deterministic**: no I/O, no clock, no
randomness, no LLM. It is **not wired into anything yet**; a later change decides where Synapse applies it
(for example to the text that goes into messages and summaries, with the raw output kept as an artifact).

It follows [ARCHITECTURE.md §11](../architecture/ARCHITECTURE.md): "Tool output reducer preserves all unique
failures and raw output". The reducer never stores the raw text. It returns `raw_sha256` (and `raw_bytes`)
so a caller that stored the raw output as an artifact can cite it.

## Result

`ReducedOutput(reduced, raw_sha256, raw_bytes, reduced_bytes, dropped_lines, kept_failures, truncated,
rules_applied, over_budget)`.

| field | meaning |
| --- | --- |
| `reduced` | the reduced text (no trailing newline) |
| `raw_sha256`, `raw_bytes` | digest and size of the exact input (a `str` is hashed as UTF-8) |
| `reduced_bytes` | UTF-8 size of `reduced` |
| `dropped_lines` | input lines not represented as an ordinary line in `reduced` (collapsed or omitted) |
| `kept_failures` | distinct failure lines in order of first appearance (at most 200) |
| `truncated` | lines were omitted to meet `max_bytes` (or a failure cap was hit) |
| `over_budget` | the failure blocks alone exceed `max_bytes`, so `reduced_bytes > max_bytes` |
| `rules_applied` | which rules changed something: `ansi`, `carriage_return`, `control_chars`, `long_line`, `invalid_utf8`, `collapse_repeats`, `blank_runs`, `collapse_similar`, `window`, `dedupe_failure_blocks`, `failure_cap`, `binary_summary`, `json_compact`, `json_prune`, `json_invalid` |

`max_bytes` must be an integer of at least 64.

## Rules (applied in one pass, line by line)

1. **Normalise.** `\r\n` becomes `\n`; a progress bar written with carriage returns keeps only its final
   frame; ANSI/OSC escapes and other control characters (except tab) are removed; trailing spaces are
   trimmed; invalid UTF-8 is replaced (`U+FFFD`); a line longer than 4096 characters is cut and marked
   `…[+N chars]`. Trailing blank lines are dropped.
2. **Collapse.** Consecutive identical lines become `line (xN)` (blank runs become one blank line), and a run
   of at least three *similar* non-failure lines (equal once digits are masked, e.g. `step 4/100 ok`) becomes
   the first line, `[... N similar lines]`, and the last line. A collapse is only used when it is smaller than
   the lines it replaces. Failure lines never join a similar-line run, so `ERROR job 7` and `ERROR job 8` stay
   distinct.
3. **Budget.** If the collapsed text fits in `max_bytes` it is returned whole. Otherwise only the failure
   blocks (below) are mandatory, and the remaining budget goes, in this order, to the last lines (last line
   first), then the first lines (up to 20 each). Gaps are marked `[... N lines omitted ...]`.
4. **Failure blocks.** A failure line plus the lines that belong to it are kept whole: a Python traceback
   (header, indented frames and the exception line), the `Traceback`-chaining sentences, pytest `E` blocks,
   `___ test ___` headers, `file.py:12:` locations, `FAILED`/`ERROR` summary lines and the failures/summary
   section headers, compiler diagnostics with their indented source context, git `fatal:`/`error:`/`CONFLICT`
   lines and conflict markers, and any line with a token such as `Error`, `Exception`, `ERROR`, `FATAL`,
   `FAILED`, `panic` or a word ending in `Error`/`Exception`. A block takes up to 40 continuation lines.
   An identical block that repeats is kept once and followed by `[... failure above repeated Nx ...]`.
5. **`kind`** only adds patterns: `pytest`, `compiler` (gcc/clang/rustc style), `git`. `generic` uses the
   token patterns alone. `json` parses the input, writes it compactly and, if still over budget, shortens long
   arrays (keeps the head, a `[... N items omitted ...]` entry and the last item) and long strings in four
   steps; strings under keys named like `error`, `errors`, `failure`, `failures`, `exception`, `stderr`,
   `message`, `traceback` are kept up to 4096 characters. Invalid or non-JSON input falls back to text
   reduction (`json_invalid`).
6. **Binary input** (a NUL byte, or many control bytes, in the first 8 KiB) is not reduced line by line; the
   result is a one-line summary with the size and SHA-256 (`binary_summary`).

## Guarantees (each has a test)

- Every distinct failure line is in `reduced`, verbatim or as the prefix of `<line> (xN)`.
- `reduced_bytes <= max_bytes`, unless the failure blocks alone are larger (`over_budget`).
- `reduced_bytes <= raw_bytes` whenever the result is not `over_budget`.
- Same input and arguments give the same result; reducing a result that is not `over_budget` again with the
  same arguments returns it unchanged.
- The last line is kept first when the budget allows, then the head.
- Real captured output of pytest (plain and coloured), a chained Python traceback, a syntax error, a git merge
  conflict and `fatal:` errors, and gcc diagnostics keep their failures under a tight budget
  (`tests/fixtures/output_reducer/`).
- Time and memory are bounded: a 20 MB log reduces in about 3 seconds and needs about one decoded copy of
  the input (about 20 MB) extra; adversarial inputs (a 5 MB line, unterminated escapes, huge underscore or
  colon runs, millions of carriage returns) stay linear. No regular expression is applied to an unbounded
  token run.

## Non-guarantees

- It reduces what Synapse **stores or forwards**. It does not change what a provider (Claude, Codex) sees
  inside its own session, and it cannot recover anything it dropped; keep the raw output as an artifact.
- Failure detection is textual. A line that merely contains `Error` is kept, and an unusual failure format
  that matches none of the patterns is treated as ordinary text.
- A failure line longer than 4096 characters is cut. At most 1000 failure blocks and 1 MiB of failure text
  are kept; beyond that `failure_cap` is reported, `truncated` is true, and later failures are not kept.
- `kept_failures` lists at most 200 lines. `dropped_lines` counts lines of the normalised input, so a
  `(xN)` line stands for N input lines and counts as one kept line.
- When `over_budget`, the small marker lines may make the result slightly larger than the input.
- Not applied to the Claude/Codex adapters, artifacts, messages or the desktop. It is not a token estimator and
  makes no savings claim; measuring tokens per verified task is a separate M5 step.
