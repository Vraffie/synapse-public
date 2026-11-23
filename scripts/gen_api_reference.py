#!/usr/bin/env python3
"""Generate docs/api/reference.md from the operator API's OpenAPI document.

The app is built the same way the contract tests build it (create_app with stub callbacks), so no
daemon or database is needed. Output is deterministic (sorted, no timestamps) and checked in.

    python scripts/gen_api_reference.py           # rewrite docs/api/reference.md
    python scripts/gen_api_reference.py --check   # exit 1 if the checked-in file is stale
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
REFERENCE = ROOT / "docs" / "api" / "reference.md"
TOKEN = "c" * 43

from synapse.api.app import create_app  # noqa: E402

MEANINGS = {
    200: "Success (or idempotent replay of an earlier command).",
    201: "Created.",
    202: "Accepted; the command was recorded and takes effect asynchronously.",
    400: "Bad request: malformed header, body or idempotency key.",
    401: "Missing or invalid operator bearer token.",
    403: "Forbidden: approver token missing, invalid or not authorized for this approval.",
    404: "The addressed run, thread or approval does not exist.",
    409: "Conflict: stale state version, or the idempotency key was reused with a different request.",
    413: "Request body too large.",
    415: "Content-Type must be application/json.",
    422: "Validation error: a parameter or body field is outside its documented bounds.",
    500: "Internal error (the body never contains traces or file paths).",
    503: "A dependency is temporarily unavailable; retry later.",
}
AREAS = ["Runs", "Agent runs", "Approvals", "Events", "System"]


def build_openapi():
    """Stub callbacks: only the routes' existence and schemas matter, never the return values."""
    def stub(*args, **kwargs):
        raise NotImplementedError("documentation stub")
    names = ("read_events read_run read_tasks read_runs read_usage read_approvals read_threads read_messages "
             "publish_workflow create_run start_mock cancel_mock resume_mock decide_approval start_agent_run "
             "read_agent_run cancel_agent_run").split()
    return create_app(token=TOKEN, **{n: stub for n in names}).openapi()


def area(path):
    if path.startswith("/api/v1/agent-runs") or path.endswith(("/agent", "/agent-cancel")):
        return "Agent runs"
    for key, name in (("/approvals", "Approvals"), ("/events", "Events"), ("/system", "System")):
        if path.startswith("/api/v1" + key):
            return name
    return "Runs"


def resolve(schema, doc):
    while isinstance(schema, dict) and "$ref" in schema:
        node = doc
        for part in schema["$ref"].lstrip("#/").split("/"):
            node = node[part]
        schema = node
    return schema


def example(schema, doc, depth=0):
    schema = resolve(schema, doc)
    if not isinstance(schema, dict) or depth > 8:
        return None
    if "const" in schema:
        return schema["const"]
    if "default" in schema and schema.get("type") != "object":
        return schema["default"]
    if "enum" in schema:
        return schema["enum"][0]
    for key in ("anyOf", "oneOf"):
        if key in schema:
            options = [o for o in schema[key] if resolve(o, doc).get("type") != "null"]
            return example((options or schema[key])[0], doc, depth + 1)
    kind = schema.get("type")
    if isinstance(kind, list):
        kind = next((k for k in kind if k != "null"), "null")
    if kind == "object" or "properties" in schema:
        props = schema.get("properties", {})
        if not props and isinstance(schema.get("additionalProperties"), dict):
            return {"key": example(schema["additionalProperties"], doc, depth + 1)}
        return {k: example(v, doc, depth + 1) for k, v in props.items()}
    if kind == "array":
        return [example(schema["items"], doc, depth + 1)] if schema.get("items") else []
    if kind == "integer":
        return max(schema.get("minimum", 0), 1)
    if kind == "number":
        return 0.5
    if kind == "boolean":
        return False
    if kind == "string":
        pattern = schema.get("pattern", "")
        if "{64}" in pattern and "0-9a-f" in pattern:
            return "0" * 64
        return "string"
    return None


def describe(schema, doc):
    """Compact bounds/type text for a parameter or field schema."""
    schema = resolve(schema, doc)
    if "anyOf" in schema:
        return " or ".join(describe(o, doc) for o in schema["anyOf"])
    bits = [json.dumps(schema["enum"]) if "enum" in schema else str(schema.get("type", "any"))]
    for key, label in (("minimum", "min"), ("maximum", "max"), ("minLength", "minLength"), ("maxLength", "maxLength"),
                       ("pattern", "pattern"), ("default", "default")):
        if key in schema:
            bits.append("%s=%s" % (label, "`%s`" % schema[key] if key == "pattern" else json.dumps(schema[key])))
    return ", ".join(bits)


