"""The coordination hierarchy, as a client actually reaches it (D-089).

Stage 1 built the domain, the storage invariants and four core services, and proved all of
them — while **no transport exposed any of it**. From a client's point of view that deploy
changed nothing. So this suite is deliberately written at the *adapter* level: it calls the
MCP tools and reads the projections the way an agent does, because `CLAUDE.md` is explicit
that a green core gate is not evidence for `adapters/`, and three bugs have already reached
production-shaped failure while unit tests passed.

Four properties it exists to hold, each of which was a real hazard rather than a
hypothetical one:

1. **An enum typo is data, not a crash.** Constructing an enum from a caller string raises
   bare `ValueError`, which is not a `RoomError` and therefore escapes the single handler
   every tool has. Before the pre-flight check, a misspelled `state` failed as a raw
   transport exception the model could not read.
2. **The coordination view stays cheap.** `compact.py` argues that a response is spent
   context, measured at ~3,400 tokens for one room read. Five unconditional new sections
   would add a fixed cost to every poll in every room, including the majority with no jobs
   at all — so every section is gated on carrying information, and that is asserted rather
   than assumed.
3. **Privacy filtering survives the new path.** Jobs and goal versions carry a privacy
   class, and the projection is the first thing that ever read those tables. A filter that
   is right in three places and forgotten in a fourth is the usual way this leaks.
4. **A declared state is never presented as liveness.** A worker record is its supervisor's
   claim. The compact reducer names the field `declared_state` for that reason, and capacity
   clamps to `offline` from presence over the top of whatever the seat said.
"""

from __future__ import annotations

import pytest

from app.adapters.mcp import compact
from app.adapters.mcp import server as mcp_tools
from app.core import goals, jobs, presence, projections, roles, rooms, store, workers
from app.domain.commands import (
    ConnectCommand,
    JoinRoomCommand,
    PostJobCommand,
    RegisterWorkerCommand,
)
from app.domain.disclosure import Audience, Disclosure
from app.domain.job import JobState
from app.domain.room import ParticipantRole, PrivacyClass, RoomRole, RoomVisibility

pytestmark = pytest.mark.asyncio


async def _rejoinable_seat(fixture, *, display_name: str, role=ParticipantRole.COLLABORATOR):
    """A seat whose identity is reusable, so a second join is a rejoin and not a new seat.

    The `join` fixture mints a fresh identity per call, which makes a second call a *new
    participant*. Tests that need the same seat twice have to hold the identity themselves.
    """
    from app.domain.commands import CreateInvitationCommand

    issued = await rooms.create_invitation(
        participant=fixture.owner, command=CreateInvitationCommand(role=role)
    )
    identity = await rooms.create_identity(
        org_id=fixture.org_id,
        owner_user_id=fixture.owner_user_id,
        display_name=display_name,
    )
    result = await rooms.join_room(
        identity=identity,
        command=JoinRoomCommand(invitation_token=issued.token, display_name=display_name),
    )
    await presence.connect(
        participant=result.participant, command=ConnectCommand(), transport="sse"
    )
    return result.participant, result.participant_token, identity


# ---------------------------------------------------------------------------
# The projection is reachable at all
# ---------------------------------------------------------------------------


async def test_the_creator_reads_as_the_orchestrator_on_its_own_participant_card(make_room):
    """The position has to be visible where a reader is already looking.

    Stage 1 assigned the creator ORCHESTRATOR inside the creation transaction. Nothing
    surfaced it, so from any client's side the room looked exactly as it did before.
    """
    fixture = await make_room()
    snapshot = await projections.snapshot(room_id=fixture.room.id, recipient=fixture.owner)

    card = next(p for p in snapshot["participants"] if p["id"] == fixture.owner.id)
    assert card["room_role"] == "orchestrator"
    # Two axes, and the names keep them apart. `role` is authority; `room_role` is position.
    assert card["role"] == "owner"

    view = compact.room_state(snapshot)
    compact_card = next(p for p in view["participants"] if p["participant_id"] == fixture.owner.id)
    assert compact_card["room_role"] == "orchestrator"


async def test_a_joiner_reads_as_a_supervisor_and_an_observer_as_an_observer(make_room, join):
    fixture = await make_room()
    supervisor = await join(fixture, display_name="Bea")
    observer = await join(fixture, display_name="Cass", role=ParticipantRole.OBSERVER)

    snapshot = await projections.snapshot(room_id=fixture.room.id, recipient=fixture.owner)
    by_id = {p["id"]: p for p in snapshot["participants"]}
    assert by_id[supervisor.participant.id]["room_role"] == "supervisor"
    assert by_id[observer.participant.id]["room_role"] == "observer"


