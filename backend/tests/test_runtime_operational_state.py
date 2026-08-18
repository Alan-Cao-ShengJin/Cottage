"""Operational state is projected per runtime and never substitutes for liveness."""

from __future__ import annotations

import pytest

from app.core import eventlog, presence, runtime_state, tasks
from app.core.errors import InvalidCommand
from app.domain.commands import (
    ClaimTaskCommand,
    ConnectCommand,
    CreateTaskCommand,
    SetRuntimeStateCommand,
)
from app.domain.events import EventType
from app.domain.room import Liveness, RuntimeOperationalState, RuntimeRole

from .conftest import FULL_CAPABILITIES

pytestmark = pytest.mark.asyncio


async def _companion(member, *, label: str = "worker-main"):
    return await presence.connect(
        participant=member.participant,
        command=ConnectCommand(
            capabilities=FULL_CAPABILITIES,
            transport="long_poll",
            attachment_label=label,
            attachment_resumable=True,
            runtime_role=RuntimeRole.COMPANION,
            executor_kind="echo",
        ),
        transport="long_poll",
    )


async def test_state_is_projected_for_the_connection_attachment(make_room, join):
    room = await make_room()
    member = await join(room, display_name="Codex")
    runtime = await _companion(member)

    changed = await runtime_state.set_state(
        participant=member.participant,
        command=SetRuntimeStateCommand(
            connection_id=runtime.connection.id,
            state=RuntimeOperationalState.WORKING,
            summary="Reviewing the reconnect boundary",
        ),
    )

    assert changed["state"] == "working"
    views = await presence.presence_for_room(await room.refresh())
    projected = next(
        r
        for r in views[member.participant.id].runtimes
        if r.ref == runtime.connection.attachment_id
    )
    assert projected.operation is not None
    assert projected.operation.state is RuntimeOperationalState.WORKING
    assert projected.operation.summary == "Reviewing the reconnect boundary"
    assert any(
        event.type is EventType.RUNTIME_STATE_CHANGED
        for event in await eventlog.read_since(room.room.id, 0)
    )


async def test_state_cannot_target_a_sibling_runtime(make_room, join):
    room = await make_room()
    member = await join(room, display_name="Codex")
    first = await _companion(member, label="worker-a")
    second = await _companion(member, label="worker-b")

    await runtime_state.set_state(
        participant=member.participant,
        command=SetRuntimeStateCommand(
            connection_id=first.connection.id,
            state=RuntimeOperationalState.WAITING,
            waiting_reason="Needs an approval",
        ),
    )
    views = await presence.presence_for_room(await room.refresh())
    by_ref = {r.ref: r for r in views[member.participant.id].runtimes}
    assert by_ref[first.connection.attachment_id].operation.state is RuntimeOperationalState.WAITING
    assert (
        by_ref[second.connection.attachment_id].operation.state
        is RuntimeOperationalState.MONITORING
    )


async def test_working_task_must_be_executed_by_that_runtime(make_room, join):
    room = await make_room()
    member = await join(room, display_name="Codex")
    first = await _companion(member, label="worker-a")
    second = await _companion(member, label="worker-b")
    task = await tasks.create(
        participant=member.participant,
        command=CreateTaskCommand(title="Refactor monitor"),
    )
    await tasks.claim(
        participant=member.participant,
        command=ClaimTaskCommand(task_id=task.id, connection_id=first.connection.id),
    )

    with pytest.raises(InvalidCommand):
        await runtime_state.set_state(
            participant=member.participant,
            command=SetRuntimeStateCommand(
                connection_id=second.connection.id,
                state=RuntimeOperationalState.WORKING,
                summary="Doing the task",
                task_id=task.id,
            ),
        )


async def test_disconnect_overrides_but_does_not_erase_operation(make_room, join):
    room = await make_room()
    member = await join(room, display_name="Codex")
    runtime = await _companion(member)
    await runtime_state.set_state(
        participant=member.participant,
        command=SetRuntimeStateCommand(
            connection_id=runtime.connection.id,
            state=RuntimeOperationalState.MONITORING,
        ),
    )
    await presence.disconnect(participant=member.participant, connection_id=runtime.connection.id)

    views = await presence.presence_for_room(await room.refresh())
    projected = next(
        r
        for r in views[member.participant.id].runtimes
        if r.ref == runtime.connection.attachment_id
    )
    assert projected.liveness is Liveness.DISCONNECTED
    assert projected.operation.state is RuntimeOperationalState.MONITORING


async def test_heartbeat_on_closed_connection_requires_reconnect(make_room, join):
    room = await make_room()
    member = await join(room, display_name="Codex")
    runtime = await _companion(member)
    await presence.disconnect(participant=member.participant, connection_id=runtime.connection.id)

    with pytest.raises(InvalidCommand, match="reconnect"):
        await presence.heartbeat(
            participant=member.participant, connection_id=runtime.connection.id
        )
