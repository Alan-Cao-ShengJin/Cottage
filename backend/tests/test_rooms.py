"""Unit tests for core room logic."""

from __future__ import annotations

import pytest

from app.errors import Forbidden, GuardrailBlocked, NotFound, RoomExpired
from app.models import (
    CreateRoomRequest,
    CreateTaskRequest,
    JoinRoomRequest,
    MemoryPatch,
)
from app.services import rooms
from app.util import JOIN_CODE_LENGTH

pytestmark = pytest.mark.asyncio


async def make_room(objective: str = "Design and implement an authentication system.", **kw):
    return await rooms.create_room(
        CreateRoomRequest(title=kw.pop("title", "Auth system"), objective=objective, **kw)
    )


async def join(room, agent_name: str, owner: str = "Owner", objective: str = "", provider="other"):
    return await rooms.join_room(
        JoinRoomRequest(
            join_code=room.join_code,
            owner_name=owner,
            agent_name=agent_name,
            provider=provider,
            public_objective=objective,
        )
    )


# --------------------------------------------------------------------------
# Rooms
# --------------------------------------------------------------------------


async def test_create_room_issues_short_code_and_ttl(fresh_db):
    room = await make_room()
    assert len(room.join_code) == JOIN_CODE_LENGTH
    assert room.join_code.isupper()
    assert room.status == "active"
    assert room.seconds_remaining > 0
    assert room.objective.startswith("Design and implement")


async def test_room_lookup_by_code_is_case_insensitive(fresh_db):
    room = await make_room()
    assert (await rooms.get_room(room.join_code.lower())).id == room.id
    assert (await rooms.get_room(room.id)).id == room.id


async def test_unknown_room_raises(fresh_db):
    with pytest.raises(NotFound):
        await rooms.get_room("NOPE00")


async def test_shared_memory_seeded_with_objective(fresh_db):
    room = await make_room()
    memory = await rooms.get_shared_memory(room.id)
    assert memory.data.objective == room.objective
    assert memory.data.decisions == []


# --------------------------------------------------------------------------
# Membership
# --------------------------------------------------------------------------


async def test_join_and_list_members(fresh_db):
    room = await make_room()
    a = await join(room, "Alan-GPT", "Alan", "architecture", provider="openai")
    b = await join(room, "Tim-Claude", "Tim", "backend", provider="claude-code")

    members = await rooms.get_members(room.id)
    assert [m.agent_name for m in members] == ["Alan-GPT", "Tim-Claude"]
    assert a.agent_token != b.agent_token
    assert members[0].public_objective == "architecture"


async def test_rejoining_same_name_resumes_identity(fresh_db):
    room = await make_room()
    first = await join(room, "Tim-Claude", "Tim", "backend")
    second = await join(room, "Tim-Claude", "Tim", "backend v2")

    assert first.agent.id == second.agent.id
    assert second.agent_token == first.agent_token
    assert second.agent.public_objective == "backend v2"
    assert len(await rooms.get_members(room.id)) == 1


async def test_authenticate_agent_rejects_unknown_token(fresh_db):
    with pytest.raises(Forbidden):
        await rooms.authenticate_agent("not-a-real-token")


async def test_leave_marks_agent_left_and_blocks_posting(fresh_db):
    room = await make_room()
    joined = await join(room, "Tim-Claude")
    await rooms.leave_room(joined.agent.id, "done")

    agent = await rooms.get_agent(joined.agent.id)
    assert agent.status == "left"
    with pytest.raises(Forbidden):
        await rooms.post_message(agent=agent, content="still here?")


# --------------------------------------------------------------------------
# Messages
# --------------------------------------------------------------------------


async def test_post_and_read_messages_with_cursor(fresh_db):
    room = await make_room()
    gpt = (await join(room, "Alan-GPT")).agent
    claude = (await join(room, "Tim-Claude")).agent

    first = await rooms.post_message(agent=gpt, content="Short-lived access tokens, server-side refresh.")
    second = await rooms.post_message(agent=claude, content="Refresh token currently in localStorage.")

    all_messages = await rooms.read_messages(room.id)
    contents = [m.content for m in all_messages]
    assert "Short-lived access tokens, server-side refresh." in contents

    incremental = await rooms.read_messages(room.id, since_id=first.id)
    assert second.id in [m.id for m in incremental]
    assert first.id not in [m.id for m in incremental]


