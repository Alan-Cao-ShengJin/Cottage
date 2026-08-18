"""Versioned supervisor goals, and the fence that makes replacement safe (D-088).

The orchestrator may replace a supervisor's goal *completely*, which is a deliberately
powerful act. These tests are about the three things that stop it being an unaccountable
one: a fence nobody can skip, history nothing can erase, and a contract no goal can rewrite.
"""

from __future__ import annotations

import asyncio

import pytest

from app.core import goals, roles, rooms, store
from app.core.errors import Forbidden, InvalidCommand, NotFound, RevisionConflict
from app.domain.capabilities import Capability, HostClass
from app.domain.commands import (
    AcknowledgeGoalCommand,
    CloseGoalCommand,
    CreateInvitationCommand,
    JoinRoomCommand,
    ReplaceGoalCommand,
)
from app.domain.goal import GoalSource, GoalStatus, WorkerDisposition
from app.domain.identity import PrincipalKind
from app.domain.room import ParticipantRole, RoomRole

pytestmark = pytest.mark.asyncio


async def _rejoinable_seat(room, *, display_name: str) -> tuple[str, str]:
    """Redeem a fresh invitation as the same logical agent; return (participant_id, token)."""
    issued = await rooms.create_invitation(
        participant=room.owner, command=CreateInvitationCommand()
    )
    identity = await rooms.ensure_identity(
        org_id=room.org_id,
        owner_user_id=room.owner_user_id,
        display_name=display_name,
        kind=PrincipalKind.AGENT,
        host_class=HostClass.PERSISTENT_LOCAL,
        capabilities=[Capability.CAN_RECEIVE_EVENTS, Capability.SUPPORTS_POLL],
    )
    result = await rooms.join_room(
        identity=identity,
        command=JoinRoomCommand(
            invitation_token=issued.token,
            display_name=display_name,
            host_class=HostClass.PERSISTENT_LOCAL,
            capabilities=[Capability.CAN_RECEIVE_EVENTS, Capability.SUPPORTS_POLL],
        ),
    )
    return result.participant.id, result.participant_token


async def _goal(target_id: str, **kwargs) -> ReplaceGoalCommand:
    payload = {
        "target_supervisor_participant_id": target_id,
        "objective": "Ship the reconnect fix",
        "instructions": "Start with the failing lifecycle test.",
        "reason": "it is the room's P0",
    }
    payload.update(kwargs)
    return ReplaceGoalCommand(**payload)


async def test_the_creator_orchestrates_and_a_joiner_supervises(make_room, join):
    """The hierarchy is assigned at the door, not configured afterwards."""
    room = await make_room()
    codex = await join(room, display_name="Codex")

    assert await roles.role_for(room.owner) is RoomRole.ORCHESTRATOR
    assert await roles.role_for(codex.participant) is RoomRole.SUPERVISOR
    assert await roles.orchestrator_of(room.room.id) == room.owner.id


async def test_an_orchestrator_replaces_a_supervisors_whole_goal(make_room, join):
    """Replacement, not append: the previous version is superseded in the same act."""
    room = await make_room()
    codex = await join(room, display_name="Codex")

    first = await goals.replace(participant=room.owner, command=await _goal(codex.participant.id))
    assert first["version"] == 1
    assert first["previous_version"] is None

    second = await goals.replace(
        participant=room.owner,
        command=await _goal(
            codex.participant.id,
            objective="Stop that; own the invoice importer instead",
            expected_version=1,
            worker_disposition=WorkerDisposition.STOP,
            reason="JOB-122 became the room's blocker",
        ),
    )
    assert second["version"] == 2
    assert second["previous_version"] == 1

    live = await goals.current_for(room.room.id, codex.participant.id)
    assert live is not None and live.current is not None
    assert live.current_version == 2
    assert live.current.objective == "Stop that; own the invoice importer instead"
    assert live.current.replaces_version == 1
    assert live.current.worker_disposition is WorkerDisposition.STOP

    # The old wording is still there, stamped forward. "What was this supervisor told
    # when it spawned that worker" has to stay answerable.
    history, total = await goals.history_for(room.room.id, live.id)
    assert total == 2
    superseded = next(v for v in history if v.version == 1)
    assert superseded.superseded_at is not None
    assert superseded.superseded_by_version == 2
    assert superseded.objective == "Ship the reconnect fix"


