"""End-to-end M1 slice: CONNECT → SEE CURRENT WORK → COORDINATE → CLAIM → DISCONNECT.

Exercised through the real transports rather than the service layer, because the
point of the slice is that two independently authenticated participants on *different
transports* see one coherent room. The HTTP client drives the ARP surface; the MCP
adapter's tool functions are called in-process (they are ordinary async functions, and
going through the MCP wire protocol would test the SDK, not us).

These map onto the M1 exit criteria in `docs/ROADMAP.md`.
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from app.adapters.mcp import server as mcp_tools
from app.core import eventlog, rooms, store, tasks
from app.db import database as db
from app.main import app
from app.util import iso_in

pytestmark = pytest.mark.asyncio


async def _client() -> httpx.AsyncClient:
    # No lifespan: the fixtures own the database, and the reaper is driven explicitly
    # so the tests stay deterministic.
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://arp.test")


async def _owner_token(org_id: str, user_id: str) -> str:
    return await rooms.issue_principal_token(
        subject_kind="user", subject_id=user_id, org_id=org_id, label="test"
    )


def _ok(response: httpx.Response) -> dict:
    assert response.status_code < 400, f"{response.status_code}: {response.text}"
    return response.json()


# ---------------------------------------------------------------------------
# The full loop over HTTP
# ---------------------------------------------------------------------------


async def test_full_slice_over_http(fresh_db, org):
    org_id, user_id = org
    token = await _owner_token(org_id, user_id)
    auth = {"Authorization": f"Bearer {token}"}

    async with await _client() as client:
        # --- meta: capabilities are discoverable, and say so explicitly ---------
        caps = _ok(await client.get("/api/capabilities"))
        assert "supports_push" in caps["capabilities"]
        assert "requires_human_presence" in caps["capabilities"]
        assert "descriptive label" in caps["note"]

        # --- CONNECT: create room, invite, join, connect -----------------------
        created = _ok(
            await client.post(
                "/api/rooms",
                json={"name": "Ship the API", "purpose": "coordinate the release"},
                headers=auth,
            )
        )
        room_id = created["room"]["id"]

        # One call did everything: the room exists, the creator is already an owner
        # participant, and a shareable join token is minted. No bootstrap dance.
        assert created["participant"]["role"] == "owner"
        assert created["participant"]["state"] == "joined"
        owner_auth = {"Authorization": f"Bearer {created['participant_token']}"}
        invitation_token = created["join_token"]
        assert invitation_token and invitation_token != created["participant_token"]

        # The owner token works immediately for an admin action, with no extra setup.
        extra = _ok(
            await client.post(
                f"/api/rooms/{room_id}/invitations",
                json={"role": "collaborator", "max_redemptions": 5},
                headers=owner_auth,
            )
        )
        assert "token" not in extra["invitation"], "the invitation record must not carry the token"

        joined = _ok(
            await client.post(
                "/api/rooms/join",
                json={
                    "invitation_token": invitation_token,
                    "display_name": "Browser Human",
                    "kind": "human",
                    "host_class": "browser_human",
                    "capabilities": [
                        "can_receive_events",
                        "supports_push",
                        "supports_resume",
                        "can_initiate_followup",
                        "can_execute_background",
                        "supports_tools",
                    ],
                },
                headers=auth,
            )
        )
        human_token = joined["participant_token"]
        human_auth = {"Authorization": f"Bearer {human_token}"}
        human_id = joined["participant"]["id"]

        negotiated = _ok(
            await client.post(
                f"/api/rooms/{room_id}/connect",
                json={
                    "host_class": "browser_human",
                    "capabilities": [
                        "can_receive_events",
                        "supports_push",
                        "supports_resume",
                        "can_initiate_followup",
                        "can_execute_background",
                        "supports_tools",
                    ],
                },
                headers=human_auth,
            )
        )
        assert negotiated["delivery_mode"] == "push"
        assert negotiated["may_claim"] is True
        assert negotiated["lease_renewable_unattended"] is True

        # --- SEE CURRENT WORK --------------------------------------------------
        _ok(
            await client.post(
                f"/api/rooms/{room_id}/work",
                json={
                    "headline": "Reviewing the release checklist",
                    "targets": ["docs/RELEASE.md"],
                },
                headers=human_auth,
            )
        )

        snapshot = _ok(await client.get(f"/api/rooms/{room_id}/snapshot", headers=human_auth))
        assert snapshot["type"] == "snapshot"
        assert [w["headline"] for w in snapshot["work"]] == ["Reviewing the release checklist"]
        me = next(p for p in snapshot["participants"] if p["id"] == human_id)
        assert me["presence"]["liveness"] == "live_push"
        assert me["presence"]["runtime"]["may_claim"] is True

        # --- COORDINATE --------------------------------------------------------
        _ok(
            await client.post(
                f"/api/rooms/{room_id}/messages",
                json={"body": "Starting on the checklist; shout if you need the file."},
                headers=human_auth,
            )
        )

        # --- DISTRIBUTE / CLAIM ------------------------------------------------
        task = _ok(
            await client.post(
                f"/api/rooms/{room_id}/tasks",
                json={
                    "title": "Cut the release branch",
                    "targets": ["git/main"],
                    "priority": 5,
                },
                headers=human_auth,
            )
        )["task"]

        claimed = _ok(
            await client.post(
                f"/api/rooms/{room_id}/tasks/claim",
                json={"task_id": task["id"]},
                headers=human_auth,
            )
        )["task"]
        fence = claimed["claim"]["fence"]
        assert claimed["claim"]["participant_id"] == human_id

        # A fence-guarded mutation succeeds...
        _ok(
            await client.patch(
                f"/api/rooms/{room_id}/tasks",
                json={"task_id": task["id"], "fence": fence, "in_progress": True},
                headers=human_auth,
            )
        )
        # ...and a stale one is refused with the protocol code, not a generic 500.
        stale = await client.patch(
            f"/api/rooms/{room_id}/tasks",
            json={"task_id": task["id"], "fence": fence - 1, "title": "nope"},
            headers=human_auth,
        )
        assert stale.status_code == 409
        assert stale.json()["error"] == "stale_fence"

        completed = _ok(
            await client.post(
                f"/api/rooms/{room_id}/tasks/complete",
                json={"task_id": task["id"], "fence": fence, "result": "branch cut at a1b2c3d"},
                headers=human_auth,
            )
        )["task"]
        assert completed["status"] == "done"

        # --- DISCONNECT --------------------------------------------------------
        _ok(
            await client.post(
                f"/api/rooms/{room_id}/leave", json={"note": "done"}, headers=human_auth
            )
        )
        # The participant token is revoked on leave, so it cannot be replayed.
        after_leave = await client.get(f"/api/rooms/{room_id}/snapshot", headers=human_auth)
        assert after_leave.status_code == 401


# `_bootstrap_owner` used to live here, seeding an owner participant row by hand because
# creating a room did not join its creator. That helper existing in two test files was the
# clearest evidence the API was wrong; `room.create` now does it, so the helper is gone.


# ---------------------------------------------------------------------------
# MCP-only path: create and join without ever touching the browser
# ---------------------------------------------------------------------------


async def test_agent_can_create_and_share_a_room_entirely_over_mcp(fresh_db, org):
    """The whole flow an agent host needs: create, get a token, hand it over, join.

    No HTTP call, no web console. This is the path a ChatGPT or Claude Code user takes.
    """
    org_id, user_id = org
    principal = await _owner_token(org_id, user_id)

    created = await mcp_tools.create_room(
        principal_token=principal,
        name="MCP-only room",
        purpose="prove the browser is optional",
        display_name="Creator Agent",
    )
    assert created["ok"] is True
    join_token = created["join_token"]
    assert join_token

    # A second agent joins with nothing but that token.
    joined = await mcp_tools.join_room(
        invitation_token=join_token,
        display_name="Second Agent",
        execution_mode="unattended_loop",
    )
    assert joined["ok"] is True
    assert joined["room_id"] == created["room_id"]

    # Both are present, and the creator is an owner.
    state = await mcp_tools.get_room_state(participant_token=created["participant_token"])
    names = sorted(p["identity"]["display_name"] for p in state["participants"])
    assert names == ["Creator Agent", "Second Agent"]
    creator = next(p for p in state["participants"] if p["id"] == created["participant_id"])
    assert creator["role"] == "owner"

    # And the whole thing is auditable from seq 1 with no gaps.
    events = await eventlog.read_since(created["room_id"], 0)
    assert [e.seq for e in events] == list(range(1, len(events) + 1))
    assert [e.type.value for e in events][:3] == [
        "room.created",
        "participant.joined",
        "invitation.created",
    ]


async def test_creating_a_room_needs_a_user_principal_not_a_room_token(fresh_db, org, make_room):
    """Creating a room is an org-level act, so a room-scoped token must not do it."""
    room = await make_room()
    result = await mcp_tools.create_room(principal_token=room.owner_token, name="should not work")
    assert result["ok"] is False
    assert result["error"] in {"forbidden", "unauthenticated"}


async def test_execution_mode_is_required_and_validated(fresh_db, org, make_room):
    """No default: guessing how a client runs is worse than asking it."""
    room = await make_room()
    result = await mcp_tools.join_room(
        invitation_token=room.join_token,
        display_name="Confused agent",
        execution_mode="whatever",
    )
    assert result["ok"] is False
    assert result["error"] == "invalid_command"
    assert "human_turn_only" in result["message"]


async def test_human_turn_only_client_gets_short_leases_and_is_told_why(fresh_db, org, make_room):
    """A ChatGPT-class connector: real participant, honestly graded, short leases.

    The room must not quietly treat it as autonomous, and it must not be shut out either.
    """
    from app.domain.capabilities import ATTENDED_MAX_LEASE_SECONDS
    from app.domain.room import RoomPolicy

    room = await make_room(policy=RoomPolicy(allow_attended_claims=True))
    joined = await mcp_tools.join_room(
        invitation_token=room.join_token,
        display_name="ChatGPT (connector)",
        execution_mode="human_turn_only",
    )
    assert joined["ok"] is True
    caps = joined["negotiated_capabilities"]
    assert "requires_human_presence" in caps
    assert "can_execute_background" not in caps
    assert "supports_push" not in caps
    assert joined["may_claim"] is True
    assert joined["max_lease_seconds"] <= ATTENDED_MAX_LEASE_SECONDS
    # The explanation has to say the useful part out loud.
    assert "human" in joined["what_this_means"].lower()

    # Other participants see the honest grade, not an optimistic one.
    state = await mcp_tools.get_room_state(participant_token=joined["participant_token"])
    me = next(p for p in state["participants"] if p["id"] == joined["participant_id"])
    assert me["presence"]["liveness"] == "attended"
    assert me["presence"]["runtime"]["lease_renewable_unattended"] is False


async def test_observer_mode_cannot_claim_but_can_still_coordinate(fresh_db, org, make_room):
    room = await make_room()
    joined = await mcp_tools.join_room(
        invitation_token=room.join_token,
        display_name="Watcher",
        execution_mode="observer",
    )
    assert joined["ok"] is True
    assert joined["may_claim"] is False
    assert "supports_tools" in (joined["claim_denied_reason"] or "")

    posted = await mcp_tools.post_message(
        body="I am watching, not working.", participant_token=joined["participant_token"]
    )
    assert posted["ok"] is True


async def test_one_user_can_bring_several_agents_as_separate_participants(fresh_db, org):
    """The core premise: a person brings Claude Code *and* Codex *and* ChatGPT.

    Each is its own seat with its own presence and leases. An earlier version returned one
    identity per user, which silently capped everyone at one seat and turned a second join
    into a rejoin that rotated the first seat's token away.
    """
    org_id, user_id = org
    principal = await _owner_token(org_id, user_id)

    created = await mcp_tools.create_room(
        principal_token=principal, name="My agents", display_name="Alan"
    )
    assert created["ok"] is True

    seats = {}
    for name, mode in (
        ("Claude Code", "unattended_loop"),
        ("Codex", "unattended_loop"),
        ("ChatGPT", "human_turn_only"),
    ):
        joined = await mcp_tools.join_room(
            invitation_token=created["join_token"], display_name=name, execution_mode=mode
        )
        assert joined["ok"] is True, joined
        seats[name] = joined

    # Four distinct participants, all owned by one human.
    assert len({s["participant_id"] for s in seats.values()}) == 3
    assert created["participant_id"] not in {s["participant_id"] for s in seats.values()}

    # Every earlier token still works — no seat was rotated away by a later join.
    for name, seat in seats.items():
        reloaded = await store.load_participant_by_token(seat["participant_token"])
        assert reloaded.identity.display_name == name
    owner = await store.load_participant_by_token(created["participant_token"])
    assert owner.role.value == "owner"

    # And they are graded independently, which is the whole point of separate seats.
    state = await mcp_tools.get_room_state(participant_token=created["participant_token"])
    grades = {
        p["identity"]["display_name"]: p["presence"]["liveness"] for p in state["participants"]
    }
    assert grades["Claude Code"] == "live_poll"
    assert grades["Codex"] == "live_poll"
    assert grades["ChatGPT"] == "attended"


async def test_rejoining_under_the_same_name_is_a_rejoin_not_a_new_seat(fresh_db, org):
    """Reconnecting must not litter the room with ghosts of itself."""
    org_id, user_id = org
    principal = await _owner_token(org_id, user_id)
    created = await mcp_tools.create_room(
        principal_token=principal, name="Restart room", display_name="Alan"
    )

    first = await mcp_tools.join_room(
        invitation_token=created["join_token"],
        display_name="Claude Code",
        execution_mode="unattended_loop",
    )
    second = await mcp_tools.join_room(
        invitation_token=created["join_token"],
        display_name="Claude Code",
        execution_mode="unattended_loop",
    )
    assert first["participant_id"] == second["participant_id"], "same seat, stable id"

    participants = await store.list_participants(created["room_id"])
    assert sorted(p.identity.display_name for p in participants) == ["Alan", "Claude Code"]

    # The rejoin rotated the token, which is documented behaviour (PROTOCOL §3).
    from app.core.errors import Unauthenticated

    with pytest.raises(Unauthenticated):
        await store.load_participant_by_token(first["participant_token"])
    assert (await store.load_participant_by_token(second["participant_token"])).id == second[
        "participant_id"
    ]


async def test_rejoining_never_demotes_an_existing_participant(fresh_db, org, make_room):
    """An owner who redeems their own room's collaborator join link keeps owner.

    Found by a smoke test: the creator re-joined through the default join link and was
    silently downgraded to collaborator, losing `room.admin` in their own room. Redeeming
    an invitation must never *reduce* standing.
    """
    from app.domain.commands import JoinRoomCommand
    from app.domain.room import Scope

    room = await make_room()
    owner_identity_id = room.owner.agent_identity_id
    assert room.owner.role.value == "owner"

    identity_row = await db.fetch_one(
        "SELECT * FROM agent_identities WHERE id = ?", (owner_identity_id,)
    )
    rejoined = await rooms.join_room(
        identity=store.to_identity(identity_row),
        command=JoinRoomCommand(invitation_token=room.join_token, display_name="Room Owner"),
    )

    # Same participant row (ids are stable, so the audit trail stays readable)...
    assert rejoined.participant.id == room.owner.id
    # ...and still an owner, with admin intact.
    assert rejoined.participant.role.value == "owner"
    assert Scope.ROOM_ADMIN in rejoined.participant.scopes

    # The returned token works; the old one is rotated out, which is expected on rejoin.
    reloaded = await store.load_participant_by_token(rejoined.participant_token)
    assert reloaded.id == room.owner.id


async def test_a_higher_role_invitation_still_promotes(fresh_db, org, make_room):
    """Never-demote must not become never-change: a real promotion has to work."""
    from app.domain.commands import CreateInvitationCommand, JoinRoomCommand
    from app.domain.room import ParticipantRole, Scope

    room = await make_room()
    observer_invite = await rooms.create_invitation(
        participant=room.owner,
        command=CreateInvitationCommand(role=ParticipantRole.OBSERVER),
    )
    identity = await rooms.create_identity(
        org_id=room.org_id, owner_user_id=room.owner_user_id, display_name="Climber"
    )
    first = await rooms.join_room(
        identity=identity,
        command=JoinRoomCommand(invitation_token=observer_invite.token, display_name="Climber"),
    )
    assert first.participant.role == ParticipantRole.OBSERVER
    assert Scope.TASK_CLAIM not in first.participant.scopes

    promoted = await rooms.join_room(
        identity=identity,
        command=JoinRoomCommand(invitation_token=room.join_token, display_name="Climber"),
    )
    assert promoted.participant.role == ParticipantRole.COLLABORATOR
    assert Scope.TASK_CLAIM in promoted.participant.scopes


async def test_display_name_is_per_room(fresh_db, org, make_room):
    """The same identity may present differently in different rooms.

    The participants table carried a `display_name` column that was written on join and
    never read, so rooms showed the identity-level name instead.
    """
    from app.domain.commands import JoinRoomCommand

    room_a = await make_room(name="Room A")
    room_b = await make_room(name="Room B")

    identity = await rooms.create_identity(
        org_id=room_a.org_id, owner_user_id=room_a.owner_user_id, display_name="Agent"
    )
    in_a = await rooms.join_room(
        identity=identity,
        command=JoinRoomCommand(invitation_token=room_a.join_token, display_name="Reviewer"),
    )
    in_b = await rooms.join_room(
        identity=identity,
        command=JoinRoomCommand(invitation_token=room_b.join_token, display_name="Builder"),
    )

    assert in_a.participant.identity.display_name == "Reviewer"
    assert in_b.participant.identity.display_name == "Builder"

    # And the room listing agrees, not just the join response.
    names_a = {p.identity.display_name for p in await store.list_participants(room_a.room.id)}
    names_b = {p.identity.display_name for p in await store.list_participants(room_b.room.id)}
    assert "Reviewer" in names_a and "Builder" not in names_a
    assert "Builder" in names_b and "Reviewer" not in names_b


async def test_default_join_link_is_reusable_by_several_agents(fresh_db, org, make_room):
    """One token, many joiners — that is the point of the shared join link."""
    room = await make_room()
    for i in range(3):
        joined = await mcp_tools.join_room(
            invitation_token=room.join_token,
            display_name=f"Agent {i}",
            execution_mode="unattended_loop",
        )
        assert joined["ok"] is True, joined

    state = await mcp_tools.get_room_state(participant_token=room.owner_token)
    assert len([p for p in state["participants"] if p["state"] == "joined"]) == 4


# ---------------------------------------------------------------------------
# Two transports, one room (M1 exit criterion 1)
# ---------------------------------------------------------------------------


async def test_three_execution_modes_coexist_with_honest_grades(fresh_db, org, make_room, join):
    """One room, one join token, three genuinely different kinds of participant.

    This is the shape the product has to get right: an autonomous local agent, a
    human-driven connector, and a pushable client all in the same room, each rendered as
    what it actually is. If any of them were flattened into the others, participants would
    coordinate against a false assumption — which is the failure mode the whole capability
    model exists to prevent.
    """
    from app.core import projections
    from app.core import work as work_service
    from app.domain.commands import DeclareWorkCommand
    from app.domain.room import RoomPolicy

    room = await make_room(
        name="Cross-transport room", policy=RoomPolicy(allow_attended_claims=True)
    )

    # 1. A pushable client (browser console / native ARP over SSE).
    sse = await join(room, display_name="SSE Agent", transport="sse")
    await work_service.declare(
        participant=sse.participant,
        command=DeclareWorkCommand(
            headline="Refactoring the stream handler", targets=["app/api/routes.py"]
        ),
    )

    # 2. An autonomous local agent over MCP.
    local = await mcp_tools.join_room(
        invitation_token=room.join_token,
        display_name="Claude Code",
        execution_mode="unattended_loop",
        description="local coding agent",
    )
    assert local["ok"] is True
    # MCP cannot be pushed to, so it must not claim push however it was labelled.
    assert local["delivery_mode"] == "long_poll"
    assert "supports_push" not in local["negotiated_capabilities"]
    assert local["may_claim"] is True

    # 3. A human-driven connector over MCP (ChatGPT in a browser tab).
    connector = await mcp_tools.join_room(
        invitation_token=room.join_token,
        display_name="ChatGPT (connector)",
        execution_mode="human_turn_only",
    )
    assert connector["ok"] is True
    assert connector["may_claim"] is True
    assert connector["max_lease_seconds"] < local["max_lease_seconds"], (
        "a human-driven client must get a shorter lease than an autonomous one"
    )

    # The local agent sees both others, each graded honestly.
    state = await mcp_tools.get_room_state(participant_token=local["participant_token"])
    assert "Refactoring the stream handler" in [w["headline"] for w in state["work"]]

    by_id = {p["id"]: p for p in state["participants"]}
    assert by_id[sse.participant.id]["presence"]["liveness"] == "live_push"
    assert by_id[connector["participant_id"]]["presence"]["liveness"] == "attended"
    assert (
        by_id[connector["participant_id"]]["presence"]["runtime"]["lease_renewable_unattended"]
        is False
    )

    # And the SSE participant sees the MCP ones as poll/attended — not as push.
    sse_state = await projections.snapshot(room_id=room.room.id, recipient=sse.participant)
    sse_by_id = {p["id"]: p for p in sse_state["participants"]}
    assert sse_by_id[local["participant_id"]]["presence"]["liveness"] == "live_poll"
    assert sse_by_id[local["participant_id"]]["presence"]["runtime"]["delivery_mode"] == "long_poll"
    assert sse_by_id[connector["participant_id"]]["presence"]["liveness"] == "attended"


# ---------------------------------------------------------------------------
# MCP long-poll semantics (M1 exit criterion 2, poll side)
# ---------------------------------------------------------------------------


async def test_mcp_long_poll_returns_missed_events_and_advances_its_cursor(
    fresh_db, org, make_room, join
):
    room = await make_room()
    from .conftest import FULL_CAPABILITIES

    joined = await mcp_tools.join_room(
        invitation_token=room.join_token,
        display_name="Poller",
        execution_mode="unattended_loop",
    )
    token = joined["participant_token"]
    cursor = joined["cursor"]

    # An empty poll returns promptly and says so honestly.
    empty = await mcp_tools.await_room_events(
        since_seq=cursor, timeout_seconds=1, participant_token=token
    )
    assert empty["ok"] is True
    assert empty["timed_out"] is True
    assert empty["events"] == []
    assert empty["cursor"] == cursor

    # Something happens while the poller is away.
    other = await join(room, display_name="Doer")
    created = await tasks.create(
        participant=other.participant,
        command=__import__("app.domain.commands", fromlist=["CreateTaskCommand"]).CreateTaskCommand(
            title="Something to notice", targets=["x.py"]
        ),
    )

    got = await mcp_tools.await_room_events(
        since_seq=cursor, timeout_seconds=1, participant_token=token
    )
    types = [e["type"] for e in got["events"]]
    assert "task.created" in types
    assert got["cursor"] > cursor
    assert got["timed_out"] is False

    # The cursor is usable: polling again from it yields nothing new.
    again = await mcp_tools.await_room_events(
        since_seq=got["cursor"], timeout_seconds=1, participant_token=token
    )
    assert again["events"] == []
    del created


async def test_mcp_errors_are_actionable_data_not_exceptions(fresh_db, org, make_room, join):
    """An agent must be able to branch on the reason, so errors come back as data."""
    room = await make_room()
    joined = await mcp_tools.join_room(
        invitation_token=room.join_token,
        display_name="Poller",
        execution_mode="unattended_loop",
    )
    token = joined["participant_token"]

    holder = await join(room, display_name="Holder")
    task = await tasks.create(
        participant=holder.participant,
        command=__import__("app.domain.commands", fromlist=["CreateTaskCommand"]).CreateTaskCommand(
            title="Contended", targets=["y.py"]
        ),
    )
    await tasks.claim(
        participant=holder.participant,
        command=__import__("app.domain.commands", fromlist=["ClaimTaskCommand"]).ClaimTaskCommand(
            task_id=task.id
        ),
    )

    result = await mcp_tools.claim_task(task_id=task.id, participant_token=token)
    assert result["ok"] is False
    assert result["error"] == "lease_conflict"
    assert result["details"]["held_by_display_name"] == "Holder"

    # A refused disclosure is likewise data.
    refused = await mcp_tools.post_message(
        body="key sk-abcdefghijklmnopqrstuvwxyz012345", participant_token=token
    )
    assert refused["ok"] is False
    assert refused["error"] == "privacy_violation"


# ---------------------------------------------------------------------------
# SSE resume (M1 exit criterion 2, push side)
# ---------------------------------------------------------------------------


async def _read_sse_frames(
    *, room_id: str, participant, since_seq: int, want: int
) -> list[tuple[str, dict]]:
    """Drive the stream endpoint's generator directly and collect `want` frames.

    Deliberately not via `httpx.ASGITransport`: that transport never signals client
    disconnect, so `request.is_disconnected()` stays False and the generator loops on
    keepalives until the test times out. Calling the route and closing the iterator
    ourselves tests *our* stream logic — snapshot boundary, cursor advance, frame
    encoding — which is the part that can actually be wrong.
    """
    from app.api import routes

    class _FakeRequest:
        async def is_disconnected(self) -> bool:
            return False

    response = await routes.stream(
        room_id=room_id,
        request=_FakeRequest(),  # type: ignore[arg-type]
        participant=participant,
        since_seq=since_seq,
        connection_id=None,
    )

    frames: list[tuple[str, dict]] = []
    iterator = response.body_iterator.__aiter__()
    event_name: str | None = None
    try:
        while len(frames) < want:
            chunk = await asyncio.wait_for(iterator.__anext__(), timeout=10)
            text = chunk if isinstance(chunk, str) else chunk.decode("utf-8")
            for line in text.splitlines():
                if line.startswith("event: "):
                    event_name = line[7:].strip()
                elif line.startswith("data: ") and event_name:
                    frames.append((event_name, json.loads(line[6:])))
    finally:
        await iterator.aclose()  # type: ignore[attr-defined]
    return frames


async def test_sse_stream_opens_with_a_snapshot_then_streams_events(fresh_db, org, make_room, join):
    from app.domain.commands import CreateTaskCommand

    room = await make_room()
    member = await join(room, display_name="Watcher")

    await tasks.create(
        participant=member.participant,
        command=CreateTaskCommand(title="Already there", targets=["z.py"]),
    )

    frames = await _read_sse_frames(
        room_id=room.room.id, participant=member.participant, since_seq=0, want=1
    )

    assert frames[0][0] == "snapshot"
    body = frames[0][1]
    assert "snapshot_seq" in body
    assert any(t["title"] == "Already there" for t in body["tasks"])


async def test_sse_resume_from_a_cursor_skips_what_was_already_seen(fresh_db, org, make_room, join):
    """Resuming with a cursor must not replay the snapshot or re-send old events."""
    from app.core import eventlog, messages
    from app.domain.commands import PostMessageCommand

    room = await make_room()
    member = await join(room, display_name="Watcher")

    cursor = await eventlog.current_seq(room.room.id)
    await messages.post(
        participant=member.participant, command=PostMessageCommand(body="after the cursor")
    )

    frames = await _read_sse_frames(
        room_id=room.room.id, participant=member.participant, since_seq=cursor, want=1
    )

    assert frames[0][0] == "message.posted", "a cursored resume must not send a snapshot"
    assert frames[0][1]["payload"]["body"] == "after the cursor"
    assert frames[0][1]["seq"] > cursor


async def test_sse_resume_gap_is_signalled_before_a_fresh_snapshot(fresh_db, org, make_room, join):
    """A client whose cursor fell below the retained floor must be *told*, then
    re-snapshotted — never handed a silent partial replay."""
    from app.core import messages
    from app.domain.commands import PostMessageCommand

    room = await make_room()
    member = await join(room, display_name="Watcher")
    for i in range(4):
        await messages.post(
            participant=member.participant, command=PostMessageCommand(body=f"m{i}")
        )
    await db.execute("UPDATE rooms SET retained_from_seq = ? WHERE id = ?", (4, room.room.id))

    frames = await _read_sse_frames(
        room_id=room.room.id, participant=member.participant, since_seq=1, want=2
    )

    assert frames[0][0] == "resume_gap"
    assert frames[0][1]["error"] == "resume_gap"
    assert frames[1][0] == "snapshot", "a gap must be followed by a fresh snapshot"


# ---------------------------------------------------------------------------
# Conflict surfacing (AVOID CONFLICTS step)
# ---------------------------------------------------------------------------


async def test_overlapping_targets_raise_a_conflict_without_blocking(
    fresh_db, org, make_room, join
):
    """The room warns; it never blocks. Both declarations must survive."""
    from app.core import work as work_service
    from app.domain.commands import DeclareWorkCommand

    room = await make_room()
    alice = await join(room, display_name="Alice")
    bob = await join(room, display_name="Bob")

    first = await work_service.declare(
        participant=alice.participant,
        command=DeclareWorkCommand(headline="Rewriting auth", targets=["./src/Auth.py"]),
    )
    second = await work_service.declare(
        participant=bob.participant,
        # Different spelling of the same file: normalization must catch it.
        command=DeclareWorkCommand(headline="Patching auth bug", targets=["src/auth.py"]),
    )

    assert first.ended_at is None and second.ended_at is None, "neither may be blocked"

    conflicts = await store.list_conflicts(room.room.id)
    overlaps = [c for c in conflicts if c.kind.value == "overlapping_work"]
    assert len(overlaps) == 1
    assert set(overlaps[0].participant_ids) == {alice.participant.id, bob.participant.id}
    assert "src/auth.py" in overlaps[0].detail
    # The detail must be readable by the humans on the board, not just machines.
    assert "Bob" in overlaps[0].detail or "Alice" in overlaps[0].detail


async def test_duplicate_task_is_flagged_but_still_created(fresh_db, org, make_room, join):
    from app.domain.commands import CreateTaskCommand

    room = await make_room()
    alice = await join(room, display_name="Alice")
    bob = await join(room, display_name="Bob")

    await tasks.create(
        participant=alice.participant,
        command=CreateTaskCommand(title="Update the changelog", targets=["CHANGELOG.md"]),
    )
    second = await tasks.create(
        participant=bob.participant,
        command=CreateTaskCommand(title="update the changelog", targets=["CHANGELOG.md"]),
    )

    assert second.status.value == "open", "the duplicate is still created"
    conflicts = await store.list_conflicts(room.room.id)
    dupes = [c for c in conflicts if c.kind.value == "duplicate_task"]
    assert len(dupes) == 1
    assert "merge" in dupes[0].detail


async def test_room_closure_stops_writes_but_not_reads(fresh_db, org, make_room, join):
    room = await make_room()
    member = await join(room, display_name="Member")

    await rooms.close_room(participant=room.owner)

    from app.core import projections
    from app.core.errors import RoomClosed
    from app.domain.commands import CreateTaskCommand

    # Reads still work: a participant must be able to see why work stopped.
    snapshot = await projections.snapshot(room_id=room.room.id, recipient=member.participant)
    assert snapshot["room"]["status"] == "closed"

    with pytest.raises(RoomClosed):
        await tasks.create(
            participant=member.participant, command=CreateTaskCommand(title="too late")
        )


async def test_expired_room_is_closed_by_the_janitor(fresh_db, org, make_room):
    room = await make_room()
    await db.execute("UPDATE rooms SET expires_at = ? WHERE id = ?", (iso_in(-60), room.room.id))

    closed = await rooms.expire_due_rooms()
    assert room.room.id in closed

    refreshed = await store.load_room(room.room.id)
    assert refreshed.status.value == "closed"

    # Idempotent: a second sweep must not re-close or re-emit.
    assert await rooms.expire_due_rooms() == []