async def test_direct_message_requires_recipient_in_room(fresh_db):
    room = await make_room()
    other_room = await make_room(title="Other")
    gpt = (await join(room, "Alan-GPT")).agent
    outsider = (await join(other_room, "Outsider")).agent

    with pytest.raises(NotFound):
        await rooms.post_message(agent=gpt, content="hi", recipient_agent_id=outsider.id)


async def test_expired_room_rejects_messages(fresh_db):
    room = await make_room()
    agent = (await join(room, "Alan-GPT")).agent
    await rooms.set_expiry_now(room.id)

    refreshed = await rooms.get_room(room.id)
    assert refreshed.status == "expired"
    with pytest.raises(RoomExpired):
        await rooms.post_message(agent=agent, content="anyone there?")


# --------------------------------------------------------------------------
# Shared memory
# --------------------------------------------------------------------------


async def test_memory_updates_are_additive_and_deduplicated(fresh_db):
    room = await make_room()
    await rooms.update_shared_memory(
        room.id, MemoryPatch(add_decisions=["Use OAuth 2.1"]), updated_by="Alan-GPT"
    )
    await rooms.update_shared_memory(
        room.id,
        MemoryPatch(add_decisions=["use oauth 2.1", "Refresh tokens stored server-side"]),
        updated_by="Tim-Claude",
    )

    memory = await rooms.get_shared_memory(room.id)
    assert memory.data.decisions == ["Use OAuth 2.1", "Refresh tokens stored server-side"]
    assert memory.updated_by == "Tim-Claude"


async def test_resolving_open_questions_removes_them(fresh_db):
    room = await make_room()
    await rooms.update_shared_memory(
        room.id,
        MemoryPatch(add_open_questions=["Session expiry duration", "Device-level sessions?"]),
    )
    await rooms.update_shared_memory(
        room.id, MemoryPatch(resolve_open_questions=["session expiry duration"])
    )

    memory = await rooms.get_shared_memory(room.id)
    assert memory.data.open_questions == ["Device-level sessions?"]


# --------------------------------------------------------------------------
# Tasks
# --------------------------------------------------------------------------


async def test_task_lifecycle(fresh_db):
    room = await make_room()
    gpt = (await join(room, "Alan-GPT")).agent
    claude = (await join(room, "Tim-Claude")).agent

    task = await rooms.create_task(
        room.id, CreateTaskRequest(title="Move refresh token to HttpOnly cookie"), created_by=gpt
    )
    assert task.status == "open"

    claimed = await rooms.claim_task(task.id, claude)
    assert claimed.status == "claimed"
    assert claimed.assigned_agent_id == claude.id

    done = await rooms.complete_task(task.id, claude, "Cookie set with SameSite=Lax.")
    assert done.status == "done"
    assert "SameSite" in (done.result or "")


async def test_claiming_someone_elses_task_fails(fresh_db):
    room = await make_room()
    gpt = (await join(room, "Alan-GPT")).agent
    claude = (await join(room, "Tim-Claude")).agent

    task = await rooms.create_task(
        room.id, CreateTaskRequest(title="Write token tests", assign_to_self=True), created_by=gpt
    )
    with pytest.raises(Exception) as err:
        await rooms.claim_task(task.id, claude)
    assert "already claimed" in str(err.value).lower()


# --------------------------------------------------------------------------
# Snapshot
# --------------------------------------------------------------------------


async def test_snapshot_contains_public_state_only(fresh_db):
    room = await make_room()
    joined = await join(room, "Alan-GPT", "Alan", "architecture")
    await rooms.post_message(agent=joined.agent, content="Proposing short-lived access tokens.")

    snapshot = await rooms.get_snapshot(room.join_code)
    assert snapshot.room.id == room.id
    assert [a.agent_name for a in snapshot.agents] == ["Alan-GPT"]
    assert any("short-lived" in m.content.lower() for m in snapshot.messages)

    # Agent tokens are credentials and must never reach the room snapshot.
    dumped = snapshot.model_dump_json()
    assert joined.agent_token not in dumped