async def test_a_seat_that_left_has_no_room_role_rather_than_a_stale_one(make_room, join):
    """`room_roles` answers only for members, and a position for a non-member is a claim
    about nobody. The hazard is the other shape: a naive `roles_map[p.id]` lookup raises
    KeyError, because the snapshot's participant list is unfiltered by state."""
    from app.domain.commands import LeaveRoomCommand

    fixture = await make_room()
    leaver = await join(fixture, display_name="Dov")
    await rooms.leave_room(participant=leaver.participant, command=LeaveRoomCommand())

    snapshot = await projections.snapshot(room_id=fixture.room.id, recipient=fixture.owner)
    card = next(p for p in snapshot["participants"] if p["id"] == leaver.participant.id)
    assert card["state"] == "left"
    assert card["room_role"] is None


# ---------------------------------------------------------------------------
# The job board, end to end through the tools
# ---------------------------------------------------------------------------


async def test_a_posted_job_reaches_the_board_and_reads_as_unallocated(make_room):
    fixture = await make_room()

    posted = await mcp_tools.post_job(
        title="Ship the reconnect fix",
        human_instruction="just get the reconnect thing working, I don't care how",
        targets=["backend/app/core/presence.py"],
        acceptance_criteria=["a live reconnect is observed, not just tested"],
        participant_token=fixture.owner_token,
    )
    assert posted["ok"] is True
    assert posted["state"] == "posted"

    state = await mcp_tools.get_room_state(participant_token=fixture.owner_token)
    board = state["jobs"]
    assert len(board) == 1
    entry = board[0]
    assert entry["title"] == "Ship the reconnect fix"
    # Said positively rather than left to be inferred from a missing key: an unallocated job
    # is the one thing on this board an orchestrator must act on.
    assert entry["unallocated"] is True
    # The person's own words, kept because a paraphrase cannot be un-paraphrased.
    assert entry["human_instruction"] == "just get the reconnect thing working, I don't care how"
    assert entry["targets"] == ["backend/app/core/presence.py"]


async def test_allocating_a_job_makes_the_owner_and_the_missing_acceptance_visible(make_room, join):
    fixture = await make_room()
    supervisor = await join(fixture, display_name="Bea")
    posted = await mcp_tools.post_job(
        title="Audit the adapters", participant_token=fixture.owner_token
    )

    assigned = await mcp_tools.assign_job(
        job_id=posted["job_id"],
        to_participant_id=supervisor.participant.id,
        reason="Bea owns the adapter surface this week",
        participant_token=fixture.owner_token,
    )
    assert assigned["ok"] is True

    state = await mcp_tools.get_room_state(participant_token=fixture.owner_token)
    entry = state["jobs"][0]
    assert entry["owner"] == supervisor.participant.id
    # Assigned but not accepted is the state an orchestrator most needs to see.
    assert entry["accepted"] is False
    assert "unallocated" not in entry

    await mcp_tools.accept_job(job_id=posted["job_id"], participant_token=supervisor.token)
    state = await mcp_tools.get_room_state(participant_token=fixture.owner_token)
    assert "accepted" not in state["jobs"][0]


async def test_accepting_twice_is_safe_and_says_so_without_a_seq(make_room, join):
    """A retry after a lost response must not append a second acceptance. The response
    deliberately omits `seq`, which is why the tool docstring warns callers not to expect it."""
    fixture = await make_room()
    supervisor = await join(fixture, display_name="Bea")
    posted = await mcp_tools.post_job(title="Something", participant_token=fixture.owner_token)
    await mcp_tools.assign_job(
        job_id=posted["job_id"],
        to_participant_id=supervisor.participant.id,
        reason="because",
        participant_token=fixture.owner_token,
    )

    first = await mcp_tools.accept_job(job_id=posted["job_id"], participant_token=supervisor.token)
    second = await mcp_tools.accept_job(job_id=posted["job_id"], participant_token=supervisor.token)
    assert first["ok"] is True
    assert second["already_accepted"] is True
    assert "seq" not in second


async def test_a_closed_job_leaves_the_coordination_view_but_stays_on_the_record(make_room):
    """Two different questions. The board is the durable record of what people asked for and
    what became of it; the coordination view is what needs doing now."""
    fixture = await make_room()
    posted = await mcp_tools.post_job(title="Old idea", participant_token=fixture.owner_token)
    await mcp_tools.close_job(
        job_id=posted["job_id"],
        state="cancelled",
        reason="superseded by the new plan in the charter",
        participant_token=fixture.owner_token,
    )

    view = await mcp_tools.get_room_state(participant_token=fixture.owner_token)
    assert "jobs" not in view

    full = await mcp_tools.get_room_state(detail="full", participant_token=fixture.owner_token)
    assert [j["state"] for j in full["jobs"]] == ["cancelled"]

    board = await mcp_tools.get_room_state(
        detail="hierarchy", participant_token=fixture.owner_token
    )
    assert [j["state"] for j in board["board"]] == ["cancelled"]
    assert board["board"][0]["closed_because"].startswith("superseded by")