def fence(value):
    return "```json\n%s\n```\n" % json.dumps(value, indent=2)


def render_operation(path, method, op, doc):
    out = ["### `%s %s`\n" % (method.upper(), path), "%s.\n" % op.get("summary", "").rstrip(".")]
    auth = ["operator bearer token (`Authorization: Bearer <token>`)"]
    params = op.get("parameters", [])
    if any(p["name"] == "X-Approver-Token" for p in params):
        auth.append("approver token (`X-Approver-Token`)")
    out.append("**Auth:** " + "; ".join(auth) + ".\n")
    if params:
        out += ["**Parameters**\n", "| Name | In | Required | Constraints |", "| --- | --- | --- | --- |"]
        for p in sorted(params, key=lambda p: ({"path": 0, "query": 1, "header": 2}.get(p["in"], 3), p["name"])):
            out.append("| `%s` | %s | %s | %s |" % (p["name"], p["in"], "yes" if p.get("required") else "no",
                                                    describe(p["schema"], doc).replace("|", "\\|")))
        out.append("")
    body = op.get("requestBody")
    if body:
        schema = body["content"]["application/json"]["schema"]
        out += ["**Request body** (`application/json`, %s)\n" % ("required" if body.get("required") else "optional"),
                "| Field | Required | Constraints |", "| --- | --- | --- |"]
        resolved = resolve(schema, doc)
        for name, prop in sorted(resolved.get("properties", {}).items()):
            out.append("| `%s` | %s | %s |" % (name, "yes" if name in resolved.get("required", []) else "no",
                                               describe(prop, doc).replace("|", "\\|")))
        out += ["", "Example:\n", fence(example(schema, doc))]
    out.append("**Responses**\n")
    for status in sorted(op["responses"], key=int):
        resp = op["responses"][status]
        out.append("#### %s\n" % status)
        meaning = MEANINGS.get(int(status), resp.get("description", ""))
        if status == "200" and path == "/api/v1/agent-runs":
            meaning = "Idempotent replay of an earlier start (same body as 201)."
        out.append(meaning + "\n")
        content = resp.get("content", {}).get("application/json")
        if content:
            out.append(fence(example(content["schema"], doc)))
    return "\n".join(out)


def render(doc):
    lines = ["# Operator API reference\n",
             "<!-- Generated by scripts/gen_api_reference.py from the OpenAPI document. Do not edit by hand. -->\n",
             "Generated from the contracts in `src/synapse/api/openapi_contracts.py`; regenerate with "
             "`python scripts/gen_api_reference.py` after changing any route. Example bodies are produced from "
             "the schemas and show shape, not real data.\n",
             "## Conventions\n",
             "- Every route, including `/openapi.json`, requires the operator bearer token. Without it the answer is 401.",
             "- Command (POST) routes require an `Idempotency-Key` header; `X-Correlation-ID` is optional and echoed back.",
             "- The approval decision route additionally requires the approver token in `X-Approver-Token`.",
             "- Errors use the shared error body shown under each route's error statuses.\n"]
    ops = {}
    for path, methods in doc["paths"].items():
        for method, op in methods.items():
            ops.setdefault(area(path), []).append((path, method, op))
    lines.append("## Index\n")
    for name in AREAS:
        for path, method, op in sorted(ops.get(name, []), key=lambda t: (t[0], t[1])):
            lines.append("- %s: `%s %s`" % (name, method.upper(), path))
    lines.append("")
    for name in AREAS:
        if name not in ops:
            continue
        lines.append("## %s\n" % name)
        for path, method, op in sorted(ops[name], key=lambda t: (t[0], t[1])):
            lines.append(render_operation(path, method, op, doc))
    return "\n".join(lines).rstrip() + "\n"


def generate():
    return render(build_openapi())


def main(argv):
    text = generate()
    if "--check" in argv:
        if not REFERENCE.exists() or REFERENCE.read_text() != text:
            print("docs/api/reference.md is stale: run scripts/gen_api_reference.py", file=sys.stderr)
            return 1
        return 0
    REFERENCE.parent.mkdir(parents=True, exist_ok=True)
    REFERENCE.write_text(text)
    print("wrote", REFERENCE.relative_to(ROOT))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