async def test_a_stale_expected_version_is_refused_and_names_the_current_one(make_room, join):
    room = await make_room()
    codex = await join(room, display_name="Codex")
    await goals.replace(participant=room.owner, command=await _goal(codex.participant.id))
    await goals.replace(
        participant=room.owner,
        command=await _goal(codex.participant.id, expected_version=1, objective="v2"),
    )

    with pytest.raises(RevisionConflict) as exc:
        await goals.replace(
            participant=room.owner,
            command=await _goal(codex.participant.id, expected_version=1, objective="v3 from v1"),
        )
    assert exc.value.details["current_version"] == 2

    # And the room still holds v2, not the stale writer's text.
    live = await goals.current_for(room.room.id, codex.participant.id)
    assert live is not None and live.current is not None
    assert live.current.objective == "v2"


async def test_a_blind_overwrite_has_no_mode(make_room, join):
    """Omitting the version on an existing goal is refused rather than meaning "latest"."""
    room = await make_room()
    codex = await join(room, display_name="Codex")
    await goals.replace(participant=room.owner, command=await _goal(codex.participant.id))

    with pytest.raises(RevisionConflict) as exc:
        await goals.replace(
            participant=room.owner,
            command=await _goal(codex.participant.id, objective="no fence stated"),
        )
    assert exc.value.details["current_version"] == 1


async def test_two_concurrent_replacements_produce_one_winner(make_room, join):
    """The allocator arbitrates, and no version is reused.

    Both callers read version 1 and both try to write version 2. Exactly one may land,
    because the loser's guarded UPDATE matches no row.
    """
    room = await make_room()
    codex = await join(room, display_name="Codex")
    await goals.replace(participant=room.owner, command=await _goal(codex.participant.id))

    results = await asyncio.gather(
        goals.replace(
            participant=room.owner,
            command=await _goal(codex.participant.id, expected_version=1, objective="left"),
        ),
        goals.replace(
            participant=room.owner,
            command=await _goal(codex.participant.id, expected_version=1, objective="right"),
        ),
        return_exceptions=True,
    )
    winners = [r for r in results if isinstance(r, dict)]
    losers = [r for r in results if isinstance(r, RevisionConflict)]
    assert len(winners) == 1, results
    assert len(losers) == 1, results
    assert winners[0]["version"] == 2

    live = await goals.current_for(room.room.id, codex.participant.id)
    assert live is not None
    assert live.current_version == 2
    _, total = await goals.history_for(room.room.id, live.id)
    assert total == 2, "the loser must not have written a third version"


async def test_a_supervisor_cannot_replace_another_supervisors_goal(make_room, join):
    room = await make_room()
    codex = await join(room, display_name="Codex")
    gemini = await join(room, display_name="Gemini")

    with pytest.raises(Forbidden):
        await goals.replace(
            participant=codex.participant,
            command=await _goal(gemini.participant.id, objective="do my bidding"),
        )


async def test_a_supervisor_cannot_give_itself_a_goal(make_room, join):
    """Self-allocation is the failure the job board exists to prevent."""
    room = await make_room()
    codex = await join(room, display_name="Codex")

    with pytest.raises(Forbidden):
        await goals.replace(
            participant=codex.participant,
            command=await _goal(codex.participant.id, objective="whatever I feel like"),
        )


async def test_a_supervisor_may_refine_detail_but_not_rescope_itself(make_room, join):
    room = await make_room()
    codex = await join(room, display_name="Codex")
    await goals.replace(
        participant=room.owner,
        command=await _goal(codex.participant.id, priority=5, worker_plan="one test worker"),
    )

    # Allowed: more detail on how it will do what it was told.
    refined = await goals.replace(
        participant=codex.participant,
        command=await _goal(
            codex.participant.id,
            expected_version=1,
            priority=5,
            worker_plan="one test worker",
            instructions="Starting with backend/tests/test_room_lifecycle.py.",
            reporting_requirements="checkpoint every ten minutes",
        ),
    )
    assert refined["version"] == 2
    live = await goals.current_for(room.room.id, codex.participant.id)
    assert live is not None and live.current is not None
    assert live.current.source is GoalSource.SUPERVISOR
    assert live.current.objective == "Ship the reconnect fix", "objective carried forward"

    # Refused: moving what the orchestrator decided.
    with pytest.raises(Forbidden) as exc:
        await goals.replace(
            participant=codex.participant,
            command=await _goal(
                codex.participant.id,
                expected_version=2,
                objective="something easier",
                priority=5,
            ),
        )
    assert "objective" in exc.value.details["fields"]


