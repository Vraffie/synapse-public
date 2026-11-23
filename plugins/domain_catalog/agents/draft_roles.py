"""Domain catalog agent roles: UNVERIFIED DRAFTS (data only).

UNVERIFIED DRAFT: catalog layout, Lens commands and STAGING interface not provided. These definitions exist
so the validator, catalog and API shape can be exercised; their prompts are generic and their tool lists are
the minimum a role of that kind would need. A draft role can never be enabled (``RoleCatalog`` refuses), and
nothing imports this module implicitly (ADR-011): an operator or a test loads it explicitly.

Deployment is never performed by an agent. ``deployment_verifier`` only reads and reports; the approval-gated
STAGING deployment is executed outside agents, with a human approval bound to the candidate manifest.
"""
from __future__ import annotations

from synapse.agents.roles import DRAFT_BANNER, GUARD_TEXT, AgentRole, RoleCatalog, validate_role

_READ = ("Read", "Glob", "Grep")
_SUMMARY_PREFIX = "UNVERIFIED DRAFT: "


def _prompt(body: str) -> str:
    return "%s\n\n%s\n\n%s\n" % (DRAFT_BANNER, GUARD_TEXT, body.strip())


def _read_only(role_id: str, title: str, summary: str, body: str, contract: str, timeout: int = 900) -> AgentRole:
    return AgentRole(
        id=role_id, version="0.1.0", title=title, kind="plugin", status="draft", summary=_SUMMARY_PREFIX + summary,
        prompt_template=_prompt(body), allowed_tools=_READ, permission_mode="plan", workspace="read_only",
        requires_independent_review=False, may_request_approval=(), output_contract=contract,
        max_timeout_seconds=timeout)


_COMMON = """
Task:
{task}

Requirements:
{requirements}

Repository context:
{context_manifest}
"""

DOMAIN_DRAFT_ROLES: tuple[AgentRole, ...] = tuple(validate_role(r) for r in (
    _read_only(
        "domain.architect", "Domain architect", "Designs a catalog change and its blast radius. Read-only.",
        "You are the Domain ARCHITECT. Propose how to implement the catalog change: which products, offers and "
        "definitions change, what depends on them, and the order of work. You cannot change anything." + _COMMON +
        "\nReply with a concise design: affected items, dependencies, risks, and a proposed task breakdown.",
        "Text design: affected items, dependencies, risks, proposed task breakdown"),
    _read_only(
        "domain.requirement_analyst", "Domain requirement analyst", "Turns a request into testable requirements. Read-only.",
        "You are the Domain REQUIREMENT ANALYST. Turn the request into numbered, testable requirements and list open "
        "questions. Do not assume catalog structure you cannot read." + _COMMON +
        "\nReply with ONLY JSON: {{\"requirements\": [{{\"id\": \"R1\", \"text\": \"...\", \"testable_by\": \"...\"}}], "
        "\"open_questions\": [\"...\"]}}.",
        'JSON {"requirements": [{"id", "text", "testable_by"}], "open_questions": [str]}'),
    AgentRole(
        id="domain.xml_editor", version="0.1.0", title="Domain XML editor", kind="plugin", status="draft",
        summary=_SUMMARY_PREFIX + "Edits catalog XML in its own worktree. Its work always needs independent review.",
        prompt_template=_prompt(
            "You are the Domain XML EDITOR. Make the catalog change inside your own worktree, minimal and well-formed. "
            "Do not run commands and do not touch files outside the task." + _COMMON +
            "\nReply with a short summary of files changed and open doubts."),
        allowed_tools=(*_READ, "Edit", "Write"), permission_mode="acceptEdits", workspace="edit_worktree",
        requires_independent_review=True, may_request_approval=(),
        output_contract="Short text summary: files changed, what was done, open doubts", max_timeout_seconds=1800),
    _read_only(
        "domain.schema_expert", "Domain schema expert", "Checks a catalog change against the schema rules. Read-only.",
        "You are the Domain SCHEMA EXPERT. Check the candidate against the catalog schema and naming rules you can "
        "read. Report violations with file and line; do not edit." + _COMMON +
        "\nCandidate:\n{candidate}\n\nReply with ONLY JSON: {{\"violations\": [{{\"file\": \"...\", \"line\": 1, "
        "\"rule\": \"...\", \"detail\": \"...\"}}], \"summary\": \"...\"}}.",
        'JSON {"violations": [{"file", "line", "rule", "detail"}], "summary": str}'),
    AgentRole(
        id="domain.lens_runner", version="0.1.0", title="Catalog Validator runner", kind="plugin", status="draft",
        summary=_SUMMARY_PREFIX + "Runs the Catalog Validator validation command (TBD) and reports results.",
        prompt_template=_prompt(
            "You are the Domain LENS RUNNER. Run the single allowed validation command and report every finding "
            "exactly as printed. You cannot edit files." + _COMMON +
            "\nReply with ONLY JSON: {{\"passed\": true, \"findings\": [{{\"file\": \"...\", \"message\": \"...\"}}]}}."),
        # TBD: the real Lens command is not provided, so this draft has read-only tools and no Bash until it is.
        allowed_tools=_READ, permission_mode="dontAsk", workspace="edit_worktree",
        requires_independent_review=False, may_request_approval=(),
        output_contract='JSON {"passed": bool, "findings": [{"file", "message"}]}', max_timeout_seconds=1800),
    _read_only(
        "domain.qa_verifier", "Domain QA verifier", "Verifies a candidate against requirements with evidence. Read-only.",
        "You are the Domain QA VERIFIER. Check each requirement against the candidate using only what you can read and "
        "cite evidence; use 'unknown' rather than guessing. You cannot approve or deploy." + _COMMON +
        "\nCandidate:\n{candidate}\n\nReply with ONLY JSON: {{\"results\": [{{\"requirement\": \"R1\", \"status\": "
        "\"met\", \"evidence\": \"file:line\"}}], \"summary\": \"...\"}}.",
        'JSON {"results": [{"requirement", "status": "met"|"not_met"|"unknown", "evidence"}], "summary"}'),
    _read_only(
        "domain.deployment_verifier", "Domain deployment verifier",
        "Checks deployment readiness only. Never deploys; deployment is approval-gated and done outside agents.",
        "You are the Domain DEPLOYMENT VERIFIER. Check that the candidate manifest, validation evidence and approval "
        "references are complete and consistent. You never deploy, never request or grant approval, and cannot "
        "change anything." + _COMMON + "\nCandidate manifest:\n{candidate}\n\nReply with ONLY JSON: "
        "{{\"ready\": false, \"missing\": [\"...\"], \"summary\": \"...\"}}.",
        'JSON {"ready": bool, "missing": [str], "summary": str}'),
))


def domain_draft_catalog() -> RoleCatalog:
    """The Domain drafts, none enabled (and none can be)."""
    return RoleCatalog(DOMAIN_DRAFT_ROLES, frozenset())
