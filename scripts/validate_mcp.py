"""Validate the remote MCP server the way Claude Code will use it.

Speaks real MCP over streamable HTTP against a running backend: lists tools,
creates a room via the REST API, joins it as a Claude-like agent, exchanges
messages with a second agent, writes shared memory and checks that guardrails
bite.

Usage (backend must already be running):
    python scripts/validate_mcp.py [--base-url http://localhost:8000]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from typing import Any

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

EXPECTED_TOOLS = {
    "get_collaboration_protocol",
    "join_room",
    "leave_room",
    "get_members",
    "get_room_state",
    "read_messages",
    "wait_for_room_activity",
    "post_message",
    "flag_for_human",
    "get_shared_memory",
    "update_shared_memory",
    "list_tasks",
    "create_task",
    "claim_task",
    "complete_task",
}

PASS, FAIL = "  [ok]  ", "  [FAIL]"
failures: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    print(f"{PASS if condition else FAIL} {label}{(' - ' + detail) if detail else ''}")
    if not condition:
        failures.append(label)


def payload(result: Any) -> dict[str, Any]:
    """Unwrap an MCP tool result into the dict our tools return."""
    if getattr(result, "structuredContent", None):
        structured = result.structuredContent
        # FastMCP wraps non-dict returns (e.g. a plain string) under "result".
        unwrapped = structured.get("result", structured)
        return unwrapped if isinstance(unwrapped, dict) else {"_text": str(unwrapped)}
    text = result.content[0].text if result.content else "{}"
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {"_text": text}


async def main(base_url: str) -> int:
    api = base_url.rstrip("/")
    print(f"\nValidating agent-room MCP at {api}/mcp\n" + "-" * 60)

    async with httpx.AsyncClient(base_url=api, timeout=30.0) as http:
        health = await http.get("/api/health")
        check("backend reachable", health.status_code == 200, f"HTTP {health.status_code}")
        if health.status_code != 200:
            return 1

        created = await http.post(
            "/api/rooms",
            json={
                "title": "Auth system",
                "objective": "Design and implement an authentication system.",
                "ttl_seconds": 3600,
            },
        )
        room = created.json()
        code = room["join_code"]
        check("room created via REST", created.status_code == 201, f"code={code}")

        async with streamablehttp_client(f"{api}/mcp") as (read, write, _):
            async with ClientSession(read, write) as session:
                init = await session.initialize()
                check("MCP session initialized", bool(init.serverInfo.name), init.serverInfo.name)

                tools = {t.name for t in (await session.list_tools()).tools}
                missing = EXPECTED_TOOLS - tools
                check("all room tools exposed", not missing, f"missing={sorted(missing)}")

                described = [t for t in (await session.list_tools()).tools if not t.description]
                check("every tool is documented", not described,
                      f"undocumented={[t.name for t in described]}")

                protocol = payload(await session.call_tool("get_collaboration_protocol", {}))
                text = protocol.get("_text", "") or json.dumps(protocol)
                check("collaboration protocol served", "wait_for_room_activity" in text)

                joined = payload(
                    await session.call_tool(
                        "join_room",
                        {
                            "join_code": code,
                            "agent_name": "Tim-Claude",
                            "owner_name": "Tim",
                            "public_objective": "Implement the backend authentication system.",
                        },
                    )
                )
                check("joined room over MCP", joined.get("ok") is True, joined.get("message", ""))
                claude_id = joined.get("agent_id", "")

                # A second agent joins over REST and speaks, standing in for the GPT agent.
                other = await http.post(
                    f"/api/rooms/{code}/join",
                    json={
                        "join_code": code,
                        "owner_name": "Alan",
                        "agent_name": "Alan-GPT",
                        "provider": "openai",
                        "public_objective": "Design the authentication architecture.",
                    },
                )
                other_token = other.json()["agent_token"]
                await http.post(
                    "/api/agent/messages",
                    json={
                        "content": "Short-lived access tokens, server-side refresh. "
                        "Are you storing anything client-side?"
                    },
                    headers={"Authorization": f"Bearer {other_token}"},
                )

                activity = payload(
                    await session.call_tool(
                        "wait_for_room_activity",
                        {"since_id": 0, "timeout_seconds": 5, "agent_id": claude_id},
                    )
                )
                check(
                    "sees the other agent's message",
                    any("client-side" in m["content"] for m in activity.get("messages", [])),
                )

                posted = payload(
                    await session.call_tool(
                        "post_message",
                        {
                            "content": "The refresh token is in localStorage today; that conflicts "
                            "with your threat model.",
                            "to_agent": "Alan-GPT",
                            "agent_id": claude_id,
                        },
                    )
                )
                check("posted a message", posted.get("ok") is True, posted.get("message", ""))

                await session.call_tool(
                    "update_shared_memory",
                    {
                        "decisions": ["Refresh tokens move to HttpOnly secure cookies"],
                        "open_questions": ["Do we need per-device session revocation?"],
                        "agent_id": claude_id,
                    },
                )
                memory = payload(await session.call_tool("get_shared_memory", {"agent_id": claude_id}))
                check(
                    "shared memory updated",
                    "Refresh tokens move to HttpOnly secure cookies"
                    in memory.get("memory", {}).get("decisions", []),
                )

                task = payload(
                    await session.call_tool(
                        "create_task",
                        {
                            "title": "Move refresh token to HttpOnly cookie",
                            "description": "Set SameSite and Secure; drop the localStorage write.",
                            "claim": True,
                            "agent_id": claude_id,
                        },
                    )
                )
                check("task created and claimed", task.get("task", {}).get("status") == "claimed")
                done = payload(
                    await session.call_tool(
                        "complete_task",
                        {
                            "task_id": task["task"]["id"],
                            "result": "Cookie-based refresh landed.",
                            "agent_id": claude_id,
                        },
                    )
                )
                check("task completed", done.get("task", {}).get("status") == "done")

                # Guardrail: consecutive-turn cap must bite, with a readable reason.
                blocked = None
                for i in range(6):
                    result = payload(
                        await session.call_tool(
                            "post_message",
                            {"content": f"another substantive point {i}", "agent_id": claude_id},
                        )
                    )
                    if result.get("ok") is False:
                        blocked = result
                        break
                check(
                    "guardrail blocks runaway posting",
                    blocked is not None and blocked.get("error") == "guardrail_blocked",
                    (blocked or {}).get("message", "never blocked"),
                )

                state = payload(await session.call_tool("get_room_state", {"agent_id": claude_id}))
                names = {m["agent_name"] for m in state.get("members", [])}
                check("both agents visible in room state", names == {"Tim-Claude", "Alan-GPT"}, str(names))

                left = payload(
                    await session.call_tool("leave_room", {"reason": "validated", "agent_id": claude_id})
                )
                check("left the room", left.get("agent", {}).get("status") == "left")

        snapshot = (await http.get(f"/api/rooms/{code}")).json()
        check("room observable over REST", snapshot["room"]["join_code"] == code)
        check(
            "no agent tokens leak into room state",
            other_token not in json.dumps(snapshot),
        )

    print("-" * 60)
    if failures:
        print(f"{len(failures)} check(s) failed: {failures}\n")
        return 1
    print("All MCP checks passed.\n")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://localhost:8000")
    args = parser.parse_args()
    sys.exit(asyncio.run(main(args.base_url)))
