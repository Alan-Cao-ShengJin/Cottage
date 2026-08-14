"""Guardrails: the room must be unable to run away with itself."""

from __future__ import annotations

import pytest

from app.config import settings
from app.errors import GuardrailBlocked
from app.models import CreateRoomRequest, JoinRoomRequest
from app.services import guardrails, rooms
from app.services.guardrails import RoomTurnState


def state(**kw) -> RoomTurnState:
    base = dict(
        status="active",
        autonomy_enabled=True,
        agent_turns_used=0,
        last_speaker_id=None,
        consecutive_turns=0,
        seconds_remaining=3600,
    )
    base.update(kw)
    return RoomTurnState(**base)  # type: ignore[arg-type]


# --------------------------------------------------------------------------
# Pure guardrail logic
# --------------------------------------------------------------------------


def test_expired_room_blocks_turns():
    verdict = guardrails.check_turn(state(seconds_remaining=0), "agent_1", autonomous=True)
    assert not verdict.allowed
    assert "expired" in verdict.reason.lower()


def test_room_turn_budget_blocks_turns():
    verdict = guardrails.check_turn(
        state(agent_turns_used=settings.max_room_agent_turns), "agent_1", autonomous=True
    )
    assert not verdict.allowed
    assert "budget" in verdict.reason.lower()


def test_consecutive_turn_cap_blocks_the_same_speaker():
    at_cap = state(
        last_speaker_id="agent_1", consecutive_turns=settings.max_consecutive_turns_per_agent
    )
    assert not guardrails.check_turn(at_cap, "agent_1", autonomous=True).allowed
    # ...but the other agent may still speak.
    assert guardrails.check_turn(at_cap, "agent_2", autonomous=True).allowed


def test_cooldown_only_applies_to_autonomous_turns():
    guardrails.reset()
    guardrails.record_turn("agent_1")
    assert not guardrails.check_turn(state(), "agent_1", autonomous=True).allowed
    assert guardrails.check_turn(state(), "agent_1", autonomous=False).allowed
    guardrails.reset()


def test_pausing_collaboration_blocks_every_agent_turn():
    """The UI's stop button must stop Claude Code too, not just our own loop."""
    paused = state(autonomy_enabled=False)
    assert not guardrails.check_turn(paused, "agent_1", autonomous=True).allowed
    assert not guardrails.check_turn(paused, "agent_1", autonomous=False).allowed


def test_low_relevance_forces_silence():
    assert not guardrails.check_relevance(settings.min_response_relevance - 0.01).allowed
    assert guardrails.check_relevance(settings.min_response_relevance).allowed


# --------------------------------------------------------------------------
# Enforcement through the room service
# --------------------------------------------------------------------------


async def setup_room():
    room = await rooms.create_room(
        CreateRoomRequest(title="Auth", objective="Design auth.", ttl_seconds=3600)
    )
    agents = []
    for name in ("Alan-GPT", "Tim-Claude"):
        joined = await rooms.join_room(
            JoinRoomRequest(join_code=room.join_code, owner_name=name, agent_name=name)
        )
        agents.append(joined.agent)
    return room, agents


@pytest.mark.asyncio
async def test_room_turn_budget_is_enforced_on_post(fresh_db):
    room, (gpt, claude) = await setup_room()

    # Alternate speakers so only the room-wide budget can stop them.
    speakers = [gpt, claude] * settings.max_room_agent_turns
    posted = 0
    for agent in speakers:
        try:
            await rooms.post_message(agent=agent, content=f"substantive point {posted}")
            posted += 1
        except GuardrailBlocked:
            break

    assert posted == settings.max_room_agent_turns
    refreshed = await rooms.get_room(room.id)
    assert refreshed.agent_turns_used == settings.max_room_agent_turns

    with pytest.raises(GuardrailBlocked):
        await rooms.post_message(agent=gpt, content="one more")


@pytest.mark.asyncio
async def test_consecutive_turns_enforced_on_post(fresh_db):
    _, (gpt, _claude) = await setup_room()

    for i in range(settings.max_consecutive_turns_per_agent):
        await rooms.post_message(agent=gpt, content=f"point {i}")

    with pytest.raises(GuardrailBlocked):
        await rooms.post_message(agent=gpt, content="and another thing")


@pytest.mark.asyncio
async def test_other_agent_resets_the_consecutive_counter(fresh_db):
    _, (gpt, claude) = await setup_room()

    for i in range(settings.max_consecutive_turns_per_agent):
        await rooms.post_message(agent=gpt, content=f"point {i}")
    await rooms.post_message(agent=claude, content="counterpoint")

    # GPT may speak again now that someone else has spoken.
    await rooms.post_message(agent=gpt, content="response to counterpoint")


@pytest.mark.asyncio
async def test_human_messages_do_not_consume_the_agent_budget(fresh_db):
    room, _ = await setup_room()
    for i in range(5):
        await rooms.post_human_message(room.id, f"human note {i}")

    refreshed = await rooms.get_room(room.id)
    assert refreshed.agent_turns_used == 0


@pytest.mark.asyncio
async def test_memory_and_task_updates_do_not_consume_the_budget(fresh_db):
    from app.models import CreateTaskRequest, MemoryPatch

    room, (gpt, _) = await setup_room()
    await rooms.update_shared_memory(room.id, MemoryPatch(add_facts=["fact"]), updated_by=gpt.agent_name)
    await rooms.create_task(room.id, CreateTaskRequest(title="task"), created_by=gpt)

    refreshed = await rooms.get_room(room.id)
    assert refreshed.agent_turns_used == 0


@pytest.mark.asyncio
async def test_human_can_reset_the_turn_budget(fresh_db):
    room, (gpt, claude) = await setup_room()
    for i in range(settings.max_room_agent_turns):
        agent = gpt if i % 2 == 0 else claude
        await rooms.post_message(agent=agent, content=f"point {i}")

    with pytest.raises(GuardrailBlocked):
        await rooms.post_message(agent=gpt, content="blocked")

    reset = await rooms.reset_turn_budget(room.id)
    assert reset.agent_turns_used == 0
    await rooms.post_message(agent=gpt, content="allowed again")


@pytest.mark.asyncio
async def test_pausing_collaboration_stops_agents_but_not_humans(fresh_db):
    room, (gpt, _) = await setup_room()
    await rooms.set_autonomy(room.id, False)

    with pytest.raises(GuardrailBlocked):
        await rooms.post_message(agent=gpt, content="autonomous chatter", autonomous=True)
    with pytest.raises(GuardrailBlocked):
        await rooms.post_message(agent=gpt, content="mcp-driven chatter", autonomous=False)

    # Humans can still talk in a paused room.
    await rooms.post_human_message(room.id, "Pausing you two for a second.")

    await rooms.set_autonomy(room.id, True)
    await rooms.post_message(agent=gpt, content="resuming with a real point", autonomous=False)
