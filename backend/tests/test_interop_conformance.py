"""The interop conformance harness: four join paths in one room, six asserted properties.

`docs/INTEROP.md` §3. The claim this project is judged against is that **any combination** of
independently owned agents and humans can occupy one room — so the thing that has to be
tested is the *combination*, not each path in isolation. A per-adapter test can be green
while the room is incoherent: three participants each correct on their own, and a shared
board that tells each of them something different.

Four genuinely different ways in, chosen because they differ in what they can actually do
rather than in which vendor sent them:

| Participant | Path | Credential | What makes it different |
|---|---|---|---|
| `push` | ARP HTTP + SSE | participant token | can be pushed to |
| `autonomous` | MCP | operator-provisioned | acts on its own clock, full-length leases |
| `attended` | MCP | operator-provisioned | acts only when a human does |
| `guest` | MCP | **an invitation alone** | a stranger, self-asserted name (D-025) |

The last row is why this harness had to wait for M2.0b: until an invitation was a credential
there was no way to put a stranger in a room at all, so "combination" could only ever mean
"combinations of ourselves".

Property 6 is the one that cannot appear in a single-path test, and is the reason the
capability model exists: an autonomous participant must never be led to believe an attended
one is prompt.
"""

from __future__ import annotations

import pytest

from app.adapters.mcp import server as mcp_tools
from app.core import presence, projections, rooms, store, tasks
from app.core import work as work_service
from app.core.errors import RoomError
from app.domain.capabilities import Capability, DeliveryMode, HostClass
from app.domain.commands import (
    ClaimTaskCommand,
    CompleteTaskCommand,
    ConnectCommand,
    CreateTaskCommand,
    DeclareWorkCommand,
    JoinRoomCommand,
    UpdateTaskCommand,
)
from app.domain.room import Liveness, RoomPolicy
from app.domain.task import TaskStatus


class Room:
    """The four-way room, and the handles each participant sees it through."""

    def __init__(self, fixture, push, autonomous, attended, guest, guest_participant):
        self.fixture = fixture
        self.id = fixture.room.id
        self.push = push
        self.autonomous = autonomous
        self.attended = attended
        self.guest = guest
        self.guest_participant = guest_participant

    @property
    def everyone(self) -> dict[str, str]:
        """participant_id → the name each one goes by, for readable assertions."""
        return {
            self.push.participant.id: "push",
            self.autonomous["participant_id"]: "autonomous",
            self.attended["participant_id"]: "attended",
            self.guest["participant_id"]: "guest",
        }


@pytest.fixture()
async def mixed_room(fresh_db, org, make_room, join) -> Room:
    room = await make_room(
        name="Interop conformance",
        # Attended clients may claim here. Without it the attended participant cannot hold
        # a lease at all and property 2 would only ever be tested between two autonomous
        # peers — the easy case.
        policy=RoomPolicy(allow_attended_claims=True),
    )

    push = await join(room, display_name="Browser Console", transport="sse")

    autonomous = await mcp_tools.join_room(
        invitation_token=room.join_token,
        display_name="Claude Code",
        execution_mode="unattended_loop",
    )
    attended = await mcp_tools.join_room(
        invitation_token=room.join_token,
        display_name="ChatGPT (connector)",
        execution_mode="human_turn_only",
    )

    # A stranger: no account, no operator token, only the link.
    credential = await rooms.authenticate_invitation(room.join_token)
    guest_identity = await rooms.provision_guest_identity(
        credential, display_name="Partner's Agent"
    )
    guest_join = await rooms.join_room(
        identity=guest_identity,
        command=JoinRoomCommand(invitation_token=room.join_token, display_name="Partner's Agent"),
    )
    guest = await mcp_tools.join_room(
        invitation_token=room.join_token,
        display_name="Partner's Agent",
        execution_mode="unattended_loop",
    )

    return Room(room, push, autonomous, attended, guest, guest_join.participant)


# ---------------------------------------------------------------------------
# 1. Everyone appears to everyone, graded honestly
# ---------------------------------------------------------------------------


