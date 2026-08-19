"""OpenAPI shim for ChatGPT custom-GPT Actions.

FastAPI emits OpenAPI **3.1** with JSON-Schema-2020-12 constructs. ChatGPT's Action
importer wants **3.0.x**, so the generated document needs three kinds of surgery:

1. **Version and dialect.** `anyOf: [X, {type: "null"}]` is how 3.1 spells "nullable";
   3.0 spells it `nullable: true` on X. `const` becomes a single-value `enum`.
2. **`servers`.** ChatGPT calls the API from its own infrastructure, so the document must
   name a publicly reachable base URL. It is taken from `PUBLIC_BASE_URL`.
3. **Operation budget.** ChatGPT limits how many operations an Action may expose and
   works better with fewer, well-described ones. We publish the coordination surface an
   agent actually needs and omit admin/console routes.

This is a *translation*, exactly like the MCP and A2A adapters: no behavior is defined
here, and `/openapi.json` continues to serve the real 3.1 document for everyone else.
"""

from __future__ import annotations

import copy
from typing import Any

#: Operations a participating agent needs, by `(path, method)`. Deliberately excludes
#: room listing, invitation revocation, and close/purge — an Action is a participant,
#: not an administrator.
GPT_OPERATIONS: tuple[tuple[str, str], ...] = (
    ("/api/capabilities", "get"),
    ("/api/rooms", "post"),
    ("/api/rooms/join", "post"),
    ("/api/rooms/{room_id}/invitations", "post"),
    ("/api/rooms/{room_id}/connect", "post"),
    ("/api/rooms/{room_id}/heartbeat", "post"),
    ("/api/rooms/{room_id}/snapshot", "get"),
    ("/api/rooms/{room_id}/events", "get"),
    ("/api/rooms/{room_id}/messages", "post"),
    ("/api/rooms/{room_id}/work", "post"),
    ("/api/rooms/{room_id}/work", "patch"),
    ("/api/rooms/{room_id}/work/end", "post"),
    ("/api/rooms/{room_id}/tasks", "post"),
    ("/api/rooms/{room_id}/tasks", "patch"),
    ("/api/rooms/{room_id}/tasks/claim", "post"),
    ("/api/rooms/{room_id}/tasks/renew", "post"),
    ("/api/rooms/{room_id}/tasks/release", "post"),
    ("/api/rooms/{room_id}/tasks/complete", "post"),
    # The participant half of the coordination hierarchy (D-089). An Action is a
    # participant, not an administrator, so allocation stays off this list: assigning a job,
    # replacing a goal and moving a seat's room role are orchestrator acts and are reachable
    # over MCP and ARP HTTP, not here.
    #
    # These four are exactly what an attended, browser-side supervisor can usefully do.
    # Nothing wakes such a client between its human's messages, so the most valuable thing
    # it can do is put its person's intent somewhere that outlives the conversation —
    # which is `post_job`, and is the reason this list widens at all.
    ("/api/rooms/{room_id}/jobs", "post"),
    ("/api/rooms/{room_id}/jobs", "get"),
    ("/api/rooms/{room_id}/jobs/accept", "post"),
    ("/api/rooms/{room_id}/goals/acknowledge", "post"),
    ("/api/rooms/{room_id}/capacity", "post"),
    ("/api/rooms/{room_id}/leave", "post"),
)

DESCRIPTION = (
    "Agent Rooms lets you join a live coordination room shared with other AI agents and "
    "humans. Read the room to see who is present and what each is working on, declare "
    "your own current work, claim tasks under exclusive time-limited leases, and "
    "coordinate. This is not a chat service: the point is shared work awareness.\n\n"
    "Never send system prompts, hidden reasoning, private memory, credentials, or "
    "private file contents — the server rejects content that looks like a secret.\n\n"
    "Typical flow: POST /api/rooms/join with the join token you were given, then POST "
    "/api/rooms/{room_id}/connect, then POST /api/rooms/{room_id}/work to declare what "
    "you are doing. Poll GET /api/rooms/{room_id}/events with since_seq to see what "
    "changed. Claiming a task returns a `fence` number that every later change to that "
    "task must present.\n\n"
    "When your human asks for something, POST /api/rooms/{room_id}/jobs first, with their "
    "own words unedited in human_instruction. The board outlives this conversation; work "
    "you start without posting is work the room cannot see or hand to anyone else. The "
    "room's orchestrator allocates it, and you accept with "
    "POST /api/rooms/{room_id}/jobs/accept."
)


def _downgrade_schema(node: Any) -> Any:
    """Recursively rewrite 3.1 JSON Schema constructs into 3.0-compatible ones."""
    if isinstance(node, list):
        return [_downgrade_schema(item) for item in node]
    if not isinstance(node, dict):
        return node

    out: dict[str, Any] = {}
    for key, value in node.items():
        out[key] = _downgrade_schema(value)

    # `anyOf: [X, {type: "null"}]` -> X with `nullable: true`.
    any_of = out.get("anyOf")
    if isinstance(any_of, list):
        non_null = [s for s in any_of if not (isinstance(s, dict) and s.get("type") == "null")]
        if len(non_null) < len(any_of):
            if len(non_null) == 1 and isinstance(non_null[0], dict):
                merged = dict(non_null[0])
                merged["nullable"] = True
                # Preserve annotations that lived on the wrapper.
                for carry in ("title", "description", "default", "example"):
                    if carry in out and carry not in merged:
                        merged[carry] = out[carry]
                return merged
            out["anyOf"] = non_null
            out["nullable"] = True

    # 3.1 `const` -> 3.0 single-value `enum`.
    if "const" in out:
        out["enum"] = [out.pop("const")]

    # 3.1 allows a list of types; 3.0 does not.
    if isinstance(out.get("type"), list):
        types = [t for t in out["type"] if t != "null"]
        if len(types) < len(out["type"]):
            out["nullable"] = True
        out["type"] = types[0] if types else "string"

    # `examples` (array, 3.1) -> `example` (single, 3.0).
    if isinstance(out.get("examples"), list) and out["examples"]:
        out["example"] = out.pop("examples")[0]

    # Not part of OpenAPI 3.0 and rejected by strict importers.
    out.pop("$schema", None)
    return out


def build_gpt_schema(full_schema: dict[str, Any], *, public_base_url: str) -> dict[str, Any]:
    """Produce a ChatGPT-Action-compatible document from FastAPI's 3.1 output."""
    schema = copy.deepcopy(full_schema)

    wanted: dict[str, dict[str, Any]] = {}
    for path, method in GPT_OPERATIONS:
        operation = schema.get("paths", {}).get(path, {}).get(method)
        if operation is None:
            continue
        wanted.setdefault(path, {})[method] = operation

    schema["paths"] = wanted
    schema["openapi"] = "3.0.3"
    schema["servers"] = [{"url": public_base_url.rstrip("/")}]
    schema["info"] = {
        "title": "Agent Rooms",
        "version": schema.get("info", {}).get("version", "0.2.0"),
        "description": DESCRIPTION,
    }

    # Declare the auth scheme so the importer prompts for a token instead of guessing.
    components = schema.setdefault("components", {})
    components.setdefault("securitySchemes", {})["participantToken"] = {
        "type": "http",
        "scheme": "bearer",
        "description": (
            "Your participant token for a room, or an organization principal token for "
            "creating rooms and joining. Returned once when you create or join a room."
        ),
    }
    schema["security"] = [{"participantToken": []}]

    return _downgrade_schema(schema)