async def test_the_untruncated_total_comes_from_the_database_not_the_page(make_room):
    """A truncated list presented without its count reads as a complete one, and a count
    derived from the page is an exact-looking wrong number (D-043)."""
    fixture = await make_room()
    for n in range(4):
        await mcp_tools.post_job(title=f"Job {n}", participant_token=fixture.owner_token)

    page, total = await jobs.board_for_room(fixture.room.id, limit=2)
    assert len(page) == 2
    assert total == 4


# ---------------------------------------------------------------------------
# Enum arguments: a typo must be readable, not fatal
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "call,field",
    [
        (
            lambda tok: mcp_tools.set_job_state(
                job_id="job_x", state="finished", participant_token=tok
            ),
            "state",
        ),
        (
            lambda tok: mcp_tools.close_job(
                job_id="job_x", state="done", reason="r", participant_token=tok
            ),
            "state",
        ),
        (
            lambda tok: mcp_tools.assign_room_role(
                target_participant_id="p", room_role="boss", reason="r", participant_token=tok
            ),
            "room_role",
        ),
        (lambda tok: mcp_tools.report_capacity(declared="busy", participant_token=tok), "declared"),
        (
            lambda tok: mcp_tools.update_worker(
                worker_id="w", state="thinking", participant_token=tok
            ),
            "state",
        ),
        (
            lambda tok: mcp_tools.close_goal(
                goal_id="g", status="finished", reason="r", participant_token=tok
            ),
            "status",
        ),
        (
            lambda tok: mcp_tools.register_worker(
                label="w", provenance="borrowed", participant_token=tok
            ),
            "provenance",
        ),
        (
            lambda tok: mcp_tools.replace_supervisor_goal(
                target_supervisor_participant_id="p",
                objective="o",
                worker_disposition="abandon",
                participant_token=tok,
            ),
            "worker_disposition",
        ),
        (lambda tok: mcp_tools.post_job(title="t", origin="whim", participant_token=tok), "origin"),
    ],
)
async def test_an_unknown_enum_value_is_answered_as_data_not_raised(make_room, call, field):
    """`ValueError` is not a `RoomError`, so it escapes every tool's only handler and the
    call fails as a raw transport exception the model cannot read or correct."""
    fixture = await make_room()
    result = await call(fixture.owner_token)
    assert result["ok"] is False
    assert result["error"] == "invalid_command"
    assert result["message"].startswith(f"{field} must be one of ")


# ---------------------------------------------------------------------------
# Supervisor goals through the tools
# ---------------------------------------------------------------------------


async def test_a_goal_reaches_the_supervisor_resume_payload_with_its_contract(make_room, join):
    fixture = await make_room()
    supervisor = await join(fixture, display_name="Bea")

    replaced = await mcp_tools.replace_supervisor_goal(
        target_supervisor_participant_id=supervisor.participant.id,
        objective="Bring the adapter surface up to parity with core",
        instructions="Start with the job board; the goal path can follow",
        acceptance_criteria=["every core service reachable from MCP and ARP HTTP"],
        participant_token=fixture.owner_token,
    )
    assert replaced["ok"] is True
    assert replaced["version"] == 1

    resume = await mcp_tools.get_room_state(detail="resume", participant_token=supervisor.token)
    assert resume["your_room_role"] == "supervisor"
    assert resume["your_goal"]["current"]["objective"].startswith("Bring the adapter surface")
    # The obligations no objective may rewrite, carried beside the goal rather than left in a
    # document a runtime never opens.
    assert len(resume["runtime_contract"]) == len(goals.immutable_contract())
    assert resume["runtime_contract"]


async def test_omitting_the_fence_against_an_existing_goal_is_refused_not_treated_as_latest(
    make_room, join
):
    """`expected_version=None` means "there is no goal yet". Reading it as "latest" is how a
    stale coordinating turn silently undoes a newer decision, so the tool docstring promises
    this refusal and the promise is asserted here."""
    fixture = await make_room()
    supervisor = await join(fixture, display_name="Bea")
    await mcp_tools.replace_supervisor_goal(
        target_supervisor_participant_id=supervisor.participant.id,
        objective="First direction",
        participant_token=fixture.owner_token,
    )

    blind = await mcp_tools.replace_supervisor_goal(
        target_supervisor_participant_id=supervisor.participant.id,
        objective="Second direction",
        participant_token=fixture.owner_token,
    )
    assert blind["ok"] is False
    assert blind["error"] == "revision_conflict"

    fenced = await mcp_tools.replace_supervisor_goal(
        target_supervisor_participant_id=supervisor.participant.id,
        objective="Second direction",
        expected_version=1,
        participant_token=fixture.owner_token,
    )
    assert fenced["ok"] is True
    assert fenced["version"] == 2