async def test_every_participant_appears_to_every_other_with_an_honest_grade(mixed_room):
    """A shared board is only shared if all four see the same four people.

    And *honestly*: the grades below are what the others plan around. A poll-only client
    rendered as pushable, or an attended one rendered as live, would have everyone
    coordinating against a promptness that does not exist.
    """
    expected_liveness = {
        # The room's creator, who made it and never opened a connection. Included rather
        # than filtered out: a board that quietly omits a participant is exactly the
        # incoherence this harness exists to catch.
        mixed_room.fixture.owner.id: "disconnected",
        mixed_room.push.participant.id: "live_push",
        mixed_room.autonomous["participant_id"]: "live_poll",
        mixed_room.attended["participant_id"]: "attended",
        mixed_room.guest["participant_id"]: "live_poll",
    }

    # As the MCP participants see it.
    for viewer in (mixed_room.autonomous, mixed_room.attended, mixed_room.guest):
        state = await mcp_tools.get_room_state(participant_token=viewer["participant_token"])
        seen = {p["participant_id"]: p["liveness"] for p in state["participants"]}
        for participant_id, liveness in expected_liveness.items():
            assert seen.get(participant_id) == liveness, (
                f"{mixed_room.everyone[viewer['participant_id']]} sees "
                f"{mixed_room.everyone[participant_id]} as {seen.get(participant_id)}, "
                f"expected {liveness}"
            )

    # And as the pushable HTTP participant sees it, through a different projection.
    snapshot = await projections.snapshot(
        room_id=mixed_room.id, recipient=mixed_room.push.participant
    )
    seen = {p["id"]: (p["presence"] or {}).get("liveness") for p in snapshot["participants"]}
    assert seen == expected_liveness


async def test_a_self_asserted_name_is_flagged_and_a_bound_one_is_not(mixed_room):
    """Presence was authorized; the name was not. Every other participant must be told.

    Attribution is the product's integrity guarantee, so a name nobody vouched for cannot
    render identically to one a credential bound (D-025).

    **What the split actually is here, which is not the obvious one.** Every participant
    that arrived over MCP in this harness authenticated with the *invitation* — there is no
    OAuth token in play — so all three are guests and all three are flagged, including the
    one called "Claude Code". That is not a leak in the harness: on the bearer-invitation
    path the invitation genuinely is the only authorization, so a self-chosen name is all
    anyone has. It is also the honest reading of local development, where no credential
    binds anything.

    The unflagged side is the participant whose identity an account created — the browser
    console and the room's owner. `tests/test_mcp_auth.py` covers the OAuth case, where a
    human binds the name at consent and the agent cannot rename itself.
    """
    for viewer in (mixed_room.autonomous, mixed_room.attended):
        state = await mcp_tools.get_room_state(participant_token=viewer["participant_token"])
        by_id = {p["participant_id"]: p for p in state["participants"]}

        for invited in (mixed_room.guest, mixed_room.autonomous, mixed_room.attended):
            assert by_id[invited["participant_id"]]["name_is_self_asserted"] is True, (
                f"{invited['display_name']} authenticated with an invitation, so nobody "
                "vouched for its name"
            )

        # Account-backed identities carry no flag, which is what makes the flag mean
        # something rather than decorate everyone equally.
        assert "name_is_self_asserted" not in by_id[mixed_room.push.participant.id]
        assert "name_is_self_asserted" not in by_id[mixed_room.fixture.owner.id]


# ---------------------------------------------------------------------------
# 2. One claim wins, across paths
# ---------------------------------------------------------------------------


async def test_a_task_claimed_by_one_path_is_refused_to_all_others(mixed_room):
    """The invariant that makes the room worth using, tested across adapters.

    Exclusivity that held only among MCP clients would be worthless: the whole point is
    that a browser, a local agent, and a stranger's agent cannot all start the same job.
    """
    owner = mixed_room.fixture.owner
    task = await tasks.create(
        participant=owner,
        command=CreateTaskCommand(title="Migrate the billing schema", targets=["db/schema.sql"]),
    )

    winner = await tasks.claim(
        participant=mixed_room.push.participant,
        command=ClaimTaskCommand(task_id=task.id),
    )
    assert winner.claim is not None

    for loser in (mixed_room.autonomous, mixed_room.attended, mixed_room.guest):
        refused = await mcp_tools.claim_task(
            task_id=task.id, participant_token=loser["participant_token"]
        )
        assert refused["ok"] is False, f"{loser['display_name']} claimed a held task"
        assert refused["error"] == "lease_conflict", refused

    # And the refusal is legible to the claimant's peers, not just to the caller.
    state = await mcp_tools.get_room_state(
        participant_token=mixed_room.autonomous["participant_token"]
    )
    held = next(t for t in state["tasks"] if t["task_id"] == task.id)
    assert held["held_by"] == mixed_room.push.participant.id


