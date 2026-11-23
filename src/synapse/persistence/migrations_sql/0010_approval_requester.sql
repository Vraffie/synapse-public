-- Historical requests remain unbound and cannot be approved by the new service.
ALTER TABLE approval_requests ADD COLUMN requester_subject TEXT;
ALTER TABLE approval_requests ADD COLUMN requester_kind TEXT;
CREATE TRIGGER approval_requester_valid BEFORE INSERT ON approval_requests
WHEN NOT ((NEW.requester_subject IS NULL AND NEW.requester_kind IS NULL) OR
          (NEW.requester_subject IS NOT NULL AND length(trim(NEW.requester_subject)) BETWEEN 1 AND 256 AND COALESCE(NEW.requester_kind,'') IN ('human','agent')))
BEGIN SELECT RAISE(ABORT, 'invalid approval requester binding'); END;
CREATE TRIGGER approval_binding_immutable BEFORE UPDATE OF id,run_id,operation,target,candidate_manifest_sha256,policy_version,expires_at,requested_at,requester_subject,requester_kind ON approval_requests
BEGIN SELECT RAISE(ABORT, 'approval binding is immutable'); END;
CREATE TRIGGER approval_no_delete BEFORE DELETE ON approval_requests
BEGIN SELECT RAISE(ABORT, 'approval requests are immutable'); END;
CREATE TRIGGER approval_no_replace BEFORE INSERT ON approval_requests
WHEN EXISTS (SELECT 1 FROM approval_requests WHERE id=NEW.id)
BEGIN SELECT RAISE(ABORT, 'approval requests are immutable'); END;
