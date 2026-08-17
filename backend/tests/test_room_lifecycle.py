from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import timedelta

import httpx
import pytest
from pydantic import ValidationError

from app.adapters.mcp import server as mcp_server
from app.api import routes as api_routes
from app.config import Settings, settings
from app.core import presence, rooms, store
from app.core.errors import Forbidden, RoomClosed, Unauthenticated
from app.db import database as db
from app.domain.commands import (
    ConnectCommand,
    CreateInvitationCommand,
    CreateRoomCommand,
    ExtendRoomCommand,
    LeaveRoomCommand,
    MintCredentialCommand,
    RevokeCredentialCommand,
)
from app.domain.events import EventType
from app.domain.room import RetentionPolicy
from app.main import app
from app.util import from_iso, iso_in, to_iso, utcnow


async def _count(table: str) -> int:
    return int(await db.fetch_value(f"SELECT COUNT(*) FROM {table}"))


async def _extension_events(room_id: str) -> int:
    return int(
        await db.fetch_value(
            "SELECT COUNT(*) FROM room_events WHERE room_id = ? AND type = ?",
            (room_id, EventType.ROOM_EXPIRY_EXTENDED.value),
        )
    )


@pytest.mark.asyncio
async def test_heartbeat_does_not_publish_a_transition_back_to_the_same_grade(make_room, join):
    """Crossing the derived idle threshold between polls is not an observed transition.

    Consumers last saw ``live_poll``. A heartbeat that restores the same published grade
    must not append another identical ``presence.changed`` on every polling cycle.
    """
    created = await make_room()
    member = await join(created, display_name="Quiet poller", transport="long_poll")
    connection = await store.load_connection(member.connection_id)
    await db.execute(
        "UPDATE connections SET last_heartbeat_at = ? WHERE id = ?",
        (iso_in(-(connection.heartbeat_interval_s + 1)), connection.id),
    )
    seq_before = (await created.refresh()).event_seq

    await presence.heartbeat(connection_id=member.connection_id, participant=member.participant)

    assert (await created.refresh()).event_seq == seq_before


@pytest.mark.asyncio
async def test_default_ttl_is_applied_and_invitations_cannot_outlive_room(make_room, monkeypatch):
    monkeypatch.setattr(rooms, "settings", replace(settings, default_room_ttl_seconds=120))
    created = await make_room()

    assert created.room.retention.ttl_seconds == 120
    assert created.room.expires_at is not None
    assert 115 <= (from_iso(created.room.expires_at) - utcnow()).total_seconds() <= 120

    # The room fixture does not retain the default invitation id; inspect by room.
    default_invitation = await db.fetch_one(
        "SELECT expires_at FROM invitations WHERE room_id = ? ORDER BY created_at LIMIT 1",
        (created.room.id,),
    )
    assert default_invitation is not None
    assert default_invitation["expires_at"] <= created.room.expires_at

    later = await rooms.create_invitation(
        participant=created.owner,
        command=CreateInvitationCommand(ttl_seconds=3600),
    )
    assert later.invitation.expires_at == created.room.expires_at


def test_lifecycle_durations_are_bounded():
    with pytest.raises(ValueError):
        Settings(default_room_ttl_seconds=59)
    with pytest.raises(ValidationError):
        RetentionPolicy(ttl_seconds=91 * 24 * 3600)
    with pytest.raises(ValidationError):
        RetentionPolicy(max_event_age_days=366)
    with pytest.raises(ValidationError):
        ExtendRoomCommand(extend_seconds=59)
    with pytest.raises(ValidationError):
        ExtendRoomCommand(extend_seconds=31 * 24 * 3600)


@pytest.mark.asyncio
async def test_extension_is_idempotent_and_http_mcp_have_parity(make_room):
    created = await make_room()
    before = from_iso(created.room.expires_at or "")

    first = await rooms.extend_room(
        participant=created.owner,
        command=ExtendRoomCommand(command_id="extend-once", extend_seconds=60),
    )
    replay = await rooms.extend_room(
        participant=created.owner,
        command=ExtendRoomCommand(command_id="extend-once", extend_seconds=60),
    )
    assert first.expires_at == replay.expires_at
    assert from_iso(first.expires_at or "") == before + timedelta(seconds=60)
    assert await _extension_events(created.room.id) == 1

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://arp.test"
    ) as client:
        response = await client.post(
            f"/api/rooms/{created.room.id}/extend",
            headers={"Authorization": f"Bearer {created.owner_token}"},
            json={"command_id": "extend-http", "extend_seconds": 60},
        )
    assert response.status_code == 200
    replay_after_later_extension = await rooms.extend_room(
        participant=created.owner,
        command=ExtendRoomCommand(command_id="extend-once", extend_seconds=60),
    )
    assert replay_after_later_extension.expires_at == first.expires_at
    assert (await store.load_room(created.room.id)).expires_at == response.json()["room"][
        "expires_at"
    ]
    mcp_result = await mcp_server.extend_room(
        extend_seconds=60,
        command_id="extend-mcp",
        participant_token=created.owner_token,
    )
    assert mcp_result["ok"] is True
    assert await _extension_events(created.room.id) == 3