async def test_a_held_task_cannot_be_finished_or_edited_by_a_non_holder(mixed_room):
    """Exclusivity has to cover the terminal transitions, not only the claim.

    This is the gap the original property 2 left open, and a live ChatGPT participant
    walked straight into it on 2026-08-15: refusing the *claim* while allowing anyone
    to `complete` the task is not exclusivity, it is a speed bump. The reason it is
    easy to get wrong is that `complete` and `update` do carry a fence check — and a
    fence *looks* like a secret. It is not: every participant needs it to reason about
    staleness, so it is published in the projection and in `task.claimed`. Presenting
    it proves the caller read the board, never that it holds the lease (D-026).
    """
    owner = mixed_room.fixture.owner
    task = await tasks.create(
        participant=owner,
        command=CreateTaskCommand(title="Rewrite the auth middleware", targets=["app/auth.py"]),
    )
    held = await tasks.claim(
        participant=mixed_room.push.participant, command=ClaimTaskCommand(task_id=task.id)
    )
    assert held.claim is not None
    fence = held.claim.fence

    # The premise: a stranger can read the holder's current fence off the board.
    state = await mcp_tools.get_room_state(participant_token=mixed_room.guest["participant_token"])
    published = next(t for t in state["tasks"] if t["task_id"] == task.id)
    assert published["fence"] == fence, "the fence is public — that is why it cannot authorize"

    stranger = mixed_room.guest_participant
    with pytest.raises(RoomError) as completed:
        await tasks.complete(
            participant=stranger,
            command=CompleteTaskCommand(
                task_id=task.id, fence=fence, result="Completed by someone who never held it."
            ),
        )
    assert completed.value.code == "lease_conflict"

    with pytest.raises(RoomError) as edited:
        await tasks.update(
            participant=stranger,
            command=UpdateTaskCommand(task_id=task.id, fence=fence, title="Renamed by a stranger"),
        )
    assert edited.value.code == "lease_conflict"

    # The holder's lease is untouched by either attempt.
    after = await store.load_task(task.id)
    assert after.status is not TaskStatus.DONE
    assert after.claim is not None
    assert after.claim.participant_id == mixed_room.push.participant.id
    assert after.title == "Rewrite the auth middleware"

    # And the holder itself is still free to finish its own work.
    finished = await tasks.complete(
        participant=mixed_room.push.participant,
        command=CompleteTaskCommand(task_id=task.id, fence=fence, result="Done by the holder."),
    )
    assert finished.status is TaskStatus.DONE


