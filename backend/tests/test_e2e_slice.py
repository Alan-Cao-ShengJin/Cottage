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
from app.core import rooms, store, tasks
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

        # The creator needs to be a participant to administer the room; join via a
        # bootstrap owner invitation created by the service layer.
        owner = await _bootstrap_owner(room_id, org_id, user_id)
        owner_auth = {"Authorization": f"Bearer {owner}"}

        invite = _ok(
            await client.post(
                f"/api/rooms/{room_id}/invitations",
                json={"role": "collaborator", "max_redemptions": 5},
                headers=owner_auth,
            )
        )
        invitation_token = invite["token"]
        assert "token" not in invite["invitation"], "the invitation record must not carry the token"

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


async def _bootstrap_owner(room_id: str, org_id: str, user_id: str) -> str:
    """Make the room creator a participant with admin scopes.

    M1 has no "creator is automatically a participant" step because membership has
    exactly one entry path (invitation redemption) and the first invitation needs an
    admin to exist. M5's real auth flow closes this loop; until then, tests and the
    dev bootstrap seed it directly.
    """
    from app.core.authz import effective_scopes
    from app.domain import ids
    from app.domain.capabilities import HostClass
    from app.domain.identity import PrincipalKind, TrustTier
    from app.domain.room import ParticipantRole
    from app.util import hash_token, new_token, utcnow_iso

    identity = await rooms.create_identity(
        org_id=org_id,
        owner_user_id=user_id,
        display_name="Owner",
        kind=PrincipalKind.HUMAN,
        host_class=HostClass.BROWSER_HUMAN,
    )
    token = new_token()
    await db.execute(
        """
        INSERT INTO participants (
            id, room_id, agent_identity_id, org_id, role, scopes, trust, state,
            display_name, token_hash, joined_at
        ) VALUES (?,?,?,?,'owner',?,'member','joined','Owner',?,?)
        """,
        (
            ids.new_id(ids.PARTICIPANT),
            room_id,
            identity.id,
            org_id,
            db.dumps(
                [s.value for s in effective_scopes(ParticipantRole.OWNER, None, TrustTier.MEMBER)]
            ),
            hash_token(token),
            utcnow_iso(),
        ),
    )
    return token


# ---------------------------------------------------------------------------
# Two transports, one room (M1 exit criterion 1)
# ---------------------------------------------------------------------------


async def test_sse_participant_and_mcp_participant_see_each_other(fresh_db, org, make_room):
    """An SSE participant and an MCP long-poll participant must each see the other's
    presence, negotiated capabilities, and current work — with honest grades."""
    room = await make_room(name="Cross-transport room")

    # The SSE side, via the service layer for brevity (its HTTP path is covered above).
    from app.core import presence
    from app.core import work as work_service
    from app.domain.commands import ConnectCommand, DeclareWorkCommand

    from .conftest import FULL_CAPABILITIES

    owner = await store.load_participant_by_token(
        await _bootstrap_owner(room.room.id, room.org_id, room.owner_user_id)
    )
    invite = await rooms.create_invitation(
        participant=owner,
        command=__import__(
            "app.domain.commands", fromlist=["CreateInvitationCommand"]
        ).CreateInvitationCommand(max_redemptions=5),
    )

    sse_identity = await rooms.create_identity(
        org_id=room.org_id,
        owner_user_id=room.owner_user_id,
        display_name="SSE Agent",
        capabilities=FULL_CAPABILITIES,
    )
    sse_join = await rooms.join_room(
        identity=sse_identity,
        command=__import__("app.domain.commands", fromlist=["JoinRoomCommand"]).JoinRoomCommand(
            invitation_token=invite.token,
            display_name="SSE Agent",
            capabilities=FULL_CAPABILITIES,
        ),
    )
    await presence.connect(
        participant=sse_join.participant,
        command=ConnectCommand(capabilities=FULL_CAPABILITIES),
        transport="sse",
    )
    await work_service.declare(
        participant=sse_join.participant,
        command=DeclareWorkCommand(
            headline="Refactoring the stream handler", targets=["app/api/routes.py"]
        ),
    )

    # The MCP side, through the adapter's own tools.
    invite2 = await rooms.create_invitation(
        participant=owner,
        command=__import__(
            "app.domain.commands", fromlist=["CreateInvitationCommand"]
        ).CreateInvitationCommand(max_redemptions=5),
    )
    mcp_join = await mcp_tools.join_room(
        invitation_token=invite2.token,
        display_name="Claude Code",
        description="local coding agent",
    )
    assert mcp_join["ok"] is True
    # Honest grading: MCP cannot be pushed to, so it must not claim push.
    assert mcp_join["delivery_mode"] == "long_poll"
    assert "supports_poll" in mcp_join["negotiated_capabilities"]
    assert "supports_push" not in mcp_join["negotiated_capabilities"]
    assert mcp_join["may_claim"] is True

    mcp_token = mcp_join["participant_token"]

    # The MCP participant sees the SSE participant's live work.
    state = await mcp_tools.get_room_state(participant_token=mcp_token)
    headlines = [w["headline"] for w in state["work"]]
    assert "Refactoring the stream handler" in headlines
    sse_view = next(p for p in state["participants"] if p["id"] == sse_join.participant.id)
    assert sse_view["presence"]["liveness"] == "live_push"

    # And the SSE participant sees the MCP one, graded as poll — not as push.
    from app.core import projections

    sse_state = await projections.snapshot(room_id=room.room.id, recipient=sse_join.participant)
    mcp_view = next(p for p in sse_state["participants"] if p["id"] == mcp_join["participant_id"])
    assert mcp_view["presence"]["liveness"] == "live_poll"
    assert mcp_view["presence"]["runtime"]["delivery_mode"] == "long_poll"


# ---------------------------------------------------------------------------
# MCP long-poll semantics (M1 exit criterion 2, poll side)
# ---------------------------------------------------------------------------


async def test_mcp_long_poll_returns_missed_events_and_advances_its_cursor(
    fresh_db, org, make_room, join
):
    room = await make_room()
    from .conftest import FULL_CAPABILITIES

    owner = await store.load_participant_by_token(
        await _bootstrap_owner(room.room.id, room.org_id, room.owner_user_id)
    )
    invite = await rooms.create_invitation(
        participant=owner,
        command=__import__(
            "app.domain.commands", fromlist=["CreateInvitationCommand"]
        ).CreateInvitationCommand(max_redemptions=5),
    )
    joined = await mcp_tools.join_room(invitation_token=invite.token, display_name="Poller")
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
    owner = await store.load_participant_by_token(
        await _bootstrap_owner(room.room.id, room.org_id, room.owner_user_id)
    )
    invite = await rooms.create_invitation(
        participant=owner,
        command=__import__(
            "app.domain.commands", fromlist=["CreateInvitationCommand"]
        ).CreateInvitationCommand(max_redemptions=5),
    )
    joined = await mcp_tools.join_room(invitation_token=invite.token, display_name="Poller")
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
    owner = await store.load_participant_by_token(
        await _bootstrap_owner(room.room.id, room.org_id, room.owner_user_id)
    )

    await rooms.close_room(participant=owner)

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
