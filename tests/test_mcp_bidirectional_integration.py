"""Two scoped MCP agents exchange durable review messages across a restart."""
import asyncio
from contextlib import AsyncExitStack
import json
import os
from pathlib import Path
import sys

import pytest

pytest.importorskip("mcp")
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from synapse.application.message_gateway import MessageGateway
from synapse.application.messaging import MessageAccessDenied
from synapse.application.session_authority import SessionAuthority
from synapse.infrastructure.artifact_blobs import ArtifactBlobStore
from synapse.infrastructure.daemon_database import daemon_database
from synapse.mcp_gateway.ipc import private_ipc_server
from synapse.persistence.artifact_store import publish_artifact

ROOT = Path(__file__).resolve().parents[1]


def _seed(db, blob_dir):
    db.execute("INSERT INTO workflow_versions VALUES('wf','1','{}','hash','now')")
    db.execute("INSERT INTO runs VALUES('run','wf','1','running',0,'now','now')")
    db.execute("INSERT INTO agent_definitions VALUES('agent','1',NULL,'{}')")
    for instance in ("author", "reviewer"):
        db.execute("INSERT INTO agent_instances(id,run_id,definition_id,definition_version,state,created_at) VALUES(?,'run','agent','1','running','now')", (instance,))
    db.execute("INSERT INTO threads VALUES('thread','run','review','open','now')")
    for artifact in ("author-result", "reviewer-result"):
        publish_artifact(db, ArtifactBlobStore(blob_dir, max_bytes=100), chunks=[artifact.encode()], artifact_id=artifact,
                         media_type="text/plain", provenance={"run_id": "run"}, principal="daemon", idempotency_key=artifact)


async def _client(stack, path, credential):
    env = dict(os.environ)
    env["SYNAPSE_MCP_SOCKET"] = str(path)
    env["SYNAPSE_MCP_CREDENTIAL"] = credential
    params = StdioServerParameters(command=sys.executable, args=["-m", "synapse.mcp_gateway"], env=env)
    read, write = await stack.enter_async_context(stdio_client(params))
    client = await stack.enter_async_context(ClientSession(read, write))
    await client.initialize()
    return client


async def _call(client, name, arguments):
    response = await client.call_tool(name, arguments)
    assert not response.isError, response.content
    return json.loads(response.content[0].text)


def test_two_mcp_agents_exchange_review_and_resume_after_restart(tmp_path):
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    blob_dir = tmp_path / "blobs"
    blob_dir.mkdir(mode=0o700)
    socket = state / "agent.sock"
    with daemon_database(state / "db.sqlite", ROOT / "migrations") as db:
        _seed(db, blob_dir)
        async def first_session():
            authority = SessionAuthority()
            author = authority.issue(instance_id="author", run_id="run", grants=[("message.send", "thread"), ("message.recipient", "reviewer")])
            reviewer = authority.issue(instance_id="reviewer", run_id="run", grants=[("inbox.claim", "reviewer"), ("thread.access", "thread"), ("message.result_artifact", "reviewer-result"), ("message.send", "thread"), ("message.recipient", "author")])
            gateway = MessageGateway(db, authority)
            async with private_ipc_server(socket, gateway), AsyncExitStack() as stack:
                a = await _client(stack, socket, author.secret)
                b = await _client(stack, socket, reviewer.secret)
                request = {"idempotency_key": "request", "message_id": "review-request", "thread_id": "thread", "type": "review.request", "payload": {"revision": "candidate-1"}, "created_at": "2026-11-19T12:00:00Z", "recipients": ["reviewer"]}
                sent = await _call(a, "send_message", request)
                replayed = await _call(a, "send_message", request)
                assert not sent["replayed"] and replayed["replayed"] and sent["response"] == replayed["response"]
                lease = (await _call(b, "get_inbox", {"idempotency_key": "review-inbox"}))["response"][0]
                assert lease["delivery_id"] == sent["response"][0]
                gateway.grant_owned_delivery(reviewer.identity.principal, lease["delivery_id"])
                read = await _call(b, "read_delivery", {"delivery_id": lease["delivery_id"]})
                assert read["sender_instance_id"] == "author" and read["type"] == "review.request" and read["payload"]["revision"] == "candidate-1"
                ack = {"idempotency_key": "review-ack", "delivery_id": lease["delivery_id"], "token": lease["token"], "success": True, "result_artifact_id": "reviewer-result"}
                assert (await _call(b, "ack_delivery", ack))["response"]["state"] == "acknowledged"
                assert (await _call(b, "ack_delivery", ack))["replayed"]
                reply = {"idempotency_key": "reply", "message_id": "review-finding", "thread_id": "thread", "type": "review.finding", "payload": {"decision": "changes_required"}, "created_at": "2026-11-19T12:01:00Z", "recipients": ["author"], "causation_id": "review-request"}
                reply_id = (await _call(b, "send_message", reply))["response"][0]
                assert db.execute("SELECT state FROM message_deliveries WHERE id=?", (reply_id,)).fetchone() == ("pending",)
            return author.secret, reply_id
        old_secret, reply_id = asyncio.run(first_session())
    # Restart drops all in-memory sessions; pending messages remain durable.
    with daemon_database(state / "db.sqlite", ROOT / "migrations") as reopened:
        async def second_session():
            authority = SessionAuthority()
            author = authority.issue(instance_id="author", run_id="run", grants=[("inbox.claim", "author"), ("thread.access", "thread"), ("message.result_artifact", "author-result")])
            gateway = MessageGateway(reopened, authority)
            with pytest.raises(MessageAccessDenied):
                gateway.invoke(old_secret, "claim", b'{"idempotency_key":"poll"}')
            async with private_ipc_server(socket, gateway), AsyncExitStack() as stack:
                client = await _client(stack, socket, author.secret)
                lease = (await _call(client, "get_inbox", {"idempotency_key": "author-inbox"}))["response"][0]
                assert lease["delivery_id"] == reply_id
                gateway.grant_owned_delivery(author.identity.principal, reply_id)
                message = await _call(client, "read_delivery", {"delivery_id": reply_id})
                assert message["sender_instance_id"] == "reviewer" and message["type"] == "review.finding"
                assert message["payload"] == {"decision": "changes_required"} and message["causation_id"] == "review-request"
                ack = {"idempotency_key": "author-ack", "delivery_id": reply_id, "token": lease["token"], "success": True, "result_artifact_id": "author-result"}
                assert (await _call(client, "ack_delivery", ack))["response"]["state"] == "acknowledged"
        asyncio.run(second_session())
        assert reopened.execute("SELECT state FROM message_deliveries ORDER BY id").fetchall() == [("acknowledged",), ("acknowledged",)]
        assert reopened.execute("SELECT COUNT(*) FROM messages").fetchone() == (2,)
        assert reopened.execute("SELECT COUNT(*) FROM events").fetchone() == reopened.execute("SELECT COUNT(*) FROM outbox").fetchone()