async def test_an_unacknowledged_goal_says_so_and_a_rejection_says_that_too(make_room, join):
    """A quietly acknowledged goal is the normal case and needs no field. The two exceptions
    each change what a reader should expect of that supervisor."""
    fixture = await make_room()
    supervisor = await join(fixture, display_name="Bea")
    replaced = await mcp_tools.replace_supervisor_goal(
        target_supervisor_participant_id=supervisor.participant.id,
        objective="Do the thing",
        participant_token=fixture.owner_token,
    )

    view = await mcp_tools.get_room_state(participant_token=fixture.owner_token)
    assert view["goals"][0]["acknowledged"] is False

    await mcp_tools.acknowledge_goal(
        goal_id=replaced["goal_id"],
        version=1,
        note="my human wants the other thing first",
        rejected=True,
        participant_token=supervisor.token,
    )
    view = await mcp_tools.get_room_state(participant_token=fixture.owner_token)
    entry = view["goals"][0]
    assert "acknowledged" not in entry
    assert entry["acknowledged_but_rejected"] is True
    assert entry["rejection_note"] == "my human wants the other thing first"


async def test_goal_history_is_readable_and_names_the_untruncated_total(make_room, join):
    fixture = await make_room()
    supervisor = await join(fixture, display_name="Bea")
    first = await mcp_tools.replace_supervisor_goal(
        target_supervisor_participant_id=supervisor.participant.id,
        objective="v1 objective",
        participant_token=fixture.owner_token,
    )
    await mcp_tools.replace_supervisor_goal(
        target_supervisor_participant_id=supervisor.participant.id,
        objective="v2 objective",
        expected_version=1,
        participant_token=fixture.owner_token,
    )

    history = await mcp_tools.read_goal_history(
        goal_id=first["goal_id"], participant_token=supervisor.token
    )
    assert history["total"] == 2
    assert [v["objective"] for v in history["versions"]] == ["v2 objective", "v1 objective"]
    # Append-only: the superseded version still says what it said, and now also says what
    # replaced it.
    assert history["versions"][1]["superseded_by_version"] == 2


# ---------------------------------------------------------------------------
# Capacity and workers: declarations, never liveness
# ---------------------------------------------------------------------------


async def test_capacity_appears_only_once_it_carries_information(make_room, join):
    """A room where nobody declared anything would otherwise get one identical
    `available / 1 slot / 0 workers` card per seat: a fixed cost that says nothing, in a view
    whose whole argument is that a response is spent context."""
    fixture = await make_room()
    # Declared from the joined seat rather than the creator, because the creator holds no
    # connection in these fixtures and therefore reads `effective: offline` whatever it says.
    # That is the clamp working, and it would mask what this test is about.
    supervisor = await join(fixture, display_name="Bea")

    view = await mcp_tools.get_room_state(participant_token=fixture.owner_token)
    assert "capacity" not in view

    # A default declaration is still nothing to say. The gate is on what a seat *told* the
    # room, not on `effective` — a disconnected seat reads `offline` there, and putting a
    # capacity card beside every disconnected seat would repeat what its `liveness` already
    # says one section earlier.
    await mcp_tools.report_capacity(declared="available", participant_token=supervisor.token)
    view = await mcp_tools.get_room_state(participant_token=fixture.owner_token)
    assert "capacity" not in view

    await mcp_tools.report_capacity(
        declared="blocked",
        note="waiting on the Stripe webhook secret",
        participant_token=supervisor.token,
    )
    view = await mcp_tools.get_room_state(participant_token=fixture.owner_token)
    assert len(view["capacity"]) == 1
    assert view["capacity"][0]["supervisor"] == supervisor.participant.id
    assert view["capacity"][0]["effective"] == "blocked"
    assert view["capacity"][0]["note"] == "waiting on the Stripe webhook secret"


async def test_declaring_offline_is_refused_because_it_is_derived(make_room):
    fixture = await make_room()
    result = await mcp_tools.report_capacity(
        declared="offline", participant_token=fixture.owner_token
    )
    assert result["ok"] is False
    assert result["error"] == "invalid_command"
    assert "derived from your connections" in result["message"]