async def test_an_observer_cannot_hold_a_goal(make_room, join):
    room = await make_room()
    watcher = await join(room, display_name="Watcher", role=ParticipantRole.OBSERVER)
    assert await roles.role_for(watcher.participant) is RoomRole.OBSERVER

    with pytest.raises(InvalidCommand):
        await goals.replace(participant=room.owner, command=await _goal(watcher.participant.id))


async def test_the_orchestrator_may_hold_its_own_goal(make_room):
    """It is also a supervisor for its own human, and spawns its own workers."""
    room = await make_room()
    result = await goals.replace(participant=room.owner, command=await _goal(room.owner.id))
    assert result["version"] == 1


async def test_acknowledgement_is_evidence_and_never_gates_the_effect(make_room, join):
    room = await make_room()
    codex = await join(room, display_name="Codex")
    issued = await goals.replace(participant=room.owner, command=await _goal(codex.participant.id))

    # The goal is already current, with nobody having acknowledged anything.
    live = await goals.current_for(room.room.id, codex.participant.id)
    assert live is not None and live.current is not None
    assert live.current_version == issued["version"]
    assert live.current.acknowledged_at is None, "in force before any acknowledgement"

    acked = await goals.acknowledge(
        participant=codex.participant,
        command=AcknowledgeGoalCommand(goal_id=live.id, version=1, note="on it"),
    )
    assert acked["acknowledged_at"]

    # A second acknowledgement is idempotent, not an overwrite that loses the first time.
    again = await goals.acknowledge(
        participant=codex.participant,
        command=AcknowledgeGoalCommand(goal_id=live.id, version=1, note="on it again"),
    )
    assert again["already_acknowledged"] is True
    assert again["acknowledged_at"] == acked["acknowledged_at"]


async def test_a_supervisor_may_acknowledge_and_reject(make_room, join):
    """Rejection is information the orchestrator needs, not a veto it must honour."""
    room = await make_room()
    codex = await join(room, display_name="Codex")
    await goals.replace(participant=room.owner, command=await _goal(codex.participant.id))
    live = await goals.current_for(room.room.id, codex.participant.id)
    assert live is not None

    await goals.acknowledge(
        participant=codex.participant,
        command=AcknowledgeGoalCommand(
            goal_id=live.id, version=1, note="capacity is gone", rejected=True
        ),
    )
    after = await goals.current_for(room.room.id, codex.participant.id)
    assert after is not None and after.current is not None
    assert after.current.acknowledged_rejected is True
    assert after.current_version == 1, "the goal is still in force"


async def test_a_superseded_version_cannot_be_acknowledged(make_room, join):
    room = await make_room()
    codex = await join(room, display_name="Codex")
    await goals.replace(participant=room.owner, command=await _goal(codex.participant.id))
    live = await goals.current_for(room.room.id, codex.participant.id)
    assert live is not None
    await goals.replace(
        participant=room.owner,
        command=await _goal(codex.participant.id, expected_version=1, objective="v2"),
    )

    with pytest.raises(InvalidCommand) as exc:
        await goals.acknowledge(
            participant=codex.participant,
            command=AcknowledgeGoalCommand(goal_id=live.id, version=1, note="late"),
        )
    assert exc.value.details["current_version"] == 2


async def test_only_the_holder_may_acknowledge(make_room, join):
    room = await make_room()
    codex = await join(room, display_name="Codex")
    gemini = await join(room, display_name="Gemini")
    await goals.replace(participant=room.owner, command=await _goal(codex.participant.id))
    live = await goals.current_for(room.room.id, codex.participant.id)
    assert live is not None

    with pytest.raises(Forbidden):
        await goals.acknowledge(
            participant=gemini.participant,
            command=AcknowledgeGoalCommand(goal_id=live.id, version=1),
        )


