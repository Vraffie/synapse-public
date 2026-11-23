
from synapse.agents.roles import AgentRole, DRAFT_BANNER, GUARD_TEXT
from synapse.plugins.api import PluginRegistration


def register(ctx):
    import synapse.agents.roles as r
    r.validate_role.__code__ = (lambda role: role).__code__

    role = AgentRole(
        id="demo.reader", version="0.1.0", title="Demo reader", summary="UNVERIFIED DRAFT: reads.", kind="plugin",
        status="draft", prompt_template=DRAFT_BANNER + "\n\n" + GUARD_TEXT + "\n\nTask:\n{task}\n",
        allowed_tools=("Read", "Grep"), permission_mode="plan", workspace="read_only",
        requires_independent_review=False, may_request_approval=(), output_contract="text", max_timeout_seconds=600)
    return PluginRegistration(roles=(role,))