async def test_a_stale_seat_reads_offline_however_available_it_declared_itself(make_room, join):
    """The clamp, exercised through the projection rather than through the service, because
    the projection loads capacity before it knows presence and asks the rule a second time.
    A copy of that rule in the projection is what this would otherwise be."""
    fixture = await make_room()
    quiet = await join(fixture, display_name="Bea")
    await mcp_tools.report_capacity(declared="available", participant_token=quiet.token)

    await presence.disconnect(connection_id=quiet.connection_id, participant=quiet.participant)

    snapshot = await projections.snapshot(room_id=fixture.room.id, recipient=fixture.owner)
    row = next(
        c for c in snapshot["capacity"] if c["supervisor_participant_id"] == quiet.participant.id
    )
    assert row["declared"] == "available"
    assert row["effective"] == "offline"

    rendered = compact.capacity(row)
    # Both, because they disagree exactly when it matters.
    assert rendered["effective"] == "offline"
    assert rendered["declared"] == "available"


async def test_a_worker_is_rendered_as_a_declaration_and_never_as_presence(make_room):
    fixture = await make_room()
    registered = await mcp_tools.register_worker(
        label="codex-1",
        assignment="Port the job board reducers",
        participant_token=fixture.owner_token,
    )
    assert registered["ok"] is True

    view = await mcp_tools.get_room_state(participant_token=fixture.owner_token)
    entry = view["workers"][0]
    # Named `declared_state`, not `state`, so a reader skimming for a status cannot mistake
    # it for one the room verified.
    assert entry["declared_state"] == "starting"
    assert "state" not in entry
    assert "liveness" not in entry
    assert entry["supervisor"] == fixture.owner.id


async def test_re_registering_a_label_updates_the_same_worker(make_room):
    """A restarted supervisor re-declaring its pool must not double the room's idea of its
    capacity — the rule an attachment label already follows."""
    fixture = await make_room()
    first = await mcp_tools.register_worker(label="codex-1", participant_token=fixture.owner_token)
    again = await mcp_tools.register_worker(
        label="codex-1", assignment="a new brief", participant_token=fixture.owner_token
    )
    assert again["worker_id"] == first["worker_id"]
    assert again["redeclared"] is True

    pool = await workers.workers_for(fixture.room.id)
    assert len(pool) == 1
    assert pool[0].assignment == "a new brief"


async def test_a_finished_worker_does_not_complete_its_job(make_room):
    fixture = await make_room()
    posted = await mcp_tools.post_job(
        title="Port the reducers", participant_token=fixture.owner_token
    )
    registered = await mcp_tools.register_worker(
        label="codex-1", related_job_id=posted["job_id"], participant_token=fixture.owner_token
    )

    finished = await mcp_tools.finish_worker(
        worker_id=registered["worker_id"],
        state="completed",
        summary="reducers ported, tests green",
        result_reference="ckp_abc",
        participant_token=fixture.owner_token,
    )
    assert finished["awaiting_supervisor_review"] is True

    job = await jobs.get(fixture.room.id, posted["job_id"])
    assert job.state is JobState.POSTED
    assert job.closed_at is None


async def test_a_worker_record_refuses_a_class_it_cannot_store(make_room):
    """`workers` has no `privacy_class` column, deliberately — a worker record is
    room-visible coordination state. But the disclosure decision is stamped on the *event*
    while the projection reads the *row*, so accepting `participant_private` would file a
    filtered event beside a room-visible row and disclose exactly what was held back.
    Refused rather than downgraded: a downgrade performs the disclosure it prevents."""
    from app.core.errors import InvalidCommand

    fixture = await make_room()
    with pytest.raises(InvalidCommand) as caught:
        await workers.register(
            participant=fixture.owner,
            command=RegisterWorkerCommand(
                label="secret-1",
                assignment="something sensitive",
                disclosure=Disclosure(
                    privacy_class=PrivacyClass.PARTICIPANT_PRIVATE,
                    audience=Audience.PARTICIPANT,
                    to_participant_id=fixture.owner.id,
                ),
            ),
        )
    assert "nowhere to store one" in str(caught.value)


# ---------------------------------------------------------------------------
# The coordination view stays cheap; the hierarchy view is where the detail lives
# ---------------------------------------------------------------------------


async def test_a_room_with_no_hierarchy_activity_pays_nothing_for_it(make_room, join):
    """The context-cost claim, asserted. Five unconditional sections would add a fixed cost
    to every poll in every room, and most rooms have no jobs, goals or workers at all."""
    fixture = await make_room()
    await join(fixture, display_name="Bea")

    view = await mcp_tools.get_room_state(participant_token=fixture.owner_token)
    for section in ("jobs", "goals", "workers", "capacity", "jobs_total", "closed_jobs"):
        assert section not in view, f"{section} should be absent until it carries information"


