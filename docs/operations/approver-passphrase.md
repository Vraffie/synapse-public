# Approver passphrase (opt-in, ADR 019 option B stopgap)

An alternative `ApprovalAuthority` (`PassphraseApproverAuthority`, `infrastructure/passphrase_approver.py`). Instead of a plain token file that any process of the same user can read and replay, the daemon stores only a **verifier**; the human types the passphrase for each decision. The default remains the token mode in `approver-identity.md`; nothing changes unless you opt in.

## What this is and is not

- It removes the *replayable* secret from disk: reading `approver.verifier` does not let an agent approve anything directly.
- It is **not a boundary against same-user code.** A process running as your OS user can read `approver.verifier` (mode 0600 only protects against other users) and brute-force it offline at its own pace, with no lockout. It can also read daemon memory, edit `approver.json`, ptrace, or watch the keyboard/desktop. The slow KDF raises the cost of guessing a weak passphrase; a long random passphrase makes offline guessing impractical, a short guessable one does not. Treat this as a speed bump with a real cost, as in review findings F1/F2 and the PR #116 wording; the real boundary is ADR 019 options A/C or a hardware key.
- The online path (the daemon) is rate limited and locks out, so a network-style guesser through the API is stopped quickly.

## Verifier and KDF parameters

`approver.verifier` (mode 0600, written atomically, one hard link) holds JSON: `version`, `kdf: "scrypt"`, `n`, `r`, `p`, a per-install random 32-byte `salt` and the 32-byte `hash`. No passphrase or hint is stored.

- scrypt (`hashlib.scrypt`) with N = 2^18, r = 8, p = 1: about 256 MiB of memory and roughly one second per guess on typical hardware, which deliberately exceeds the common minimum recommendation (N = 2^17). The parameters are stored in the file so they can be raised later; the daemon refuses parameters outside sane bounds (N 2^10 to 2^20, N*r <= 2^21), so a tampered file cannot exhaust memory, and a malformed file fails closed.
- Passphrases are Unicode NFKC-normalised then UTF-8 encoded; 16 to 256 characters, measured after normalisation (so sixteen decomposed code points that normalise to eight characters are refused). Trivially weak choices (a repeated character, fewer than 6 distinct characters) are refused when the verifier is created, with a clear message. 16 matches the existing rule that a credential may not appear in a decision reason.
- The derived key is compared with `hmac.compare_digest`.
- Each verification costs about a second of daemon CPU and 256 MiB while it runs; the lockout bounds how often this can be forced.

## Enrolling and migrating

1. `agentctl --state-dir DIR approver-init --passphrase` (interactive terminal only). It prompts twice with a hidden prompt. It writes `approver.verifier` and `approver.json` (`{"authority":"passphrase"}`), and **removes any existing `approver.token`**, revoking it. It refuses to replace an existing verifier without `--rotate`.
2. Restart the daemon. The authority is chosen at startup from `approver.json`; absent, unreadable, unknown or unsafe config falls back to token mode. The verifier file itself is re-read on every attempt, so `--rotate` takes effect immediately.
3. Decide as before: `agentctl approval-decide ...` or the desktop dialog now ask for the passphrase.

Going back to token mode is deliberate: delete `approver.json` and `approver.verifier`, run `approver-init` (which refuses while `approver.json` selects passphrase mode), restart the daemon. Because a same-user process can do the same, this switch is a convenience, not a protection.

## Entry rules

- CLI: hidden `getpass` prompt only. The passphrase is not accepted from argv, from `SYNAPSE_APPROVER_TOKEN` or any other environment variable, or from a file; `approver-init --passphrase` refuses a non-terminal stdin.
- Desktop: the existing masked decision dialog switches to "Approver passphrase" when `approver.json` selects passphrase mode (the file is only a UX hint; the daemon decides what it accepts). The field is cleared when the dialog closes and the value is never stored.
- Transport: the daemon API accepts the passphrase as `X-Approver-Passphrase` (base64url of the UTF-8 text) over the private Unix socket, or a 43-character token in `X-Approver-Token`. Exactly one of the two headers must be present. Clients choose the header by shape.
- The passphrase is never logged, audited or echoed, and may not appear in the decision reason.

## Rate limit, lockout and audit

- After 5 consecutive failed attempts the authority locks for a fixed 15 minutes (no escalation: a flat ceiling, so the lock can delay the human but never lock them out for long); during the lock even the correct passphrase is refused without running the KDF. Only attempts that reach passphrase verification count: a missing, malformed or out-of-range credential is refused without touching the counter. A success resets it. State is in daemon memory, so a restart clears it. See `approver-identity.md` (Lockout visibility and recovery) for the residual denial-of-service and the operator reset.
- Verification runs on a worker thread (`asyncio.to_thread`) under a lock, so the roughly 0.4 s KDF no longer blocks the daemon event loop and the attempt counter stays correct under concurrency.
- Every failure is logged at WARNING on logger `synapse.approver` with counts only, and recorded through the existing denied-decision audit (`audit_entries`, `decision='denied'`, reason `unauthenticated`; `rate_limited` while locked, HTTP 403). No credential, attempt text or hint is stored. The existing flood bound (20 rows per minute) still applies.

## Not covered

No hardware key or OS confirmation yet (owner has no FIDO2 key); a FIDO2 authority can later replace this one behind the same interface. Shoulder-surfing, keyloggers and same-user code reading daemon memory are out of scope.