@pytest.mark.asyncio
async def test_account_agent_admin_may_extend_without_human_presence(make_room):
    seeded = await make_room(name="identity seed")
    org_id, user_id = seeded.org_id, seeded.owner_user_id
    identity = await rooms.create_identity(
        org_id=org_id,
        owner_user_id=user_id,
        display_name="Account agent admin",
    )
    created = await rooms.create_room(
        principal=rooms.Principal(kind="agent_identity", org_id=org_id, identity=identity),
        command=CreateRoomCommand(name="Agent-owned room"),
    )
    extended = await rooms.extend_room(
        participant=created.participant,
        command=ExtendRoomCommand(command_id="agent-admin", extend_seconds=60),
    )
    assert extended.expires_at is not None
    assert await _extension_events(created.room.id) == 1


@pytest.mark.asyncio
async def test_extension_vs_reaper_has_one_legal_transition(make_room, monkeypatch):
    created = await make_room()
    await db.execute(
        "UPDATE rooms SET expires_at = ? WHERE id = ?",
        (iso_in(120), created.room.id),
    )
    # Simulate a reaper that selected a stale candidate. Its guarded update must not
    # close a deadline that is actually still in the future or that extension moves.
    monkeypatch.setattr(rooms, "is_past", lambda _deadline: True)

    extended, closed = await asyncio.gather(
        rooms.extend_room(
            participant=created.owner,
            command=ExtendRoomCommand(command_id="race", extend_seconds=60),
        ),
        rooms.expire_due_rooms(),
    )

    assert extended.status.value == "open"
    assert closed == []
    assert await _extension_events(created.room.id) == 1
    assert (
        await db.fetch_value(
            "SELECT COUNT(*) FROM room_events WHERE room_id = ? AND type = ?",
            (created.room.id, EventType.ROOM_CLOSED.value),
        )
        == 0
    )


@pytest.mark.asyncio
async def test_elapsed_deadline_wins_concurrent_extension_once(make_room):
    created = await make_room()
    await db.execute(
        "UPDATE rooms SET expires_at = ? WHERE id = ?",
        (to_iso(utcnow() - timedelta(seconds=1)), created.room.id),
    )

    attempted, closed = await asyncio.gather(
        rooms.extend_room(
            participant=created.owner,
            command=ExtendRoomCommand(command_id="expired-race", extend_seconds=60),
        ),
        rooms.expire_due_rooms(),
        return_exceptions=True,
    )

    assert isinstance(attempted, RoomClosed)
    assert closed == [created.room.id]
    assert (await store.load_room(created.room.id)).status.value == "closed"
    assert await _extension_events(created.room.id) == 0
    assert (
        await db.fetch_value(
            "SELECT COUNT(*) FROM room_events WHERE room_id = ? AND type = ?",
            (created.room.id, EventType.ROOM_CLOSED.value),
        )
        == 1
    )


@pytest.mark.asyncio
async def test_heartbeat_and_poll_never_extend_room_expiry(make_room):
    created = await make_room()
    expires_at = created.room.expires_at
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://arp.test"
    ) as client:
        connected = await client.post(
            f"/api/rooms/{created.room.id}/connect",
            headers={"Authorization": f"Bearer {created.owner_token}"},
            json={"capabilities": []},
        )
        assert connected.status_code == 201
        heartbeat = await client.post(
            f"/api/rooms/{created.room.id}/heartbeat",
            headers={"Authorization": f"Bearer {created.owner_token}"},
            json={"connection_id": connected.json()["connection_id"]},
        )
        assert heartbeat.status_code == 200

    cursor = await db.fetch_value("SELECT event_seq FROM rooms WHERE id = ?", (created.room.id,))
    polled = await mcp_server.await_room_events(
        since_seq=int(cursor),
        timeout_seconds=1,
        participant_token=created.owner_token,
    )
    assert polled["ok"] is True
    assert (await store.load_room(created.room.id)).expires_at == expires_at


