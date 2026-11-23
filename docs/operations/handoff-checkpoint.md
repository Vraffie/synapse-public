# Handoff checkpoint builder and trigger (M5.6 slice 1/2)

Module: `src/synapse/orchestration/handoff_checkpoint.py`. Pure and deterministic: no I/O, no
sqlite, no subprocess, no clock, no randomness, no LLM.

## What is NOT done

- Not wired into any engine, provider call or run loop; nothing calls it yet.
- No storage: the canonical JSON and hash are shaped so a later slice can store them as an immutable
  artifact, but no artifact, table, event or migration exists.
- No summarising. The caller supplies structured items; the module never rewrites text.
- It does not decide *what* the hard requirements are; it only refuses to lose them.

## Inputs

Items (`hard_requirements`, `unresolved_issues`, `decisions`): `{"id", "label", "citations": [citation ids]}`
with no other keys. Citations: `{"id","kind":"artifact","artifact_id","sha256"}` or
`{"id","kind":"file_range","path","sha256","start_line","end_line"}`. Only `list`/`tuple`/`dict`/`str`/`int`
exact types are accepted.

## Bounds (all enforced, none silently)

| Bound | Value |
| --- | --- |
| items per category | 64 |
| citations in input | 256 |
| citation refs per item | 1 to 8 (at least one is required) |
| id | 64 chars, `[A-Za-z0-9][A-Za-z0-9._:-]*`; unique across all three categories |
| label | 1 to 160 chars, only ASCII printable characters U+0020 to U+007E (explicit allowlist, no Unicode database lookup, so identical on every Python version), no leading/trailing space, no `<<<` or `>>>` |
| path | 256 chars, relative, no `..`, no empty segment, no backslash, same ASCII-printable rule |
| line numbers | 1 to 10,000,000, start <= end |
| sha256 | 64 lowercase hex |
| checkpoint | `MAX_CHECKPOINT_BYTES` = 16384 bytes of canonical JSON (policy may only lower it, minimum 256) |

Any violation rejects the whole input with a typed `CheckpointRejected(code, field)`; nothing is cleaned,
truncated or guessed, and the rejection never echoes input text. Codes: `invalid_input`, `too_many_items`,
`missing_citation`, `invalid_citation_ref`, `invalid_policy`, `malformed_input`,
`empty_checkpoint` (all three categories empty: compacting to nothing is never a successful handoff), and
`hard_requirements_exceed_bound`.

## Rules

1. Hard requirements are always kept whole. If they plus their citations do not fit the byte bound the
   result is `CheckpointRejected("hard_requirements_exceed_bound")`: the caller must not compact.
2. Unresolved issues, then decisions, are added in ascending id order while the document stays within the
   bound; adding stops at the first one that does not fit. The rest are reported as `omitted_count` and
   `omitted_digest` (SHA-256 of the sorted omitted ids joined by newlines; empty string when none are omitted, stable under input order), and `complete` is false.
3. Only citations referenced by kept items are emitted, sorted by id. Every kept item lists its citation ids.
4. Output is canonical JSON (sorted keys, compact, ASCII, no NaN) and its SHA-256. Shuffled input gives identical bytes. Duplicate ids are rejected.
5. Free text is limited to short validated labels and carries `"labels_are_data": true`. Consumers must still
   fence labels (`agents/roles.py` `fence`) if they put them in a prompt.
6. The builder never raises: deep nesting, cycles, huge strings and hostile objects return a rejection.

## Trigger: `should_checkpoint(usage, context_limit, policy)`

`usage = (input_tokens, output_tokens)` (the shape of `UsageObservation`, `None` = unknown) and
`context_limit` is the provider capability (`None` = unknown). Integer arithmetic only. Default
`TriggerPolicy`: recommend at 7500 basis points of the limit, require at 9000 (`>=` on both edges).
Used tokens are input plus output. `TriggerDecision.used_is_lower_bound` is `True` whenever either count is
unknown: `used_tokens` then sums only the known parts and real usage is at least that. It can still trigger
(`required`/`recommended`, including known output alone against the thresholds), and it never yields `none`.

| Condition | Action |
| --- | --- |
| limit is `None` | `unknown_limit` (`used_tokens` is the known sum, flagged if a count was unknown) ("context_limit unknown; no limit is guessed"); no default is substituted |
| both token counts unknown | `unknown_usage` (`used_tokens` is `None`) |
| used >= require threshold | `required` |
| used >= recommend threshold | `recommended` |
| below, but input or output tokens unknown (usage is a lower bound, flagged) | `unknown_usage` |
| below both thresholds with known usage | `none` |
| bad types, negative, bool, limit <= 0, bad policy | `invalid` |

`unknown_limit` and `unknown_usage` are not "no"; the caller decides how to treat them and must not assume a
number.
