"""Response schemas of the candidate manifest routes (see manifest_commands.py); merged into RESPONSES."""


def _object(properties):
    return {"type": "object", "properties": properties, "required": list(properties)}


_STRING = {"type": "string"}
_INTEGER = {"type": "integer"}
_BOOL = {"type": "boolean"}
_SHA = {"type": "string", "pattern": "^[0-9a-f]{64}$"}
_REASON = {"type": "string", "enum": ["baseline_moved", "candidate_invalid", "operator"]}
INVALIDATION = _object({"reason": _REASON, "actor": _STRING, "at": _STRING})
_LISTED = _object({"manifest_sha256": _SHA, "version": _INTEGER, "seq": _INTEGER, "artifact_id": _STRING,
                   "bindings_sha256": _SHA, "created_at": _STRING, "valid": _BOOL,
                   "invalidations": {"type": "array", "items": INVALIDATION}})
_BASE = "/api/v1/runs/{run_id}/candidate-manifests"

MANIFEST_RESPONSES = {
    (_BASE, "post"): (200, _object({"run_id": _STRING, "manifest_sha256": _SHA, "artifact_id": _STRING, "seq": _INTEGER,
                                    "invalidated": {"type": "array", "items": _REASON}, "replayed": _BOOL})),
    (_BASE, "get"): (200, _object({"run_id": _STRING, "manifests": {"type": "array", "items": _LISTED}})),
    (_BASE + "/{manifest_sha256}", "get"): (200, _object({
        "run_id": _STRING, "manifest_sha256": _SHA, "version": _INTEGER, "seq": _INTEGER, "created_at": _STRING,
        "valid": _BOOL, "manifest": {"type": "object", "additionalProperties": True},
        "invalidations": {"type": "array", "items": INVALIDATION},
        "diffs": {"type": "array", "items": _object({"repo_id": _STRING, "diff": _STRING, "truncated": _BOOL})}})),
    (_BASE + "/{manifest_sha256}/recheck", "post"): (200, _object({
        "run_id": _STRING, "manifest_sha256": _SHA, "valid": {"type": "boolean", "const": True},
        "invalidations": {"type": "array", "maxItems": 0}})),
    (_BASE + "/{manifest_sha256}/approval", "post"): (200, _object({
        "approval_id": _STRING, "run_id": _STRING, "manifest_sha256": _SHA, "operation": {"type": "string", "const": "deploy"},
        "target": {"type": "string", "enum": ["DEV", "STAGING"]}, "state": _STRING, "state_version": _INTEGER,
        "expires_at": _STRING})),
}
