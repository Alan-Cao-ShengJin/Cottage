"""WebSocket credentials and durable replay primitives."""

from __future__ import annotations

import pytest

from app.api.routes import get_events
from app.core import eventlog, messages, projections, stream_tickets
from app.core.actors import actor_for
from app.core.errors import Unauthenticated
from app.db import database as db
from app.domain.commands import PostMessageCommand
from app.domain.events import EventType

pytestmark = pytest.mark.asyncio


async def test_stream_ticket_is_short_lived_room_bound_and_one_use(make_room, join):
    room = await make_room()
    member = await join(room, display_name="Human")
    elsewhere = await make_room(name="Elsewhere")

    issued = await stream_tickets.issue(member.participant)
    with pytest.raises(Unauthenticated):
        await stream_tickets.consume(issued.token, room_id=elsewhere.room.id)

    restored = await stream_tickets.consume(issued.token, room_id=room.room.id)
    assert restored.id == member.participant.id
    with pytest.raises(Unauthenticated, match="already used"):
        await stream_tickets.consume(issued.token, room_id=room.room.id)


async def test_websocket_replay_source_is_the_durable_log(make_room, join):
    room = await make_room()
    author = await join(room, display_name="Author")
    reader = await join(room, display_name="Reader")
    cursor = await eventlog.current_seq(room.room.id)

    posted = await messages.post(
        participant=author.participant,
        command=PostMessageCommand(body="This must survive a dropped socket"),
    )
    visible = await projections.visible_events_since(
        room_id=room.room.id,
        recipient=reader.participant,
        since_seq=cursor,
    )

    assert posted["seq"] is not None
    assert [event["payload"]["body"] for event in visible if event["type"] == "message.posted"] == [
        "This must survive a dropped socket"
    ]


async def test_pull_cursor_never_skips_an_unread_second_page(make_room, join):
    room = await make_room()
    member = await join(room, display_name="Reader")
    cursor = await eventlog.current_seq(room.room.id)
    for body in ("one", "two", "three"):
        await messages.post(
            participant=member.participant,
            command=PostMessageCommand(body=body),
        )

    first = await get_events(
        room.room.id,
        member.participant,
        since_seq=cursor,
        limit=1,
        wait_seconds=0,
    )
    assert [event["payload"]["body"] for event in first["events"]] == ["one"]
    assert first["cursor"] < first["current_seq"]

    rest = await get_events(
        room.room.id,
        member.participant,
        since_seq=first["cursor"],
        limit=10,
        wait_seconds=0,
    )
    assert [event["payload"]["body"] for event in rest["events"]] == ["two", "three"]


async def test_durable_context_is_not_displaced_by_routine_activity_noise(make_room, join):
    room = await make_room()
    author = await join(room, display_name="Author")
    reader = await join(room, display_name="Reader")
    await messages.post(
        participant=author.participant,
        command=PostMessageCommand(body="The architectural decision that must remain in context"),
    )
    async with db.transaction() as tx:
        for index in range(180):
            await eventlog.append(
                tx,
                room_id=room.room.id,
                type_=EventType.ACTIVITY_NOTED,
                actor=actor_for(author.participant),
                payload={"phase": "monitoring", "summary": f"routine activity {index}"},
            )

    state = await projections.hydrate(
        room_id=room.room.id,
        recipient=reader.participant,
    )

    bodies = [
        event["payload"].get("body")
        for event in state["recent_relevant_events"]
        if event["type"] == "message.posted"
    ]
    assert "The architectural decision that must remain in context" in bodies
