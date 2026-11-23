# ADR 018: Audit hash chain, external anchors and what they do not protect

Status: proposed. Responds to F1 of `docs/security/review-2026-11-20.md` (any same-user process can edit
`synapse.sqlite`: forge an approval, `DELETE FROM audit_entries`, `DROP TRIGGER`). Migration `0015_audit_hash_chain.sql`
(renumbered from 0014 after PR #92 merged with 0014_usage_ledger.sql; migrations must stay contiguous).

## The threat model, stated honestly

The adversary is a process running as the daemon's own user (for example a compromised agent child, F2/F7). It can read
and write `synapse.sqlite`, read every file in the state directory, read the daemon's environment of future starts,
start and stop user services and edit user unit files. We assume it is **not** root, does not defeat `ptrace_scope`
to read daemon memory, and cannot write or vacuum the system journal.

**Nothing inside the database file can stop this adversary.** SQLite triggers, CHECK constraints and hash columns are
all rewritable by whoever can write the file; an unkeyed hash chain can be recomputed from the first modified row to the
head with `hashlib`. So the chain by itself is *not* tamper evidence against this adversary. Three tempting upgrades
do not change that:

| Idea | Why it does not help here |
|---|---|
| HMAC with a key held in daemon memory | Cannot verify across a restart (key is gone); every restart would re-baseline, which is the attack. |
| HMAC with a key file | The adversary reads the file; it can compute the MAC. |
| Kernel keyring (`keyctl`) | The user keyring is shared by all processes of the uid (they possess it through their session keyring link). A process keyring survives only the process lifetime, so it has the same restart problem as memory. No isolation gained. |

The only thing a same-user adversary cannot do is alter state held by a more privileged component. On a systemd system
the persistent journal is such a component: `journald` stores records in files the user cannot write, stamps
trusted fields (`_UID`, `_SYSTEMD_USER_UNIT`) from the sender's cgroup, and the user cannot delete or edit entries
without root. That is the one anchor we can use.

## Decision

1. **Chain.** `chain_links(stream, sequence, entry_hash)` holds, for each `audit_entries` and `events` row,
   `sha256(prev_hash NUL canonical_json(row))`, starting from a per-stream genesis constant. Links are computed in
   Python (SQLite has no hash function). Audit rows are chained synchronously in the same transaction as the row
   (`audit_chain.append_audit`, the only audit writer). Events (about 25 insert sites) are sealed lazily by the daemon:
   at start, on a timer, before any gated execution and at shutdown. Rows that existed before the migration are adopted
   on first start (trust on first use). The adoption is explicit, never silent: the same transaction records
   `chain_adoptions(stream, adopted_up_to, adopted_at)` (insert-only), the daemon publishes an `adopt` anchor carrying
   `adopted_up_to`, and verify reports "rows up to seq N were adopted trust-on-first-use, not attested". Rows at or below N
   were never covered by an anchor, so the chain proves nothing about their pre-adoption history; a later edit of the marker is
   caught by comparing it with the journalled `adopt` anchor.
2. **Anchors.** Whenever the daemon seals, it publishes `(stream, sequence, hash)` of each head to the journal
   (`SYSLOG_IDENTIFIER=synapse-chain-anchor`) and to `chain-anchors.log` in the state directory. The file sink is
   same-user writable and only catches edits made by something that does not know about it ("local" strength). The journal
   sink is "journal-trusted" when the daemon runs in a user service and the reader filters on that unit
   (`_SYSTEMD_USER_UNIT`), because records injected by other same-user processes then carry a different unit. Under a plain
   shell or without `journalctl` the strength degrades and the daemon says so at startup.
3. **Startup verification (bounded).** Re-hash the last `DEFAULT_WINDOW` (20 000) links per stream, check every readable
   anchor against `chain_links` (a point lookup each, at most 5 000 read), and check that the audit table has no unchained rows
   and no rows beyond the last audit anchor. Cost depends on the window and the anchor read limit, not on table size
   (tested with a progress-handler instruction count). `python -m synapse.persistence.audit_chain verify --db ... --full`
   re-hashes everything.
4. **Fail closed.** A broken chain makes `verify_approval_for_execution` (the single place an `ApprovalGrant` is minted)
   raise `ApprovalChainBroken`. Gated tasks therefore stay parked and never run; ungated work is unaffected. The
   verdict is held in daemon memory for the process lifetime and re-checked at each grant: the audit head must equal what this
   process last wrote. An approved approval must additionally have a chained `approval.decide` audit row by the same principal
   saying `approved` (this is what stops "UPDATE approvals SET state='approved'" without a matching audit row).
5. **Triggers (accidental misuse only).** Migration 0015 also makes `audit_entries` append-only, makes `chain_links`
   immutable, validates audit decisions and restricts `approval_requests.state` to the legal transitions
   (`requested -> approved|rejected|expired|revoked`, `approved -> revoked|expired`). These stop bugs and a careless `sqlite3` session. They do
   **not** stop the adversary, who can `DROP TRIGGER`. The existing backup schema fingerprint covers the new triggers
   automatically (it is derived from the migration files), so a database missing them is rejected by backup-verify/restore, and
   the daemon runs the same comparison at startup and reports a modified or missing trigger as a broken chain.
6. **Reset.** A restored backup is older than the anchors, which looks exactly like truncation. The operator accepts it
   once with `agentd --accept-chain-reset`, which publishes a `reset` anchor from the daemon's unit. A reset never forgives rows
   that no longer match their links; those need investigation. A forged reset requires being able to start the daemon (or run
   inside its unit), and it leaves a permanent journal record.

## What this does protect against (given a trusted journal anchor)

* Editing or deleting a sealed, anchored audit/event row, including by recomputing the chain: the recomputed link no longer
  equals the journalled hash. Deleting rows is caught inside the verification window; deleting older rows is caught by
  `--full`.
* Truncating the tables or rolling the database back below the last anchor.
* Appending a forged audit row or forging an approval while the daemon is running, or while it was last shut down cleanly: the head
  differs from the in-memory head or from the last anchor, and a forged approval has no matching chained decision.
* Accidental updates, deletes and illegal state transitions through the SQL layer.

## What this does NOT protect against

* **Whatever happens in the unanchored window.** Anchors are published after commit, on a timer (30 s), at gate time and at
  shutdown. An edit made and then re-sealed before the next anchor is not seen. After a crash the same window shows up as
  "rows beyond the last anchor" and needs `--accept-chain-reset`; the price of not letting an attacker append silently.
* **Events appended by an attacker while the daemon runs** are adopted by the next seal (events are sealed lazily, so
  the daemon cannot tell them from its own). Events are evidence of history, not an authorization input.
* **Old rows outside the startup window** edited without touching their links are caught only by `--full`.
* **A same-user attacker with root, or one who can write the journal as the daemon's unit.** Starting a transient unit with
  the daemon's name while the real one is stopped, editing the unit file to drop the filter, or `ptrace` of the daemon
  all defeat the anchor. Without a persistent journal (`Storage=persistent`) the evidence is lost on reboot.
* **A hostile process that never touches the database but uses the daemon's own authority** (the F2 token problem).
  The chain records what happened; it does not decide who may approve.
* **Denial of service.** Anyone who can inject journal records with the right trusted fields, or edit a row, can force
  gated tasks to refuse. That is the intended fail-closed direction.
* **Anchor publication is best effort.** Each sink failure is logged as a warning (sink name and error class only). A head is
  considered published only if at least one sink accepted it, so the next flush retries. The journal send uses a 0.5 s socket
  timeout so a full journald socket cannot stall the request path.
* Availability of `journalctl` at verify time: if it cannot be read, the check degrades to local-file anchors and the
  daemon logs it; it does not claim the stronger guarantee.

## Alternatives rejected

* Keyed MAC (memory, file, keyring): see the table; no restart-safe secret exists at user privilege.
* Chain columns on the rows themselves: needs a write hook on all ~25 event insert sites; a separate link table with lazy
  sealing keeps the change local. The price is the unanchored-window caveat for events.
* Remote anchoring (another host, transparency log): meaningful but needs infrastructure outside this local-first tool.
  The anchor sink interface (`publish`, `read`, `strength`) allows adding one.

## Operations

* Verify without the daemon: `python -m synapse.persistence.audit_chain --db STATE/synapse.sqlite [--full] [--anchor-file STATE/chain-anchors.log] [--unit synapse-daemon.service]`.
* Verify exit status: `0` verified against at least one anchor; `1` broken; `2` **UNANCHORED**: links are self-consistent but no
  readable sink or no anchor records exist for this database, so nothing external vouches for them. The command prints a WARNING
  and `anchors=None`-style output; do not treat 2 as a pass. Adopted ranges are listed as NOTE lines.
* `SYNAPSE_DISABLE_JOURNAL_ANCHORS=1` makes `default_sinks` and the verify command ignore the journal (tests set it in
  `tests/conftest.py` so they do not write `synapse-chain-anchor` records to the user's journal; tests needing the journal sink
  construct `JournalAnchors` with a fake). Never set it for a real daemon.
* Run the daemon as a user service and persist the journal; otherwise treat the chain as evidence against accidents only.
* After restoring a backup, expect a broken chain and start once with `--accept-chain-reset`.