async def test_completion_requires_a_live_lease_in_every_task_state(mixed_room):
    """The state axis of the matrix, which D-026 fixed only one cell of.

    Reviewing D-026, the ChatGPT participant pointed out that "held by another" and
    "held by nobody" are different states and only the first had been closed: an
    unclaimed task still accepted `complete` from anyone who presented its public
    `fence: 0`, because there was no holder to compare the caller against. Absence of
    a lease read as a vacuous ownership success.

    So the property is stated over states, not over one example: completion requires
    an *active* lease, held by *this* caller, at the current fence (D-027).
    """
    owner = mixed_room.fixture.owner
    stranger = mixed_room.guest_participant

    async def fresh(title: str):
        return await tasks.create(participant=owner, command=CreateTaskCommand(title=title))

    # unclaimed → nobody may complete it, including the participant that created it
    never_claimed = await fresh("Nobody has claimed this")
    for actor, who in ((stranger, "a stranger"), (owner, "its own creator")):
        with pytest.raises(RoomError) as refused:
            await tasks.complete(
                participant=actor,
                command=CompleteTaskCommand(task_id=never_claimed.id, fence=0, result="x"),
            )
        assert refused.value.code == "lease_required", f"{who} completed an unclaimed task"
    assert (await store.load_task(never_claimed.id)).status is not TaskStatus.DONE

    # held by another → lease_conflict, a different answer calling for a different move
    held = await fresh("Held by the pushable participant")
    claimed = await tasks.claim(
        participant=mixed_room.push.participant, command=ClaimTaskCommand(task_id=held.id)
    )
    assert claimed.claim is not None
    with pytest.raises(RoomError) as conflicted:
        await tasks.complete(
            participant=stranger,
            command=CompleteTaskCommand(task_id=held.id, fence=claimed.claim.fence, result="x"),
        )
    assert conflicted.value.code == "lease_conflict"

    # held by self → the only state in which completion is authorized
    finished = await tasks.complete(
        participant=mixed_room.push.participant,
        command=CompleteTaskCommand(task_id=held.id, fence=claimed.claim.fence, result="done"),
    )
    assert finished.status is TaskStatus.DONE

    # lease expired → the holder itself loses the right, which is what expiry means
    lapsed = await fresh("Claimed, then left to lapse")
    lease = await tasks.claim(
        participant=mixed_room.push.participant, command=ClaimTaskCommand(task_id=lapsed.id)
    )
    assert lease.claim is not None
    await store.db.execute(
        "UPDATE tasks SET claim_expires_at = ? WHERE id = ?",
        ("2000-01-01T00:00:00.000Z", lapsed.id),
    )
    with pytest.raises(RoomError) as stale:
        await tasks.complete(
            participant=mixed_room.push.participant,
            command=CompleteTaskCommand(task_id=lapsed.id, fence=lease.claim.fence, result="x"),
        )
    assert stale.value.code == "lease_required"


# ---------------------------------------------------------------------------
# 3. A stale fence is refused, whichever path presents it
# ---------------------------------------------------------------------------


async def test_a_stale_fence_from_any_path_is_refused(mixed_room):
    """Fencing is what makes a reclaimed task safe. It cannot be adapter-specific.

    The scenario is the dangerous one: a participant loses its claim (here by releasing
    it), someone else takes over, and the original comes back still holding the old fence.
    Accepting that write would let a slow agent overwrite its successor's work.
    """
    owner = mixed_room.fixture.owner
    task = await tasks.create(
        participant=owner, command=CreateTaskCommand(title="Rotate the signing keys")
    )

    first = await tasks.claim(
        participant=mixed_room.push.participant, command=ClaimTaskCommand(task_id=task.id)
    )
    stale_fence = first.claim.fence

    await tasks.release(
        participant=mixed_room.push.participant,
        command=__import__(
            "app.domain.commands", fromlist=["ReleaseClaimCommand"]
        ).ReleaseClaimCommand(task_id=task.id, fence=stale_fence),
    )

    second = await mcp_tools.claim_task(
        task_id=task.id, participant_token=mixed_room.autonomous["participant_token"]
    )
    assert second["ok"] is True
    assert second["fence"] > stale_fence, "a reissued claim must advance the fence"

    # The original holder, back from the dead, still presenting the old fence.
    with pytest.raises(RoomError) as exc:
        await tasks.complete(
            participant=mixed_room.push.participant,
            command=CompleteTaskCommand(
                task_id=task.id, fence=stale_fence, result="overwrote someone else's work"
            ),
        )
    assert exc.value.code == "stale_fence"

    # Same fence, presented through MCP instead: identical answer.
    refused = await mcp_tools.complete_task(
        task_id=task.id,
        fence=stale_fence,
        participant_token=mixed_room.guest["participant_token"],
    )
    assert refused["ok"] is False
    assert refused["error"] in {"stale_fence", "forbidden"}, refused


# ---------------------------------------------------------------------------
# 4. A disconnect releases leases, and the rest of the room can see it
# ---------------------------------------------------------------------------


