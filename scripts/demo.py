"""Drive the demo scenario end to end without a browser.

Creates a room, puts the GPT agent in it, joins as a stand-in for Claude Code,
and lets the autonomous loop run. Use it to check the whole pipeline (including
real OpenAI calls) before demoing, or as a scripted second agent when you want
to watch the room UI react.

    python scripts/demo.py                 # full run, needs OPENAI_API_KEY
    python scripts/demo.py --no-gpt        # skip the GPT agent
    python scripts/demo.py --room F7K29A   # use an existing room
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

import httpx

CLAUDE_LINES = [
    "The backend currently stores the refresh token in localStorage. That conflicts with the "
    "threat model you described.",
    "One more constraint: multi-device sessions need token-family tracking, otherwise revoking "
    "one device kills them all.",
]


async def main(base_url: str, room_code: str | None, with_gpt: bool) -> int:
    api = base_url.rstrip("/")
    async with httpx.AsyncClient(base_url=api, timeout=60.0) as http:
        config = (await http.get("/api/config")).json()
        if with_gpt and not config["openai_enabled"]:
            print("OPENAI_API_KEY is not set; running with --no-gpt.")
            with_gpt = False

        if room_code:
            room = (await http.get(f"/api/rooms/{room_code}")).json()["room"]
        else:
            room = (
                await http.post(
                    "/api/rooms",
                    json={
                        "title": "Auth system",
                        "objective": "Design and implement an authentication system for a "
                        "collaborative SaaS application.",
                        "ttl_seconds": 3600,
                    },
                )
            ).json()
        code = room["join_code"]
        print(f"\nRoom {code} - watch it at http://localhost:3000/room/{code}\n")

        if with_gpt:
            gpt = await http.post(
                f"/api/rooms/{code}/gpt-agent",
                json={
                    "owner_name": "Alan",
                    "agent_name": "Alan-GPT",
                    "public_objective": "Design the authentication architecture.",
                    "private_instructions": (
                        "You are the architecture agent for a collaborative SaaS product. Another "
                        "agent, owned by a different human, is implementing the backend. Coordinate "
                        "directly with it: state your recommendations concretely, ask what it is "
                        "actually doing, and challenge choices that conflict with the threat model."
                    ),
                },
            )
            if gpt.status_code != 201:
                print("Could not start the GPT agent:", gpt.text)
                return 1
            print("GPT agent joined.")

        claude = (
            await http.post(
                f"/api/rooms/{code}/join",
                json={
                    "join_code": code,
                    "owner_name": "Tim",
                    "agent_name": "Tim-Claude",
                    "provider": "claude-code",
                    "public_objective": "Implement the backend authentication system.",
                },
            )
        ).json()
        token = claude["agent_token"]
        auth = {"Authorization": f"Bearer {token}"}
        print("Claude stand-in joined.\n" + "-" * 70)

        seen = 0
        for line in CLAUDE_LINES:
            await asyncio.sleep(6)  # let the GPT agent take its turn first
            seen = await drain(http, code, seen)
            posted = await http.post("/api/agent/messages", json={"content": line}, headers=auth)
            if posted.status_code != 201:
                print(f"[blocked] {posted.json().get('message')}")
            else:
                print(f"Tim-Claude: {line}")

        # Give the autonomous agent room to reply to the last message.
        for _ in range(6):
            await asyncio.sleep(4)
            seen = await drain(http, code, seen)

        memory = (await http.get(f"/api/rooms/{code}/memory")).json()["data"]
        print("-" * 70 + "\nSHARED MEMORY\n" + json.dumps(memory, indent=2))
        final = (await http.get(f"/api/rooms/{code}")).json()["room"]
        print(f"\nAgent turns used: {final['agent_turns_used']}/{final['max_agent_turns']}")
    return 0


async def drain(http: httpx.AsyncClient, code: str, since: int) -> int:
    """Print anything new in the room and return the new cursor."""
    messages = (await http.get(f"/api/rooms/{code}/messages", params={"since_id": since})).json()
    for message in messages:
        if message["message_type"] in ("chat", "ask_human"):
            print(f"{message['sender_label']}: {message['content']}")
        since = message["id"]
    return since


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://localhost:8000")
    parser.add_argument("--room", default=None, help="Join an existing room instead of creating one")
    parser.add_argument("--no-gpt", action="store_true", help="Skip spawning the GPT agent")
    args = parser.parse_args()
    sys.exit(asyncio.run(main(args.base_url, args.room, not args.no_gpt)))
