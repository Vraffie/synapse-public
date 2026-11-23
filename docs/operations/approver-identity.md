# Local approver identity

> **Security limit (read first).** Against code running as the same OS user as the daemon (including any agent child), approvals are **not** a security boundary: that code can read `approver.token` and `operator.token` and write the SQLite file directly, so it can forge or delete approvals and audit rows. The checks described here catch mistakes and honest components, not a hostile same-user process. See the [security review](../security/review-2026-11-20.md) (F1, F2). The daemon refuses to enable agent runs unless the operator passes `--allow-same-user-agents`.

`LocalApproverAuthority` is the first concrete `ApprovalAuthority`. An approver is identified for a decision by presenting a token held in `<state-dir>/approver.token`, separate from the operator bearer token.

- Provisioning (`provision_approver_token`) writes a temp file, fsyncs and links/renames it into place, so no empty or partial token is ever visible. It refuses to replace an existing token unless `rotate=True`.
- The file must be a private, user-owned regular file with one link. It is re-read on every authentication, so rotating or deleting it revokes access immediately.
- Only `approval.decide` is allowed. Requester separation, binding checks, expiry, idempotency and audit remain in `ApprovalService`.
- Limitation: same-user processes can read the file, so this does not isolate a human from same-user agents: an agent that can read the state directory can self-approve (F2). It is replaceable by stronger proof of presence behind the same interface.
- Not yet wired to a transport. Next: a daemon decision endpoint and CLI/desktop prompt that take the token per decision and never store it.

## Transport (added)

- `agentctl --state-dir DIR approver-init [--rotate]` creates `approver.token` (mode 0600, atomic) and prints it once. It refuses to replace an existing token without `--rotate`, which revokes the old token immediately.
- `POST /api/v1/approvals/{approval_id}/decision` requires the operator bearer, `Idempotency-Key`, `X-Correlation-ID` and a single `X-Approver-Token`. Body: `decision` (approved|rejected), `expected_version`, and the exact `operation`, `target`, `candidate_sha256`, `policy_version` being approved, plus optional `reason`. The operator token alone gets 401; a wrong approver token or a requester deciding their own request gets 403; binding mismatch, version conflict, expiry or idempotency conflict gets 409. Responses never echo the token.
- `agentctl approval-decide APPROVAL_ID --decision ... ` reads the token from `SYNAPSE_APPROVER_TOKEN` or a hidden prompt, never from argv.
- Not yet done: wiring workflow requests to create approvals.

## Desktop decision flow (added)

- In the Approvals tab, selecting a well-formed `requested` approval enables **Approve selected…** and **Reject selected…**. Other states, malformed rows and in-flight decisions leave them disabled; the selection survives the periodic list refresh.
- A modal shows the exact operation, target, candidate SHA-256, policy version and request version being decided, and asks for the approver token (masked, 43 characters, never stored) and an optional reason (it may not contain the token). The fields are cleared when the dialog closes.
- The decision is sent off the UI thread through the same `_request` transport as the CLI. The body binds the displayed values and `expected_version`; if the selected row changes while the modal is open, nothing is submitted.
- Each attempt uses a fresh `X-Correlation-ID`. The `Idempotency-Key` is generated per distinct decision and reused only when retrying the same decision after a lost or unconfirmed response (network error); after any definitive answer (accepted, 401, 403, 409) the next attempt gets a new key.
- Results map to fixed messages: 401 (credentials not accepted), 403 (approver token not accepted or not allowed to decide), 409 (request changed, expired or already decided). Server bodies and exception text are never displayed or logged, and the token is not written to the event log. On success the approvals page is refreshed.
- Limitation: the token is a Python string for the duration of the request and cannot be reliably zeroed from memory; it is dropped as soon as the request completes.

## Requester authority and daemon-side request creation (added)

- `DaemonRequesterAuthority` (`infrastructure/daemon_requester.py`) is the requester counterpart to `LocalApproverAuthority`. Its only subject is `daemon:workflow` with kind `agent` (principal `agent:daemon:workflow`). It authenticates an in-memory capability object created with the authority instance; no string, token, file or HTTP header can stand in for it. It allows only `approval.request`, never `approval.decide`, and `ApprovalService.decide` additionally requires a human distinct from the requester. The human approver authority allows only `approval.decide`, so the approver token cannot create requests.
- `DaemonApprovalRequester.request_approval` (`application/approval_requests.py`) is the internal daemon API. It binds run, operation, target, candidate sha256, policy version and an absolute expiry (timezone-aware, in the future, at most 24 hours ahead), requires the run to exist, and delegates to `ApprovalService.request` so the record, audit entry, `approval.requested` event, outbox row and idempotency receipt commit atomically. The approval id is derived from `run_id` and the idempotency key, so retries replay the stored receipt and a changed request under the same key conflicts. A replay after the expiry window has passed is rejected by the expiry check before the receipt is consulted; callers should read approval state instead.
- There is deliberately no HTTP route: the operator bearer token still cannot request or decide. Nothing in the daemon calls the requester yet; the fixed mock workflow is unchanged. Real agent/provider sessions and a workflow step that waits on a decision remain out of scope.
- Limitation: any code inside the daemon process that obtains the requester object can create requests; this is a daemon-trust boundary, not isolation from daemon code.

