"""Read routes for multi-repository runs (ADR 026). PROPOSALS, not a published contract.

ADR 026 lists these paths as proposals ("Route and event names in the ADR are proposals"); the daemon does not
serve them yet. Every multi-repository read in the desktop goes through this module, so a rename costs one edit
here. A daemon without these routes answers 404, which the desktop treats as "single-repository daemon" and hides
the multi-repository views.
"""
from __future__ import annotations

from urllib.parse import quote

PROPOSAL = True  # flip (and drop this comment) once A publishes the route table in docs/api/reference.md
PREFIX = "/api/v1"

# Statuses that mean "this daemon (or this run) has no multi-repository read here": hide, do not alarm.
NOT_SUPPORTED_STATUSES = (404, 405, 501)


def _seg(value: str) -> str:
    return quote(value, safe="")


def registry() -> str:
    """GET /repos: read-only registry view (ADR 026 D1 entries plus registry_sha256)."""
    return PREFIX + "/repos"


def run_repos(run_id: str) -> str:
    """GET /runs/{id}/repos: the run's bindings, per-attempt worktrees and commit states (ADR 026 D2, D4, D6)."""
    return f"{PREFIX}/runs/{_seg(run_id)}/repos"


def run_manifests(run_id: str) -> str:
    """GET /runs/{id}/candidate-manifests: the run's manifests with their invalidations (ADR 026 D8, D10)."""
    return f"{PREFIX}/runs/{_seg(run_id)}/candidate-manifests"


def run_manifest(run_id: str, manifest_sha256: str) -> str:
    """GET /runs/{id}/candidate-manifests/{sha}: one manifest with the per-repository diff for the owner to read."""
    return f"{run_manifests(run_id)}/{_seg(manifest_sha256)}"