@pytest.mark.asyncio
async def test_connect_and_heartbeat_recheck_expiry_inside_mutation(make_room):
    created = await make_room()
    connected = await presence.connect(
        participant=created.owner,
        command=ConnectCommand(capabilities=[]),
        transport="long_poll",
    )
    connection_id = connected.connection.id
    before_heartbeat = await db.fetch_one(
        "SELECT last_heartbeat_at, last_delivered_seq FROM connections WHERE id = ?",
        (connection_id,),
    )
    await db.execute(
        "UPDATE rooms SET expires_at = ? WHERE id = ?",
        (to_iso(utcnow() - timedelta(seconds=1)), created.room.id),
    )
    baseline = {
        "connections": await _count("connections"),
        "events": await _count("room_events"),
        "receipts": await _count("command_receipts"),
    }

    with pytest.raises(RoomClosed):
        await presence.connect(
            participant=created.owner,
            command=ConnectCommand(command_id="expired-connect", capabilities=[]),
            transport="long_poll",
        )
    with pytest.raises(RoomClosed):
        await presence.heartbeat(
            connection_id=connection_id,
            participant=created.owner,
            seq=999,
        )

    after_heartbeat = await db.fetch_one(
        "SELECT last_heartbeat_at, last_delivered_seq FROM connections WHERE id = ?",
        (connection_id,),
    )
    assert dict(after_heartbeat) == dict(before_heartbeat)

    cursor = await db.fetch_value("SELECT event_seq FROM rooms WHERE id = ?", (created.room.id,))
    closed_poll = await mcp_server.await_room_events(
        since_seq=int(cursor),
        timeout_seconds=1,
        participant_token=created.owner_token,
    )
    assert closed_poll["ok"] is True
    assert dict(
        await db.fetch_one(
            "SELECT last_heartbeat_at, last_delivered_seq FROM connections WHERE id = ?",
            (connection_id,),
        )
    ) == dict(before_heartbeat)
    assert {
        "connections": await _count("connections"),
        "events": await _count("room_events"),
        "receipts": await _count("command_receipts"),
    } == baseline


@pytest.mark.asyncio
async def test_connect_retry_returns_original_connection_without_a_second_event(make_room):
    created = await make_room()
    command = ConnectCommand(command_id="connect-once", capabilities=[])
    first = await presence.connect(
        participant=created.owner,
        command=command,
        transport="long_poll",
    )
    replay = await presence.connect(
        participant=created.owner,
        command=command,
        transport="long_poll",
    )
    assert replay.connection.id == first.connection.id
    assert await _count("connections") == 1


@pytest.mark.asyncio
async def test_sse_replay_remains_available_after_expiry(make_room):
    created = await make_room()
    await db.execute(
        "UPDATE rooms SET expires_at = ? WHERE id = ?",
        (to_iso(utcnow() - timedelta(seconds=1)), created.room.id),
    )

    class DisconnectedRequest:
        async def is_disconnected(self) -> bool:
            return True

    response = await api_routes.stream(
        room_id=created.room.id,
        request=DisconnectedRequest(),
        participant=created.owner,
        since_seq=0,
        connection_id=None,
    )
    first_frame = await anext(response.body_iterator)
    assert "event: snapshot" in first_frame
    await response.body_iterator.aclose()


@pytest.mark.asyncio
async def test_guest_provisioning_revalidates_invitation_and_room_in_transaction(make_room):
    created = await make_room()
    revoked = await rooms.create_invitation(
        participant=created.owner,
        command=CreateInvitationCommand(max_redemptions=2),
    )
    revoked_credential = await rooms.authenticate_invitation(revoked.token)
    await rooms.revoke_invitation(
        participant=created.owner,
        invitation_id=revoked.invitation.id,
    )
    baseline_identities = await _count("agent_identities")
    with pytest.raises(Unauthenticated):
        await rooms.provision_guest_identity(
            revoked_credential,
            display_name="Revoked guest",
        )
    assert await _count("agent_identities") == baseline_identities

    expiring = await rooms.create_invitation(
        participant=created.owner,
        command=CreateInvitationCommand(max_redemptions=2),
    )
    expiring_credential = await rooms.authenticate_invitation(expiring.token)
    await db.execute(
        "UPDATE rooms SET expires_at = ? WHERE id = ?",
        (to_iso(utcnow() - timedelta(seconds=1)), created.room.id),
    )
    with pytest.raises(Unauthenticated):
        await rooms.provision_guest_identity(
            expiring_credential,
            display_name="Expired guest",
        )
    assert await _count("agent_identities") == baseline_identities