async def test_the_hierarchy_view_puts_each_seat_beside_its_goal_and_its_pool(make_room, join):
    """Four parallel lists the reader joins by id itself is how an orchestrator ends up
    allocating against a supervisor whose goal it did not notice."""
    fixture = await make_room()
    supervisor = await join(fixture, display_name="Bea")
    await mcp_tools.replace_supervisor_goal(
        target_supervisor_participant_id=supervisor.participant.id,
        objective="Own the adapter surface",
        participant_token=fixture.owner_token,
    )
    await mcp_tools.register_worker(label="bea-worker", participant_token=supervisor.token)
    await mcp_tools.report_capacity(
        declared="partially_allocated", max_concurrent_workers=3, participant_token=supervisor.token
    )

    view = await mcp_tools.get_room_state(detail="hierarchy", participant_token=fixture.owner_token)
    assert view["orchestrator"] == fixture.owner.id
    assert "note" not in view

    card = next(c for c in view["hierarchy"] if c["participant_id"] == supervisor.participant.id)
    assert card["room_role"] == "supervisor"
    assert card["goal"]["objective"] == "Own the adapter surface"
    assert card["goal"]["version"] == 1
    assert [w["label"] for w in card["workers"]] == ["bea-worker"]
    assert card["capacity"]["free_slots"] == 2
    assert card["capacity"]["active_workers"] == 1


async def test_a_room_with_no_orchestrator_says_so_rather_than_looking_broken(make_room):
    """Not automatic failover, by design. A reader must be able to tell "nobody coordinates
    here" apart from "I failed to parse the card"."""
    fixture = await make_room()
    await roles.assign(
        participant=fixture.owner,
        command=__import__(
            "app.domain.commands", fromlist=["AssignRoomRoleCommand"]
        ).AssignRoomRoleCommand(
            target_participant_id=fixture.owner.id,
            room_role=RoomRole.UNASSIGNED,
            reason="standing myself down to test the empty-chair state",
        ),
    )

    view = await mcp_tools.get_room_state(detail="hierarchy", participant_token=fixture.owner_token)
    assert view["orchestrator"] is None
    assert "no orchestrator" in view["note"]
    assert "assign_room_role" in view["note"]


async def test_taking_an_empty_chair_is_possible_so_orchestrator_loss_is_recoverable(make_room):
    """Requiring the orchestrator gate to appoint an orchestrator would make losing one
    unrecoverable without operator surgery. `room.admin` still applies, and a reason is
    still required."""
    from app.domain.commands import AssignRoomRoleCommand

    fixture = await make_room()
    await roles.assign(
        participant=fixture.owner,
        command=AssignRoomRoleCommand(
            target_participant_id=fixture.owner.id,
            room_role=RoomRole.UNASSIGNED,
            reason="stand down",
        ),
    )
    assert await roles.orchestrator_of(fixture.room.id) is None

    retaken = await mcp_tools.assign_room_role(
        target_participant_id=fixture.owner.id,
        room_role="orchestrator",
        reason="nobody is coordinating and I hold room.admin",
        participant_token=fixture.owner_token,
    )
    assert retaken["ok"] is True
    assert await roles.orchestrator_of(fixture.room.id) == fixture.owner.id


# ---------------------------------------------------------------------------
# Privacy, through the path that reads these tables for the first time
# ---------------------------------------------------------------------------


async def test_a_participant_private_job_does_not_reach_another_seat(make_room, join):
    fixture = await make_room()
    other = await join(fixture, display_name="Bea")

    await jobs.post(
        participant=fixture.owner,
        command=PostJobCommand(
            title="Rotate the operator token",
            human_instruction="quietly-rotate-the-thing",
            disclosure=Disclosure(
                privacy_class=PrivacyClass.PARTICIPANT_PRIVATE,
                audience=Audience.PARTICIPANT,
                to_participant_id=fixture.owner.id,
            ),
        ),
    )

    theirs = await projections.snapshot(room_id=fixture.room.id, recipient=other.participant)
    assert theirs["jobs"] == []
    assert "quietly-rotate-the-thing" not in str(compact.room_state(theirs))

    mine = await projections.snapshot(room_id=fixture.room.id, recipient=fixture.owner)
    assert [j["title"] for j in mine["jobs"]] == ["Rotate the operator token"]


