from __future__ import annotations

import httpx
import pytest

from app.adapters.mcp import server as mcp_server
from app.core import projections, rooms, store
from app.core.errors import Forbidden, PrivacyViolation
from app.db import database as db
from app.domain.commands import UpdateRoomCharterCommand
from app.domain.events import EventType
from app.main import app


@pytest.mark.asyncio
async def test_charter_is_distinct_from_purpose_and_reaches_cold_start_views(make_room):
    created = await make_room(
        purpose="Ship the release",
        charter="Use small claims. Ready means the release gate is green.",
    )

    assert created.room.purpose == "Ship the release"
    assert created.room.charter == "Use small claims. Ready means the release gate is green."

    snapshot = await projections.hydrate(room_id=created.room.id, recipient=created.owner)
    assert snapshot["room"]["purpose"] == "Ship the release"
    assert snapshot["room"]["charter"] == created.room.charter

    joined = await mcp_server.join_room(
        invitation_token=created.join_token,
        display_name="Cold start agent",
        execution_mode="human_turn_only",
    )
    assert joined["ok"] is True
    assert joined["charter"] == created.room.charter


@pytest.mark.asyncio
async def test_admin_can_replace_and_clear_charter_with_http_mcp_parity(make_room):
    created = await make_room(charter="First charter")

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://arp.test"
    ) as client:
        response = await client.put(
            f"/api/rooms/{created.room.id}/charter",
            headers={"Authorization": f"Bearer {created.owner_token}"},
            json={"command_id": "charter-http", "charter": "Second charter"},
        )
    assert response.status_code == 200
    assert response.json()["room"]["charter"] == "Second charter"

    updated = await mcp_server.update_room_charter(
        charter="",
        command_id="charter-mcp",
        participant_token=created.owner_token,
    )
    assert updated == {"ok": True, "room_id": created.room.id, "charter": ""}
    assert (await store.load_room(created.room.id)).charter == ""
    assert (
        int(
            await db.fetch_value(
                "SELECT COUNT(*) FROM room_events WHERE room_id = ? AND type = ?",
                (created.room.id, EventType.ROOM_CHARTER_UPDATED.value),
            )
        )
        == 2
    )


@pytest.mark.asyncio
async def test_charter_update_is_admin_only_and_content_inspected(make_room, join):
    created = await make_room()
    member = await join(created, display_name="Collaborator")

    with pytest.raises(Forbidden):
        await rooms.update_room_charter(
            participant=member.participant,
            command=UpdateRoomCharterCommand(charter="I should not set this"),
        )

    with pytest.raises(PrivacyViolation):
        await rooms.update_room_charter(
            participant=created.owner,
            command=UpdateRoomCharterCommand(
                charter="Authorization: Bearer sk-proj-abcdefghijklmnopqrstuvwxyz123456"
            ),
        )


@pytest.mark.asyncio
async def test_identical_charter_update_is_idempotent_without_event(make_room):
    created = await make_room(charter="Stable")
    command = UpdateRoomCharterCommand(command_id="same-charter", charter="Stable")

    first = await rooms.update_room_charter(participant=created.owner, command=command)
    replay = await rooms.update_room_charter(participant=created.owner, command=command)

    assert first.charter == replay.charter == "Stable"
    assert (
        int(
            await db.fetch_value(
                "SELECT COUNT(*) FROM room_events WHERE room_id = ? AND type = ?",
                (created.room.id, EventType.ROOM_CHARTER_UPDATED.value),
            )
        )
        == 0
    )