## Denied decision audit (added)

- Denied `POST /api/v1/approvals/{id}/decision` attempts write `audit_entries` rows with `decision='denied'`, `action='approval.decide'` and `details_json` of `{"reason":..., "http_status":...}`. HTTP statuses and bodies are unchanged.
- Reasons are a fixed set: `unauthenticated` (missing, malformed, wrong or operator-token-as-approver tokens are deliberately indistinguishable, so audit reads never reveal how close a token was), `not_permitted`, `self_approval`, `conflict` (binding/version/expiry/idempotency, 409) and `rate_limited`. Validation errors (400/422) are not audited.
- No token, credential, header value or request text is stored. Unauthenticated rows use principal and resource `unauthenticated` (not the caller-supplied approval id); post-authentication denials use `human:local-approver` and the approval id.
- Rows are written with a separate autocommit statement after the decision transaction has rolled back, so they persist. A failing audit write is swallowed and never changes the response.
- Flood bound: per 60 second window, at most 20 denial rows per principal bucket (all unauthenticated callers share one bucket). The first overflow writes one `rate_limited` marker row, then further attempts are dropped until the next window. Counters are in memory, so a daemon restart can allow up to 20 more rows. Failed operator-bearer authentication is not part of this audit table; it is logged at WARNING (method and path only, never the header or token).

## Approval subject (F6)

- `GET /api/v1/approvals` (operator bearer, read-only) adds `subject` to every row: `null` unless the approval belongs to an agent run's recorded approval step, otherwise `{kind: "agent_run", node_id, repo, base_ref, worktree_head, allowed_tools, permission_mode, timeout_seconds, prompt_bytes, prompt_preview, prompt_preview_truncated, prompt_unavailable, spec_sha256}`.
- `spec_sha256` equals `candidate_manifest_sha256`: the SHA-256 the approver must echo as `candidate_sha256` binds the whole task spec (prompt, repo, base ref, worktree identity, tools, mode, timeout), not only the prompt. A UI should show the fields, not the digest alone, and the digest as a checksum.
- `prompt_preview` is at most 8 KiB (`prompt_preview_truncated` says when cut), read from the private, digest-verified prompt artifact, and only while the approval is still `requested`; decided rows carry the structured fields with `prompt_preview: null`. If the artifact cannot be read, `prompt_unavailable` is true (the approver should refuse). The subject is derived only from the run's trusted spec record, never from client input, and never contains tokens.
- The operation/target labels remain operator-chosen; the subject is what the human should judge. The server-side `require_approval*` policy (agent-runs.md) prevents omitting approval for risky specs. The approval is not single-use or revocable yet (review F13).

## Passphrase mode (opt-in)

A passphrase-verifier authority is available as an opt-in alternative to the token file: see `approver-passphrase.md` (it is not a boundary against same-user code).

## Lockout visibility and recovery (passphrase mode)

- Policy: 5 wrong passphrases that reached verification lock the approver for a fixed 15 minutes. There is no doubling and no longer lockout. Malformed, missing or wrong-length credentials never reach the KDF and are not counted.
- Visible: the 403 decision response says `Approval access denied: approver locked until HH:MM UTC` and carries `X-Approver-Locked-Until: HH:MM` (UTC). `agentctl approval-decide` prints it and the desktop decision status and gate note show it. When a lockout starts the daemon writes an `approver.lockout` audit row (`decision='denied'`, details `{reason: "lockout", seconds, until_utc}`); an operator reset writes `approver.unlock`.
- Recovery without the passphrase: run `agentctl approver-unlock` at an interactive terminal (stdin and stdout must be a TTY; you confirm by typing `unlock`). It only creates a private marker file `approver.unlock` in the state directory. The daemon consumes the marker on the next attempt, zeroes the failed-attempt counter and audits it. It never approves anything and the next wrong guess counts again. Restarting the daemon also clears the in-memory lockout. A marker that predates the lockout (or the daemon start) is deleted, so it cannot pre-authorise a reset; a marker that is not a private, user-owned regular file is ignored.
- Residual denial of service, stated plainly: any process that can reach the daemon socket and the operator token (a same-user agent) can still send 5 wrong passphrases every 15 minutes and keep the human locked out, and the unlock marker is just a file, so the same process could also create it (which only helps it guess again). The bound is that the human is delayed in 15 minute steps and can always recover with the operator CLI or a restart, not that the attack is prevented. Same-user code can also brute-force the verifier offline with no lockout, so this lockout is an availability and audit control, not a boundary against it.
