"""Integration: agents talking to a room, end to end.

Covers the two paths a real demo uses:
  * the MCP tool surface (what Claude Code calls),
  * the autonomous runner (what drives the GPT agent),
and the HTTP API the browser uses to observe both.

No OpenAI key is required: the autonomous agent here is a scripted RoomAgent, so
the test exercises our wake/decide/guardrail machinery rather than the model.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from app import mcp_server
from app.agents.base import Decision, RoomAgent
from app.agents.runner import runner
from app.config import settings
from app.models import CreateRoomRequest, JoinRoomRequest, MemoryPatch
from app.services import rooms

pytestmark = pytest.mark.asyncio


class ScriptedAgent(RoomAgent):
    """A RoomAgent whose decisions come from a list instead of a model."""

    def __init__(self, agent, decisions: list[Decision]) -> None:
        super().__init__(agent, private_instructions="never leaves this object")
        self.decisions = list(decisions)
        self.wake_count = 0
        self.seen_contexts: list = []

    async def decide_action(self, context, trigger) -> Decision:
        self.wake_count += 1
        self.seen_contexts.append(context)
        if not self.decisions:
            return Decision(action="IGNORE", reason="nothing scripted")
        return self.decisions.pop(0)


async def wait_until(predicate, timeout: float = 6.0, interval: float = 0.1) -> bool:
    """Poll until `predicate()` is true. Autonomous turns are debounced, so tests
    must wait for them rather than assume they already happened."""
    elapsed = 0.0
    while elapsed < timeout:
        if await predicate() if asyncio.iscoroutinefunction(predicate) else predicate():
            return True
        await asyncio.sleep(interval)
        elapsed += interval
    return False


# --------------------------------------------------------------------------
# MCP surface - what Claude Code actually calls
# --------------------------------------------------------------------------


async def test_claude_code_joins_and_collaborates_over_mcp(fresh_db):
    mcp_server.reset_sessions()

    room = await rooms.create_room(
        CreateRoomRequest(
            title="Auth system",
            objective="Design and implement an authentication system.",
            ttl_seconds=3600,
        )
    )

    protocol = await mcp_server.get_collaboration_protocol()
    assert "wait_for_room_activity" in protocol
    assert "Silence is the default".lower() in protocol.lower()

    joined = await mcp_server.join_room(
        join_code=room.join_code,
        agent_name="Tim-Claude",
        owner_name="Tim",
        public_objective="Implement the backend authentication system.",
    )
    assert joined["ok"] is True
    claude_id = joined["agent_id"]
    assert joined["room"]["join_code"] == room.join_code

    # The other human's agent joins and says something worth answering.
    gpt = (
        await rooms.join_room(
            JoinRoomRequest(
                join_code=room.join_code,
                owner_name="Alan",
                agent_name="Alan-GPT",
                provider="openai",
                public_objective="Design the authentication architecture.",
            )
        )
    ).agent
    await rooms.post_message(
        agent=gpt,
        content="I recommend server-side refresh tokens. Are you storing anything client-side?",
    )

    # Claude sees it without polling because it is already there.
    activity = await mcp_server.wait_for_room_activity(
        since_id=0, timeout_seconds=2.0, agent_id=claude_id
    )
    assert activity["timed_out"] is False
    assert any("client-side" in m["content"] for m in activity["messages"])
    assert activity["you_may_post"] is True

    posted = await mcp_server.post_message(
        content="The initial implementation stores the refresh token in localStorage. "
        "That is inconsistent with your threat model.",
        to_agent="Alan-GPT",
        agent_id=claude_id,
    )
    assert posted["ok"] is True
    assert posted["turns_remaining"] == settings.max_room_agent_turns - 2

    # ...and records the outcome in shared memory rather than only in chat.
    await mcp_server.update_shared_memory(
        decisions=["Refresh tokens move to HttpOnly secure cookies"],
        open_questions=["Do we need per-device session revocation?"],
        agent_id=claude_id,
    )
    memory = await mcp_server.get_shared_memory(agent_id=claude_id)
    assert "Refresh tokens move to HttpOnly secure cookies" in memory["memory"]["decisions"]

    state = await mcp_server.get_room_state(agent_id=claude_id)
    assert {m["agent_name"] for m in state["members"]} == {"Tim-Claude", "Alan-GPT"}
    assert state["you"]["agent_name"] == "Tim-Claude"

    left = await mcp_server.leave_room(reason="backend scoped", agent_id=claude_id)
    assert left["agent"]["status"] == "left"


async def test_mcp_wait_times_out_quietly_when_room_is_silent(fresh_db):
    mcp_server.reset_sessions()

    room = await rooms.create_room(
        CreateRoomRequest(title="Quiet", objective="Nothing happening.", ttl_seconds=3600)
    )
    joined = await mcp_server.join_room(
        join_code=room.join_code, agent_name="Tim-Claude", owner_name="Tim"
    )
    latest = joined["recent_messages"][-1]["id"]

    result = await mcp_server.wait_for_room_activity(
        since_id=latest, timeout_seconds=0.5, agent_id=joined["agent_id"]
    )
    assert result["timed_out"] is True
    assert result["messages"] == []
    assert "call wait_for_room_activity again" in result["guidance"]


async def test_mcp_returns_actionable_errors_instead_of_crashing(fresh_db):
    mcp_server.reset_sessions()
    bad = await mcp_server.join_room(join_code="ZZZZZZ", agent_name="X", owner_name="Y")
    assert bad["ok"] is False
    assert bad["error"] == "not_found"
    assert "ZZZZZZ" in bad["message"]


async def test_mcp_post_is_guardrailed_like_every_other_path(fresh_db):
    mcp_server.reset_sessions()

    room = await rooms.create_room(
        CreateRoomRequest(title="Auth", objective="Design auth.", ttl_seconds=3600)
    )
    joined = await mcp_server.join_room(
        join_code=room.join_code, agent_name="Tim-Claude", owner_name="Tim"
    )
    agent_id = joined["agent_id"]

    for i in range(settings.max_consecutive_turns_per_agent):
        result = await mcp_server.post_message(content=f"substantive point {i}", agent_id=agent_id)
        assert result["ok"] is True

    blocked = await mcp_server.post_message(content="and another thing", agent_id=agent_id)
    assert blocked["ok"] is False
    assert blocked["error"] == "guardrail_blocked"
    assert "in a row" in blocked["message"]


# --------------------------------------------------------------------------
# Autonomy - an agent acting without its human prompting it
# --------------------------------------------------------------------------


async def test_agent_wakes_on_room_activity_and_responds_without_being_prompted(fresh_db):

    runner.start()
    room = await rooms.create_room(
        CreateRoomRequest(title="Auth", objective="Design auth.", ttl_seconds=3600)
    )
    gpt_agent = (
        await rooms.join_room(
            JoinRoomRequest(
                join_code=room.join_code,
                owner_name="Alan",
                agent_name="Alan-GPT",
                provider="openai",
                autonomous=True,
            )
        )
    ).agent
    claude_agent = (
        await rooms.join_room(
            JoinRoomRequest(
                join_code=room.join_code,
                owner_name="Tim",
                agent_name="Tim-Claude",
                provider="claude-code",
            )
        )
    ).agent

    scripted = ScriptedAgent(
        gpt_agent,
        [
            Decision(
                action="RESPOND",
                relevance=0.9,
                message="localStorage is readable by any XSS; move it to an HttpOnly cookie.",
                reason="challenges an unsafe assumption",
            )
        ],
    )
    runner.register(scripted)
    try:
        await rooms.post_message(
            agent=claude_agent, content="Refresh token is currently stored in localStorage."
        )

        async def gpt_replied() -> bool:
            messages = await rooms.read_messages(room.id)
            return any(m.agent_id == gpt_agent.id and "HttpOnly" in m.content for m in messages)

        assert await wait_until(gpt_replied), "autonomous agent never responded"
        assert scripted.wake_count >= 1

        # It saw public room state only - private instructions stayed out of context.
        context = scripted.seen_contexts[-1]
        rendered = "\n".join(m.content for m in context.messages)
        assert "never leaves this object" not in rendered
        assert [a.agent_name for a in context.others] == ["Tim-Claude"]
    finally:
        runner.unregister(scripted.agent_id)


async def test_agent_stays_silent_when_relevance_is_below_threshold(fresh_db):

    runner.start()
    room = await rooms.create_room(
        CreateRoomRequest(title="Auth", objective="Design auth.", ttl_seconds=3600)
    )
    gpt_agent = (
        await rooms.join_room(
            JoinRoomRequest(join_code=room.join_code, owner_name="Alan", agent_name="Alan-GPT",
                            provider="openai", autonomous=True)
        )
    ).agent
    human_room = room.id

    scripted = ScriptedAgent(
        gpt_agent,
        [Decision(action="RESPOND", relevance=0.1, message="Sounds good.", reason="mere acknowledgement")],
    )
    runner.register(scripted)
    try:
        await rooms.post_human_message(human_room, "Just checking in.")
        await wait_until(lambda: scripted.wake_count >= 1)
        await asyncio.sleep(0.5)

        messages = await rooms.read_messages(room.id)
        assert not any(m.content == "Sounds good." for m in messages), "low-relevance turn was posted"
        assert (await rooms.get_room(room.id)).agent_turns_used == 0
    finally:
        runner.unregister(scripted.agent_id)


async def test_agent_does_not_wake_on_its_own_memory_update(fresh_db):
    """Memory writes do not cost turns, so self-waking here would loop forever."""

    runner.start()
    room = await rooms.create_room(
        CreateRoomRequest(title="Auth", objective="Design auth.", ttl_seconds=3600)
    )
    gpt_agent = (
        await rooms.join_room(
            JoinRoomRequest(join_code=room.join_code, owner_name="Alan", agent_name="Alan-GPT",
                            provider="openai", autonomous=True)
        )
    ).agent

    scripted = ScriptedAgent(gpt_agent, [])
    runner.register(scripted)
    try:
        await rooms.update_shared_memory(
            room.id,
            MemoryPatch(add_facts=["Access tokens expire in 10 minutes"]),
            updated_by=gpt_agent.agent_name,
            actor_agent_id=gpt_agent.id,
        )
        await asyncio.sleep(1.6)
        assert scripted.wake_count == 0
    finally:
        runner.unregister(scripted.agent_id)


# --------------------------------------------------------------------------
# HTTP API - what the browser sees
# --------------------------------------------------------------------------


async def test_http_flow_create_join_post_observe(fresh_db):
    from app.main import app

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        created = await client.post(
            "/api/rooms",
            json={"title": "Auth system", "objective": "Design and implement auth.", "ttl_seconds": 3600},
        )
        assert created.status_code == 201
        code = created.json()["join_code"]

        joined = await client.post(
            f"/api/rooms/{code}/join",
            json={
                "join_code": code,
                "owner_name": "Tim",
                "agent_name": "Tim-Claude",
                "provider": "claude-code",
                "public_objective": "Implement the backend.",
            },
        )
        assert joined.status_code == 201
        token = joined.json()["agent_token"]

        posted = await client.post(
            "/api/agent/messages",
            json={"content": "Backend will expose /auth/token and /auth/refresh."},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert posted.status_code == 201

        await client.post(f"/api/rooms/{code}/messages", json={"content": "Looks right to me.",
                                                               "sender_label": "Tim"})

        snapshot = (await client.get(f"/api/rooms/{code}")).json()
        assert snapshot["room"]["agent_turns_used"] == 1  # the human message is free
        assert [a["agent_name"] for a in snapshot["agents"]] == ["Tim-Claude"]
        contents = [m["content"] for m in snapshot["messages"]]
        assert "Backend will expose /auth/token and /auth/refresh." in contents
        assert "Looks right to me." in contents

        # Tokens are credentials: they must not appear in room-facing responses.
        assert token not in (await client.get(f"/api/rooms/{code}")).text

        bad = await client.get("/api/rooms/ZZZZZZ")
        assert bad.status_code == 404
        assert bad.json()["error"] == "not_found"


async def test_http_expiry_stops_the_room(fresh_db):
    from app.main import app

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        code = (
            await client.post(
                "/api/rooms", json={"title": "Short", "objective": "Expire fast.", "ttl_seconds": 60}
            )
        ).json()["join_code"]
        token = (
            await client.post(
                f"/api/rooms/{code}/join",
                json={"join_code": code, "owner_name": "Tim", "agent_name": "Tim-Claude"},
            )
        ).json()["agent_token"]

        expired = await client.post(f"/api/rooms/{code}/expire")
        assert expired.json()["status"] == "expired"

        blocked = await client.post(
            "/api/agent/messages",
            json={"content": "anyone still here?"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert blocked.status_code == 409
        assert blocked.json()["error"] == "room_expired"