@pytest.mark.asyncio
async def test_cleanup_and_authority_reduction_remain_available_after_expiry(make_room, join):
    created = await make_room()
    collaborator = await join(created, display_name="departing collaborator")
    invitation = await rooms.create_invitation(
        participant=created.owner,
        command=CreateInvitationCommand(),
    )
    runtime = await rooms.mint_runtime_credential(
        participant=created.owner,
        command=MintCredentialCommand(label="revoke after expiry"),
    )
    await db.execute(
        "UPDATE rooms SET expires_at = ? WHERE id = ?",
        (to_iso(utcnow() - timedelta(seconds=1)), created.room.id),
    )

    await rooms.revoke_invitation(
        participant=created.owner,
        invitation_id=invitation.invitation.id,
    )
    revoked = await rooms.revoke_runtime_credential(
        participant=created.owner,
        command=RevokeCredentialCommand(credential_id=runtime.credential.id),
    )
    await rooms.leave_room(
        participant=collaborator.participant,
        command=LeaveRoomCommand(note="safe shutdown"),
    )

    invitation_row = await db.fetch_one(
        "SELECT revoked_at FROM invitations WHERE id = ?",
        (invitation.invitation.id,),
    )
    assert invitation_row["revoked_at"] is not None
    assert revoked.revoked_at is not None
    assert (await store.load_participant(collaborator.participant.id)).state.value == "left"


@pytest.mark.asyncio
async def test_elapsed_room_refuses_before_any_identity_or_command_side_effect(make_room):
    created = await make_room()
    await db.execute(
        "UPDATE rooms SET expires_at = ? WHERE id = ?",
        (to_iso(utcnow() - timedelta(seconds=1)), created.room.id),
    )
    baseline = {
        table: await _count(table)
        for table in (
            "agent_identities",
            "participants",
            "invitations",
            "connections",
            "room_events",
            "command_receipts",
        )
    }

    with pytest.raises(RoomClosed):
        await rooms.create_invitation(
            participant=created.owner,
            command=CreateInvitationCommand(command_id="expired-invite"),
        )
    with pytest.raises(RoomClosed):
        await rooms.extend_room(
            participant=created.owner,
            command=ExtendRoomCommand(command_id="too-late", extend_seconds=60),
        )
    with pytest.raises(Unauthenticated):
        await rooms.authenticate_invitation(created.join_token)

    mcp_join = await mcp_server.join_room(
        invitation_token=created.join_token,
        display_name="Must not be provisioned",
        execution_mode="unattended_loop",
    )
    assert mcp_join["ok"] is False

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://arp.test"
    ) as client:
        response = await client.post(
            f"/api/rooms/{created.room.id}/connect",
            headers={"Authorization": f"Bearer {created.owner_token}"},
            json={"capabilities": []},
        )
    assert response.status_code == 409
    assert {table: await _count(table) for table in baseline} == baseline


@pytest.mark.asyncio
async def test_non_admin_runtime_and_foreign_extension_attempts_are_side_effect_free(
    make_room, join
):
    first = await make_room(name="first")
    collaborator = await join(first, display_name="collaborator")
    second = await make_room(name="second")
    issued_runtime = await rooms.mint_runtime_credential(
        participant=first.owner,
        command=MintCredentialCommand(label="lifecycle runtime"),
    )
    runtime = await store.load_participant_by_token(issued_runtime.token)
    baseline_receipts = await _count("command_receipts")
    first_seq = (await store.load_room(first.room.id)).event_seq
    second_seq = (await store.load_room(second.room.id)).event_seq

    with pytest.raises(Forbidden):
        await rooms.extend_room(
            participant=collaborator.participant,
            command=ExtendRoomCommand(command_id="non-admin", extend_seconds=60),
        )
    with pytest.raises(Forbidden):
        await rooms.extend_room(
            participant=runtime,
            command=ExtendRoomCommand(command_id="runtime", extend_seconds=60),
        )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://arp.test"
    ) as client:
        response = await client.post(
            f"/api/rooms/{second.room.id}/extend",
            headers={"Authorization": f"Bearer {first.owner_token}"},
            json={"command_id": "foreign", "extend_seconds": 60},
        )
    assert response.status_code == 403
    assert await _count("command_receipts") == baseline_receipts
    assert (await store.load_room(first.room.id)).event_seq == first_seq
    assert (await store.load_room(second.room.id)).event_seq == second_seq