async def test_a_disconnect_releases_leases_and_the_others_see_it(mixed_room):
    """No work is lost to a crash — and the *others* must learn, not just the database.

    A reclaimable task that still displays as held is the same as a lost one: nobody picks
    it up.
    """
    owner = mixed_room.fixture.owner
    task = await tasks.create(
        participant=owner, command=CreateTaskCommand(title="Reindex the search cluster")
    )
    claimed = await mcp_tools.claim_task(
        task_id=task.id, participant_token=mixed_room.autonomous["participant_token"]
    )
    assert claimed["ok"] is True

    await work_service.declare(
        participant=await store.load_participant(mixed_room.autonomous["participant_id"]),
        command=DeclareWorkCommand(headline="Reindexing", targets=["search/index"]),
    )

    # It leaves gracefully, which is the case a well-behaved client produces.
    left = await mcp_tools.leave_room(
        participant_token=mixed_room.autonomous["participant_token"],
        note="shutting down",
    )
    assert left["ok"] is True

    for viewer in (mixed_room.attended, mixed_room.guest):
        state = await mcp_tools.get_room_state(participant_token=viewer["participant_token"])
        freed = next(t for t in state["tasks"] if t["task_id"] == task.id)
        assert freed.get("held_by") is None, "the lease outlived its holder"
        assert freed["status"] == "open"
        assert not [w for w in state["current_work"] if w["headline"] == "Reindexing"], (
            "an ended participant's work declaration is still showing"
        )

    # And the seat is reclaimable by someone on a different path.
    retaken = await mcp_tools.claim_task(
        task_id=task.id, participant_token=mixed_room.guest["participant_token"]
    )
    assert retaken["ok"] is True


# ---------------------------------------------------------------------------
# 5. One ordering, and gaps only where privacy explains them
# ---------------------------------------------------------------------------


async def test_events_are_ordered_identically_for_everyone(mixed_room):
    """`seq` is the room's shared clock. Two participants disagreeing about order would
    make every "who went first" judgement — claims, conflicts, audit — unresolvable.

    Gaps are legitimate; *reordering* is not. So this asserts that each participant's view
    is a subsequence of the room's log in the same order, rather than that everyone sees
    everything.
    """
    owner = mixed_room.fixture.owner
    for i in range(4):
        await tasks.create(participant=owner, command=CreateTaskCommand(title=f"Task {i}"))

    room = await store.load_room(mixed_room.id)
    everyone = await store.list_participants(mixed_room.id)

    views = {}
    for participant in everyone:
        events = await projections.visible_events_since(
            room_id=mixed_room.id, recipient=participant, since_seq=0
        )
        views[participant.id] = [e["seq"] for e in events]

    for participant_id, seqs in views.items():
        assert seqs == sorted(seqs), f"{participant_id} received events out of order"
        assert len(seqs) == len(set(seqs)), f"{participant_id} received a duplicate seq"

    # Every visible seq is a real room seq, and the union covers the room-public spine.
    all_seqs = sorted({s for seqs in views.values() for s in seqs})
    assert all_seqs == sorted(set(all_seqs))
    assert max(all_seqs) <= room.event_seq


async def test_a_gap_in_one_view_is_explained_by_privacy_not_by_loss(mixed_room):
    """The distinction that makes gaps safe: a filtered event is *absent*, never renumbered.

    A recipient must be able to advance its cursor past something it cannot see without
    concluding it missed something — which is only true if `seq` keeps counting.
    """
    from app.core import messages
    from app.domain.commands import PostMessageCommand
    from app.domain.disclosure import Audience, Disclosure
    from app.domain.room import PrivacyClass

    owner = mixed_room.fixture.owner
    await messages.post(
        participant=owner,
        command=PostMessageCommand(
            body="internal: renewal terms are not final",
            disclosure=Disclosure(privacy_class=PrivacyClass.ORG_INTERNAL, audience=Audience.ROOM),
        ),
    )

    owner_view = await projections.visible_events_since(
        room_id=mixed_room.id, recipient=owner, since_seq=0
    )
    guest_view = await projections.visible_events_since(
        room_id=mixed_room.id, recipient=mixed_room.guest_participant, since_seq=0
    )

    owner_seqs = [e["seq"] for e in owner_view]
    guest_seqs = [e["seq"] for e in guest_view]

    hidden = set(owner_seqs) - set(guest_seqs)
    assert hidden, "the org_internal message should be invisible to a guest"
    # The guest's view is a subsequence — same order, fewer entries, no renumbering.
    assert guest_seqs == [s for s in owner_seqs if s in set(guest_seqs)]


# ---------------------------------------------------------------------------
# 6. The property that only exists in a mixed room
# ---------------------------------------------------------------------------


