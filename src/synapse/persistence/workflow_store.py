"""Publish versioned mock DAGs and create durable runs under daemon ownership."""
from __future__ import annotations

from datetime import datetime, timezone
from graphlib import CycleError, TopologicalSorter
import hashlib
import json
import sqlite3

from synapse.domain.contracts import EventEnvelope, RunId, encode_payload, new_id, validate_id
from synapse.domain.runs import RunChange, RunState
from synapse.persistence.idempotency import CommandResult, execute_idempotent
from synapse.persistence.run_store import write_run_transition

_MAX_NODES = 1000


def _definition(nodes: list[dict]) -> list[dict]:
    if not isinstance(nodes, list) or not 1 <= len(nodes) <= _MAX_NODES:
        raise ValueError("workflow must contain 1 to 1000 nodes")
    graph: dict[str, list[str]] = {}
    for node in nodes:
        if not isinstance(node, dict) or set(node) != {"id", "dependencies"}:
            raise ValueError("each node requires only id and dependencies")
        node_id = validate_id(node["id"])
        if node_id in graph:
            raise ValueError("duplicate node ID")
        deps = node["dependencies"]
        if not isinstance(deps, list) or len(deps) > _MAX_NODES:
            raise ValueError("dependencies must be a bounded list")
        for dep in deps:
            validate_id(dep)
        if len(set(deps)) != len(deps):
            raise ValueError("duplicate dependency")
        graph[node_id] = sorted(deps)
    if sum(map(len, graph.values())) > 10000:
        raise ValueError("workflow has too many dependencies")
    if any(dep not in graph for deps in graph.values() for dep in deps):
        raise ValueError("unknown dependency")
    try:
        TopologicalSorter(graph).prepare()
    except CycleError as exc:
        raise ValueError("workflow contains a dependency cycle") from exc
    return [{"id": node_id, "dependencies": graph[node_id]} for node_id in sorted(graph)]


def _canonical(nodes: list[dict]) -> str:
    return json.dumps({"nodes": nodes}, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _event(connection: sqlite3.Connection, *, aggregate_type: str, aggregate_id: str,
           run_id: str | None, event_type: str, payload: dict) -> None:
    event = EventEnvelope.create(aggregate_type=aggregate_type, aggregate_id=aggregate_id,
                                 event_type=event_type, payload=payload)
    connection.execute("INSERT INTO events(id,aggregate_type,aggregate_id,run_id,type,schema_version,payload_json,occurred_at) "
                       "VALUES(?,?,?,?,?,?,?,?)", (event.id, aggregate_type, aggregate_id, run_id,
                        event_type, event.schema_version, encode_payload(event.payload), event.occurred_at))
    connection.execute("INSERT INTO outbox(event_id) VALUES(?)", (event.id,))


def publish_workflow(connection: sqlite3.Connection, *, workflow_id: str, version: str,
                     nodes: list[dict], principal: str, idempotency_key: str) -> CommandResult:
    """Publish one immutable version. Caller authenticates and authorizes principal."""
    validate_id(workflow_id)
    validate_id(version)
    normalized = _definition(nodes)
    definition = _canonical(normalized)
    digest = hashlib.sha256(definition.encode("utf-8")).hexdigest()
    request = {"workflow_id": workflow_id, "version": version, "nodes": normalized}
    def write(db):
        db.execute("INSERT INTO workflow_versions(workflow_id,version,definition_json,definition_sha256,published_at) VALUES(?,?,?,?,?)",
                   (workflow_id, version, definition, digest, datetime.now(timezone.utc).isoformat()))
        _event(db, aggregate_type="workflow", aggregate_id=workflow_id, run_id=None,
               event_type="workflow.published", payload={"version": version, "definition_sha256": digest, "principal": principal})
        return {"workflow_id": workflow_id, "version": version, "definition_sha256": digest}
    return execute_idempotent(connection, principal=principal, operation="workflow.publish", key=idempotency_key,
                              request=request, execute=write)


def create_run(connection: sqlite3.Connection, *, workflow_id: str, version: str,
               principal: str, idempotency_key: str) -> CommandResult:
    """Atomically create a queued run and its task/dependency graph."""
    validate_id(workflow_id)
    validate_id(version)
    def write(db):
        row = db.execute("SELECT definition_json,definition_sha256 FROM workflow_versions WHERE workflow_id=? AND version=?",
                         (workflow_id, version)).fetchone()
        if row is None:
            raise ValueError("workflow version does not exist")
        encoded, digest = row
        if not isinstance(encoded, str) or hashlib.sha256(encoded.encode("utf-8")).hexdigest() != digest:
            raise ValueError("workflow definition hash mismatch")
        value = json.loads(encoded)
        if not isinstance(value, dict) or set(value) != {"nodes"}:
            raise ValueError("unsupported workflow definition")
        nodes = _definition(value["nodes"])
        run_id = new_id()
        task_ids = {node["id"]: new_id() for node in nodes}
        now = datetime.now(timezone.utc).isoformat()
        db.execute("INSERT INTO runs(id,workflow_id,workflow_version,state,state_version,created_at,updated_at) VALUES(?,?,?,'created',0,?,?)",
                   (run_id, workflow_id, version, now, now))
        _event(db, aggregate_type="run", aggregate_id=run_id, run_id=run_id, event_type="run.created",
               payload={"workflow_id": workflow_id, "workflow_version": version, "definition_sha256": digest, "principal": principal})
        for node in nodes:
            task_id = task_ids[node["id"]]
            db.execute("INSERT INTO tasks(id,run_id,node_id,state,state_version,created_at,updated_at) VALUES(?,?,?,'created',0,?,?)",
                       (task_id, run_id, node["id"], now, now))
        for node in nodes:
            task_id = task_ids[node["id"]]
            for dependency in node["dependencies"]:
                db.execute("INSERT INTO task_dependencies(task_id,depends_on_id) VALUES(?,?)", (task_id, task_ids[dependency]))
            _event(db, aggregate_type="task", aggregate_id=task_id, run_id=run_id, event_type="task.created",
                   payload={"node_id": node["id"], "dependencies": [task_ids[d] for d in node["dependencies"]]})
        write_run_transition(db, RunChange(RunId(run_id), RunState.CREATED, RunState.QUEUED, 0), principal=principal)
        return {"run_id": run_id, "state": "queued", "tasks": task_ids}
    return execute_idempotent(connection, principal=principal, operation="run.create", key=idempotency_key,
                              request={"workflow_id": workflow_id, "version": version}, execute=write)