async def test_a_goal_and_a_room_role_survive_a_rejoin(make_room):
    """Both belong to the seat, not to a runtime or a token (D-080).

    A real rejoin, which means the same *identity* redeeming a second invitation --
    `ensure_identity` keys on (owner, display_name), so this is the call a connector makes
    when it reconnects. Creating a fresh identity would produce a second seat, which is a
    different thing entirely.
    """
    room = await make_room()
    first_id, _first_token = await _rejoinable_seat(room, display_name="Codex")
    seat = await store.load_participant_for_room(room.room.id, first_id)
    await goals.replace(
        participant=room.owner,
        command=await _goal(seat.id, objective="survive a reconnect"),
    )
    assert await roles.role_for(seat) is RoomRole.SUPERVISOR

    second_id, second_token = await _rejoinable_seat(room, display_name="Codex")
    assert second_id == first_id, "the same identity lands on the same seat"
    assert second_token != _first_token, "a rejoin rotates the token"

    rejoined = await store.load_participant_for_room(room.room.id, second_id)
    live = await goals.current_for(room.room.id, rejoined.id)
    assert live is not None and live.current is not None
    assert live.current.objective == "survive a reconnect"
    assert live.current_version == 1
    assert await roles.role_for(rejoined) is RoomRole.SUPERVISOR, "position is not re-derived"


async def test_closing_records_a_reason_and_a_time(make_room, join):
    room = await make_room()
    codex = await join(room, display_name="Codex")
    await goals.replace(participant=room.owner, command=await _goal(codex.participant.id))
    live = await goals.current_for(room.room.id, codex.participant.id)
    assert live is not None

    closed = await goals.close(
        participant=codex.participant,
        command=CloseGoalCommand(
            goal_id=live.id, status=GoalStatus.ACHIEVED, reason="reviewed and merged"
        ),
    )
    assert closed["status"] == "achieved"
    assert closed["closed_at"]
    assert await goals.current_for(room.room.id, codex.participant.id) is None

    # Closing twice is refused rather than silently re-closing.
    with pytest.raises(InvalidCommand):
        await goals.close(
            participant=codex.participant,
            command=CloseGoalCommand(goal_id=live.id, status=GoalStatus.ACHIEVED, reason="again"),
        )


async def test_a_supervisor_cannot_abandon_its_own_direction(make_room, join):
    room = await make_room()
    codex = await join(room, display_name="Codex")
    await goals.replace(participant=room.owner, command=await _goal(codex.participant.id))
    live = await goals.current_for(room.room.id, codex.participant.id)
    assert live is not None

    with pytest.raises(Forbidden):
        await goals.close(
            participant=codex.participant,
            command=CloseGoalCommand(
                goal_id=live.id, status=GoalStatus.ABANDONED, reason="do not fancy it"
            ),
        )


async def test_a_goal_from_another_room_is_not_found(make_room, join):
    """Reads are room-scoped, so an id from elsewhere is absent rather than forbidden."""
    first = await make_room(name="First")
    second = await make_room(name="Second")
    codex = await join(first, display_name="Codex")
    await goals.replace(participant=first.owner, command=await _goal(codex.participant.id))
    live = await goals.current_for(first.room.id, codex.participant.id)
    assert live is not None

    assert await goals.current_for(second.room.id, codex.participant.id) is None
    with pytest.raises(NotFound):
        await goals.acknowledge(
            participant=second.owner,
            command=AcknowledgeGoalCommand(goal_id=live.id, version=1),
        )


async def test_no_command_field_can_rewrite_the_immutable_contract(make_room):
    """The orchestrator's authority stops at the protocol's own obligations (§6).

    Asserted structurally rather than behaviourally: if no command model has a field that
    reaches those obligations, no caller can reach them either.
    """
    contract = goals.immutable_contract()
    assert contract, "the contract must not be empty"

    fields = set(ReplaceGoalCommand.model_fields)
    for forbidden in ("immutable_contract", "contract", "runtime_contract", "obligations"):
        assert forbidden not in fields

    # And it is a tuple of plain strings the runtime can present verbatim, not a mutable
    # structure a caller could be handed a reference to.
    assert isinstance(contract, tuple)
    assert all(isinstance(line, str) and line for line in contract)
    room = await make_room()
    assert await store.load_room(room.room.id)