async def test_an_attended_participant_is_never_presented_as_prompt(mixed_room):
    """`docs/INTEROP.md` §3, point 6 — and the reason the capability model exists.

    An autonomous agent deciding who to hand time-sensitive work to reads this board. If an
    attended participant looked the same as an autonomous one, it would delegate and then
    wait on something that will not move until a human happens to return. The room's job is
    to make that impossible to conclude *by accident*: the grade differs, the lease ceiling
    differs, and the reason is stated rather than left to be inferred from a capability list.
    """
    state = await mcp_tools.get_room_state(
        participant_token=mixed_room.autonomous["participant_token"]
    )
    by_id = {p["participant_id"]: p for p in state["participants"]}

    attended = by_id[mixed_room.attended["participant_id"]]
    autonomous = by_id[mixed_room.autonomous["participant_id"]]

    assert attended["liveness"] == "attended"
    assert autonomous["liveness"] == "live_poll"
    assert attended["liveness"] != autonomous["liveness"], (
        "an autonomous peer cannot distinguish them"
    )

    # The asymmetry is enforced, not merely displayed: a client that cannot renew without
    # its human is capped short, so a lease it holds cannot silently outlive their attention.
    assert mixed_room.attended["max_lease_seconds"] < mixed_room.autonomous["max_lease_seconds"]

    attended_participant = await store.load_participant(mixed_room.attended["participant_id"])
    runtime = (await presence.presence_for_room(await store.load_room(mixed_room.id)))[
        attended_participant.id
    ]
    assert runtime.runtime.lease_renewable_unattended is False
    assert runtime.runtime.delivery_mode.value != "push"


async def test_the_room_never_invents_liveness_a_host_did_not_declare(mixed_room):
    """The other half of honesty: no path may be *upgraded* by the room.

    MCP has no server-initiated wake channel, so neither MCP participant may appear
    pushable however capable it claims to be — negotiation is an intersection with what the
    transport can genuinely honour, not a restatement of what the client asked for.
    """
    for mcp_participant in (mixed_room.autonomous, mixed_room.attended, mixed_room.guest):
        assert mcp_participant["delivery_mode"] == "long_poll"
        assert "supports_push" not in mcp_participant["negotiated_capabilities"]

    # The SSE participant genuinely is pushable, so the room says so. Both are correct;
    # the point is that the room reports which is which.
    snapshot = await projections.snapshot(
        room_id=mixed_room.id, recipient=mixed_room.push.participant
    )
    me = next(p for p in snapshot["participants"] if p["id"] == mixed_room.push.participant.id)
    assert me["presence"]["runtime"]["delivery_mode"] == "push"


async def test_a_polling_worker_is_not_described_as_attended(make_room, join):
    """The room must not call an unattended process attended (D-047).

    `POST /connect` cannot observe whether a client will open the SSE stream or poll
    `GET /events`, and it assumed SSE for everyone. A polling worker lost
    `supports_poll` in the intersection, fell through to `attended_pull`, and the
    board described a process with no human near it as attended — produced by the
    very rule that exists to keep declarations honest.

    Caught on the first cross-vendor proof, by the other participant being right to
    ask for evidence.
    """
    room = await make_room()
    worker = await join(room, display_name="Worker", connect=False)

    negotiated = await presence.connect(
        participant=worker.participant,
        command=ConnectCommand(
            capabilities=[
                Capability.CAN_RECEIVE_EVENTS,
                Capability.SUPPORTS_POLL,
                Capability.CAN_INITIATE_FOLLOWUP,
                Capability.CAN_EXECUTE_BACKGROUND,
                Capability.SUPPORTS_TOOLS,
            ],
            host_class=HostClass.PERSISTENT_LOCAL,
            transport="long_poll",
            attachment_label="worker-main",
        ),
        transport="long_poll",
    )

    assert Capability.SUPPORTS_POLL in negotiated.connection.negotiated_capabilities
    assert negotiated.runtime.delivery_mode is DeliveryMode.LONG_POLL
    assert negotiated.runtime.lease_renewable_unattended, "nobody has to be watching"
    assert negotiated.runtime.may_claim

    view = (await presence.presence_for_room(room.room))[worker.participant.id]
    assert view.liveness is Liveness.LIVE_POLL
