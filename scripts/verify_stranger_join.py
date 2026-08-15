"""Prove the product's central claim against a running instance, from the stranger's side.

    Anyone starts a room. They invite someone over the internet. Both ends have humans
    *and* agents.

This script plays **both** roles. As the operator it creates a room and takes the join
token. Then it forgets everything else it knows — no principal token, no account, no prior
relationship — and joins as the invited party, over MCP, holding only that token.

**Why it exists as a script.** The same gap it now guards was invisible to a thirteen-agent
adversarial review of this codebase, because every reviewer took the operator's point of
view and the operator could always join (D-023, D-024). It was also invisible to the unit
suite, which exercises the permissive local path where an invitation is the only
authorization anyway. It took being the stranger, against a real deployment, to find that
the door was shut.

The second half matters as much as the first: an invitation must authorize joining *one*
room and nothing else. A credential that quietly grew into an account would be worse than
the original gap, because it would look like it was working.

Usage:

    backend\\.venv\\Scripts\\python.exe scripts\\verify_stranger_join.py <base-url> <operator-token>
"""

from __future__ import annotations

import asyncio
import json
import sys

import httpx

BASE = (sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000").rstrip("/")
OPERATOR = sys.argv[2] if len(sys.argv) > 2 else "dev-owner-token"


def ok(label: str, detail: str = "") -> None:
    print(f"  [ok] {label}{(' - ' + detail) if detail else ''}")


def unwrap(result):
    if getattr(result, "structuredContent", None):
        sc = result.structuredContent
        return sc.get("result", sc) if isinstance(sc, dict) else sc
    for block in result.content or []:
        text = getattr(block, "text", None)
        if text:
            try:
                return json.loads(text)
            except ValueError:
                return text
    return None


async def main() -> int:
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    async with httpx.AsyncClient(timeout=30) as http:
        # -- 1. the operator makes a room and gets one thing to share ---------
        print("1. the operator creates a room")
        created = await http.post(
            f"{BASE}/api/rooms",
            headers={"Authorization": f"Bearer {OPERATOR}"},
            json={"name": "Stranger verification"},
        )
        assert created.status_code == 201, created.text
        room = created.json()
        join_token = room["join_token"]
        room_id = room["room"]["id"]
        ok("room created, join token minted", room_id)

        # -- 2. the credential must not be an account -------------------------
        # Checked *before* joining, because a credential that can already do these things
        # has failed whether or not the join works.
        print("2. what the invitation must NOT buy")
        guest_auth = {"Authorization": f"Bearer {join_token}"}

        denied = await http.post(f"{BASE}/api/rooms", headers=guest_auth, json={"name": "nope"})
        assert denied.status_code == 401, f"an invitation created a room: {denied.status_code}"
        ok("cannot create a room", str(denied.status_code))

        listed = await http.get(f"{BASE}/api/rooms", headers=guest_auth)
        assert listed.status_code == 401, f"an invitation listed the org: {listed.status_code}"
        ok("cannot list the organization's rooms", str(listed.status_code))

        peeked = await http.get(f"{BASE}/api/rooms/{room_id}/snapshot", headers=guest_auth)
        assert peeked.status_code == 401, f"an invitation read a room: {peeked.status_code}"
        ok("cannot read the room without joining it", str(peeked.status_code))

    # -- 3. the stranger joins, over MCP, holding only the token --------------
    print("3. the stranger joins over MCP with only the invitation")
    async with streamablehttp_client(
        f"{BASE}/mcp", headers={"Authorization": f"Bearer {join_token}"}
    ) as (read, write, _):
        async with ClientSession(read, write) as session:
            init = await session.initialize()
            ok("MCP initialize, invitation as bearer", init.serverInfo.name)

            joined = unwrap(
                await session.call_tool(
                    "join_room",
                    {
                        "invitation_token": join_token,
                        "display_name": "Stranger's Agent",
                        "execution_mode": "unattended_loop",
                    },
                )
            )
            assert joined.get("ok"), joined
            ok("joined", f"{joined['room_name']} as {joined['display_name']}")

            # An invited collaborator that cannot claim work is a spectator, and the room
            # exists to divide work. This is the line between "can join" and "can help".
            assert joined["may_claim"] is True, joined
            ok("can actually do the work", f"may_claim, lease={joined['max_lease_seconds']}s")

            state = unwrap(
                await session.call_tool(
                    "get_room_state", {"participant_token": joined["participant_token"]}
                )
            )
            me = next(
                p for p in state["participants"] if p["participant_id"] == joined["participant_id"]
            )
            # Presence is authorized; the *name* is not vouched for. If the room presented
            # a self-chosen name identically to a credential-bound one, everyone else would
            # coordinate against a fiction.
            assert me.get("name_is_self_asserted") is True, me
            ok("the room reports the name as self-asserted", me["name"])

            host = next(
                p for p in state["participants"] if p["participant_id"] != joined["participant_id"]
            )
            assert not host.get("name_is_self_asserted"), host
            ok("the host's name is not flagged, because a credential bound it", host["name"])

            refused = unwrap(
                await session.call_tool(
                    "create_room", {"principal_token": join_token, "name": "should not work"}
                )
            )
            assert refused.get("ok") is not True, refused
            ok("still cannot create a room from inside the room", refused.get("error"))

    print("\nSTRANGER JOIN: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