async def test_a_private_goal_reaches_its_supervisor_and_its_author_and_nobody_else(
    make_room, join
):
    """Two seats have a standing claim on a private goal — the supervisor it directs and the
    orchestrator that wrote it — and `owner_participant_id` names only one of them."""
    from app.domain.commands import ReplaceGoalCommand

    fixture = await make_room()
    supervisor = await join(fixture, display_name="Bea")
    bystander = await join(fixture, display_name="Cass")

    await goals.replace(
        participant=fixture.owner,
        command=ReplaceGoalCommand(
            target_supervisor_participant_id=supervisor.participant.id,
            objective="handle-the-delicate-thing",
            disclosure=Disclosure(
                privacy_class=PrivacyClass.PARTICIPANT_PRIVATE,
                audience=Audience.PARTICIPANT,
                to_participant_id=supervisor.participant.id,
            ),
        ),
    )

    theirs = await projections.snapshot(room_id=fixture.room.id, recipient=bystander.participant)
    assert theirs["goals"] == []
    assert "handle-the-delicate-thing" not in str(compact.room_state(theirs))

    directed = await projections.snapshot(room_id=fixture.room.id, recipient=supervisor.participant)
    assert directed["goals"][0]["current"]["objective"] == "handle-the-delicate-thing"

    author = await projections.snapshot(room_id=fixture.room.id, recipient=fixture.owner)
    assert author["goals"][0]["current"]["objective"] == "handle-the-delicate-thing"


async def test_a_cross_org_room_still_refuses_org_internal_on_the_new_path(make_room, join):
    """The rejection is a hard error rather than a downgrade, because a downgrade performs
    the disclosure it was meant to prevent."""
    from app.core.errors import PrivacyViolation

    fixture = await make_room(visibility=RoomVisibility.CROSS_ORG)

    with pytest.raises(PrivacyViolation):
        await jobs.post(
            participant=fixture.owner,
            command=PostJobCommand(
                title="Internal roadmap work",
                disclosure=Disclosure(privacy_class=PrivacyClass.ORG_INTERNAL),
            ),
        )


# ---------------------------------------------------------------------------
# Event compaction
# ---------------------------------------------------------------------------


async def test_a_job_posted_event_is_compacted_rather_than_relayed_whole(make_room):
    """Without an `_EVENT_FIELDS` row an unknown type falls through to `kept = payload`, so
    the full envelope lands in every poll. That failure is silent — the view becomes verbose
    rather than breaking — which is why it is asserted."""
    fixture = await make_room()
    await mcp_tools.post_job(
        title="Ship it",
        human_instruction="ship the thing today please",
        room_goal_relationship="this is the whole point of the room",
        participant_token=fixture.owner_token,
    )

    page = await mcp_tools.await_room_events(
        since_seq=0, timeout_seconds=0, participant_token=fixture.owner_token
    )
    posted = next(e for e in page["events"] if e["type"] == "job.posted")
    assert posted["title"] == "Ship it"
    # Kept: the one field on the board a reader cannot reconstruct from anything else.
    assert posted["human_instruction"] == "ship the thing today please"
    # Dropped: real payload content that no coordinating decision turns on.
    assert "room_goal_relationship" not in posted


async def test_a_goal_replacement_carries_its_objective_and_its_version_pair(make_room, join):
    fixture = await make_room()
    supervisor = await join(fixture, display_name="Bea")
    await mcp_tools.replace_supervisor_goal(
        target_supervisor_participant_id=supervisor.participant.id,
        objective="Own the adapter surface",
        instructions="a long paragraph nobody needs on the wire",
        participant_token=fixture.owner_token,
    )

    page = await mcp_tools.await_room_events(
        since_seq=0, timeout_seconds=0, participant_token=supervisor.token
    )
    replaced = next(e for e in page["events"] if e["type"] == "supervisor.goal_replaced")
    assert replaced["objective"] == "Own the adapter surface"
    assert replaced["new_version"] == 1
    assert replaced["worker_disposition"] == "stop"
    assert "instructions" not in replaced


# ---------------------------------------------------------------------------
# The HTTP transport reaches the same services
# ---------------------------------------------------------------------------


async def test_the_verb_routes_are_not_swallowed_by_the_job_id_path(make_room):
    """`GET /jobs/{job_id}` registered before `POST /jobs/assign` would swallow every verb.
    The trap is live in this file already for tasks, and it fails silently."""
    from app.main import build_app

    app = build_app()
    registered = {
        (route.path, method)
        for route in app.routes
        for method in getattr(route, "methods", set()) or set()
    }
    for verb in ("update", "assign", "accept", "state", "close"):
        assert ("/api/rooms/{room_id}/jobs/" + verb, "POST") in registered
    assert ("/api/rooms/{room_id}/jobs/{job_id}", "GET") in registered

    paths = [getattr(r, "path", "") for r in app.routes]
    assert paths.index("/api/rooms/{room_id}/jobs/assign") < paths.index(
        "/api/rooms/{room_id}/jobs/{job_id}"
    )


