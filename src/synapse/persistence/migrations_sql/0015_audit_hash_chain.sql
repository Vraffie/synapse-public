-- Tamper evidence and accidental-misuse protection for the audit trail (ADR 018).
-- chain_links holds one SHA-256 link per audit_entries/events row. The links are
-- written by the daemon (synapse.persistence.audit_chain), because SQLite has no
-- hash function; verification and anchoring also live there. Everything below
-- is defence against bugs and casual edits: a same-user process can drop any
-- trigger, so detection of deliberate tampering rests on the external anchors
-- described in the ADR, not on this file.
CREATE TABLE chain_links (
  stream TEXT NOT NULL CHECK (stream IN ('audit','events')),
  sequence INTEGER NOT NULL CHECK (sequence > 0),
  entry_hash TEXT NOT NULL CHECK (length(entry_hash) = 64),
  PRIMARY KEY (stream, sequence)
) WITHOUT ROWID;
CREATE TRIGGER chain_links_no_update BEFORE UPDATE ON chain_links
BEGIN SELECT RAISE(ABORT, 'chain links are immutable'); END;
CREATE TRIGGER chain_links_no_delete BEFORE DELETE ON chain_links
BEGIN SELECT RAISE(ABORT, 'chain links are immutable'); END;

-- Rows that existed before the chain are adopted on first start without any attestation
-- (trust on first use). This records, once per stream, up to which sequence that happened.
CREATE TABLE chain_adoptions (
  stream TEXT NOT NULL PRIMARY KEY CHECK (stream IN ('audit','events')),
  adopted_up_to INTEGER NOT NULL CHECK (adopted_up_to > 0),
  adopted_at TEXT NOT NULL
) WITHOUT ROWID;
CREATE TRIGGER chain_adoptions_no_update BEFORE UPDATE ON chain_adoptions
BEGIN SELECT RAISE(ABORT, 'chain adoptions are immutable'); END;
CREATE TRIGGER chain_adoptions_no_delete BEFORE DELETE ON chain_adoptions
BEGIN SELECT RAISE(ABORT, 'chain adoptions are immutable'); END;

CREATE TRIGGER audit_no_update BEFORE UPDATE ON audit_entries
BEGIN SELECT RAISE(ABORT, 'audit entries are append-only'); END;
CREATE TRIGGER audit_no_delete BEFORE DELETE ON audit_entries
BEGIN SELECT RAISE(ABORT, 'audit entries are append-only'); END;
CREATE TRIGGER audit_no_replace BEFORE INSERT ON audit_entries
WHEN NEW.sequence IS NOT NULL AND EXISTS (SELECT 1 FROM audit_entries WHERE sequence = NEW.sequence)
BEGIN SELECT RAISE(ABORT, 'audit entries are append-only'); END;
CREATE TRIGGER audit_decision_valid BEFORE INSERT ON audit_entries
WHEN NEW.decision NOT IN ('allowed','denied')
BEGIN SELECT RAISE(ABORT, 'invalid audit decision'); END;

-- Legal approval lifecycle: requested -> approved|rejected|expired|revoked and
-- approved -> revoked|expired (restore neutralisation; lapse). Everything else is terminal.
CREATE TRIGGER approval_state_transition BEFORE UPDATE OF state ON approval_requests
WHEN NEW.state IS NOT OLD.state AND NOT (
  (OLD.state = 'requested' AND NEW.state IN ('approved','rejected','expired','revoked'))
  OR (OLD.state = 'approved' AND NEW.state IN ('revoked','expired')))
BEGIN SELECT RAISE(ABORT, 'illegal approval state transition'); END;
CREATE TRIGGER approval_state_valid BEFORE INSERT ON approval_requests
WHEN NEW.state NOT IN ('requested','approved','rejected','expired','revoked')
BEGIN SELECT RAISE(ABORT, 'invalid approval state'); END;