async def test_every_hierarchy_service_is_reachable_over_arp_http(make_room):
    """The MCP adapter and the ARP HTTP surface must not diverge: a companion runs on HTTP,
    and an agent runs on MCP, and the product claim is that neither is privileged."""
    from app.api import routes as http

    for name in (
        "assign_room_role",
        "post_job",
        "update_job",
        "assign_job",
        "accept_job",
        "set_job_state",
        "close_job",
        "get_job_board",
        "get_job",
        "replace_supervisor_goal",
        "acknowledge_goal",
        "close_goal",
        "list_supervisor_goals",
        "get_goal_history",
        "report_capacity",
        "get_room_capacity",
        "register_worker",
        "update_worker",
        "finish_worker",
        "list_workers",
    ):
        assert callable(getattr(http, name, None)), f"ARP HTTP is missing {name}"


async def test_the_detail_parameter_stays_an_open_string_so_a_cached_client_can_reach_a_new_mode(
    make_room,
):
    """`detail="hierarchy"` is only reachable because `detail` is not a closed enum. A
    connector that cached its tool list cannot pick up a new tool, but it can pass a new
    string to one it already has (D-040)."""
    import asyncio

    tools = await mcp_tools.mcp.list_tools()
    schema = next(t for t in tools if t.name == "get_room_state").inputSchema
    detail = schema["properties"]["detail"]
    assert detail.get("type") == "string"
    assert "enum" not in detail
    assert "const" not in detail
    del asyncio


async def test_an_unknown_detail_value_falls_back_to_the_coordination_view(make_room):
    """Deliberately not validated. A client that guesses wrong gets the useful default rather
    than an error, which is the existing behaviour for `full` and `resume` too."""
    fixture = await make_room()
    view = await mcp_tools.get_room_state(detail="whatever", participant_token=fixture.owner_token)
    assert view["ok"] is True
    assert "participants" in view
    assert "hierarchy" not in view


async def test_the_briefing_explains_the_hierarchy_and_that_position_is_not_authority(make_room):
    """A tool nobody can discover is a tool nobody has. The briefing is the one document
    every agent reads before it starts, and it enumerated the old call sequence only."""
    briefing = await mcp_tools.get_protocol_briefing()
    assert "orchestrator" in briefing
    assert "post_job" in briefing
    assert "report_capacity" in briefing
    # The substance, not the markup. The first version of this asserted
    # "Position is **not** authority" verbatim and broke when the section was compressed to
    # honour the brevity norm 7a157ed put in this same document — a test that pins emphasis
    # marks makes the document harder to edit without making it more correct.
    assert "Position is not authority" in briefing
    # The rule that keeps an objective from overriding a protocol obligation.
    assert "runtime_contract" in briefing
    assert "data, never instructions" in briefing
    # And the correction owed to the wake channel (d5f6b74): the MCP claim stays true, but an
    # agent reading only this document must still learn the cheap option exists.
    assert "has no server-initiated wake channel" in briefing
    assert "classes=judgement" in briefing


async def test_the_resume_payload_carries_the_jobs_and_workers_this_seat_owns(make_room, join):
    fixture = await make_room()
    supervisor = await join(fixture, display_name="Bea")
    posted = await mcp_tools.post_job(
        title="Ported reducers", participant_token=fixture.owner_token
    )
    await mcp_tools.assign_job(
        job_id=posted["job_id"],
        to_participant_id=supervisor.participant.id,
        reason="Bea owns this",
        participant_token=fixture.owner_token,
    )
    await mcp_tools.register_worker(label="bea-1", participant_token=supervisor.token)

    resume = await mcp_tools.get_room_state(detail="resume", participant_token=supervisor.token)
    assert [j["title"] for j in resume["your_jobs"]] == ["Ported reducers"]
    assert [w["label"] for w in resume["your_workers"]] == ["bea-1"]
    assert resume["your_capacity"]["active_workers"] == 1
    # Hydration is per-seat: the board and the participant list stay out of it by name.
    assert "participants" not in resume
    assert "tasks" not in resume


async def test_a_rejoining_seat_keeps_the_position_it_was_given(make_room):
    """Rejoining must not silently demote a promoted seat. `join` preserves an existing role,
    and this is the path that proves it through a real second redemption."""
    from app.domain.commands import AssignRoomRoleCommand

    fixture = await make_room()
    participant, token, identity = await _rejoinable_seat(fixture, display_name="Bea")
    await roles.assign(
        participant=fixture.owner,
        command=AssignRoomRoleCommand(
            target_participant_id=participant.id,
            room_role=RoomRole.OBSERVER,
            reason="Bea is only watching this week",
        ),
    )

    from app.domain.commands import CreateInvitationCommand

    issued = await rooms.create_invitation(
        participant=fixture.owner, command=CreateInvitationCommand()
    )
    again = await rooms.join_room(
        identity=identity,
        command=JoinRoomCommand(invitation_token=issued.token, display_name="Bea"),
    )
    assert again.participant.id == participant.id

    reloaded = await store.load_participant(participant.id)
    assert await roles.role_for(reloaded) is RoomRole.OBSERVER
    del token
