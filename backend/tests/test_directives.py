"""The control plane: the five properties B had to have before C (D-045).

The acceptance list came from the ChatGPT participant in-room and is reproduced
here as the test names, because a spec agreed in conversation and then paraphrased
in code is a spec nobody can check afterwards.

The through-line: **a directive must not depend on the cooperation of the thing it
is directing.** Everything below is a consequence of that one sentence, including
the single case where waiting is legitimate.
"""

from __future__ import annotations

import pytest

from app.core import directives, presence, projections, tasks
from app.core.errors import ExecutorConflict, InvalidCommand, SteeringHalted
from app.db import database as db
from app.domain.capabilities import HostClass
from app.domain.commands import (
    AcknowledgeDirectiveCommand,
    ClaimTaskCommand,
    CompleteTaskCommand,
    ConnectCommand,
    CreateTaskCommand,
    IssueDirectiveCommand,
)
from app.domain.directive import DirectiveAction, EffectStatus
from app.domain.identity import PrincipalKind
from app.domain.room import ParticipantRole, Scope
from app.domain.task import Steering

from .conftest import FULL_CAPABILITIES

pytestmark = pytest.mark.asyncio


async def _admin(room, join):
    return await join(
        room,
        display_name="Alan",
        kind=PrincipalKind.HUMAN,
        role=ParticipantRole.OWNER,
    )


async def _worker(room, join, label: str = "worker"):
    member = await join(room, display_name="Worker", connect=False)
    negotiated = await presence.connect(
        participant=member.participant,
        command=ConnectCommand(
            capabilities=FULL_CAPABILITIES,
            host_class=HostClass.PERSISTENT_LOCAL,
            attachment_label=label,
        ),
        transport="sse",
    )
    member.connection_id = negotiated.connection.id
    return member


async def _claimed_task(worker, title: str = "Deploy to production"):
    task = await tasks.create(
        participant=worker.participant, command=CreateTaskCommand(title=title)
    )
    claimed = await tasks.claim(
        participant=worker.participant,
        command=ClaimTaskCommand(task_id=task.id, connection_id=worker.connection_id),
    )
    assert claimed.claim is not None
    return claimed


# ---------------------------------------------------------------------------
# 1. STOP lands immediately, whether or not the target ever looks
# ---------------------------------------------------------------------------


async def test_stop_applies_immediately_even_if_the_target_never_polls(make_room, join):
    """The property the whole design turns on.

    If the effect waited for acknowledgement, stopping a runaway worker would
    require the runaway worker's cooperation. So the task is halted in the same
    transaction the directive is written in, and the worker's ignorance is recorded
    rather than being allowed to block anything.
    """
    room = await make_room()
    admin = await _admin(room, join)
    worker = await _worker(room, join)
    claimed = await _claimed_task(worker)
    assert claimed.claim is not None

    directive = await directives.issue(
        participant=admin.participant,
        command=IssueDirectiveCommand(
            target_participant_id=worker.participant.id,
            action=DirectiveAction.STOP,
            task_id=claimed.id,
            reason="prod freeze, stop now",
        ),
    )

    assert directive.effect_status is EffectStatus.APPLIED
    assert directive.applied_at is not None
    assert directive.acknowledged_at is None, "nobody has seen it and it applied anyway"

    row = await db.fetch_one("SELECT * FROM tasks WHERE id = ?", (claimed.id,))
    assert row["steering"] == Steering.STOPPED.value
    assert row["claim_lease_id"] is None, "stop releases the hold, it does not freeze it"

    # And the worker cannot carry on, having never read a thing.
    with pytest.raises(SteeringHalted):
        await tasks.complete(
            participant=worker.participant,
            command=CompleteTaskCommand(
                task_id=claimed.id,
                fence=claimed.claim.fence,
                connection_id=worker.connection_id,
            ),
        )


async def test_a_stopped_task_cannot_be_re_claimed_to_get_around_the_stop(make_room, join):
    """Otherwise `stop` means "stop until your next loop iteration".

    The worker would release, re-claim, and continue having technically obeyed —
    which is the shape of every control that is enforced one layer too high.
    """
    room = await make_room()
    admin = await _admin(room, join)
    worker = await _worker(room, join)
    claimed = await _claimed_task(worker)

    await directives.issue(
        participant=admin.participant,
        command=IssueDirectiveCommand(
            target_participant_id=worker.participant.id,
            action=DirectiveAction.STOP,
            task_id=claimed.id,
            reason="stop",
        ),
    )

    with pytest.raises(SteeringHalted):
        await tasks.claim(
            participant=worker.participant,
            command=ClaimTaskCommand(task_id=claimed.id, connection_id=worker.connection_id),
        )


async def test_pause_keeps_the_hold_but_forbids_progress(make_room, join):
    """Pause and stop are different instructions and must not collapse into one.

    A paused task is still that worker's job — it keeps its place — where a stopped
    one is released for someone else. Collapsing them would make "hold on a moment"
    cost the worker its lease.
    """
    room = await make_room()
    admin = await _admin(room, join)
    worker = await _worker(room, join)
    claimed = await _claimed_task(worker)
    assert claimed.claim is not None

    await directives.issue(
        participant=admin.participant,
        command=IssueDirectiveCommand(
            target_participant_id=worker.participant.id,
            action=DirectiveAction.PAUSE,
            task_id=claimed.id,
            reason="hold while I check something",
        ),
    )

    row = await db.fetch_one("SELECT * FROM tasks WHERE id = ?", (claimed.id,))
    assert row["claim_lease_id"] is not None, "pausing must not cost the worker its place"
    with pytest.raises(SteeringHalted):
        await tasks.complete(
            participant=worker.participant,
            command=CompleteTaskCommand(
                task_id=claimed.id,
                fence=claimed.claim.fence,
                connection_id=worker.connection_id,
            ),
        )

    await directives.issue(
        participant=admin.participant,
        command=IssueDirectiveCommand(
            target_participant_id=worker.participant.id,
            action=DirectiveAction.RESUME,
            task_id=claimed.id,
            reason="carry on",
        ),
    )
    done = await tasks.complete(
        participant=worker.participant,
        command=CompleteTaskCommand(
            task_id=claimed.id,
            fence=claimed.claim.fence,
            connection_id=worker.connection_id,
        ),
    )
    assert done.status.value == "done"


# ---------------------------------------------------------------------------
# 2. Delivery ahead of ordinary events; ack changes nothing about the effect
# ---------------------------------------------------------------------------


async def test_the_worker_is_told_first_and_acking_does_not_re_apply(make_room, join):
    room = await make_room()
    admin = await _admin(room, join)
    worker = await _worker(room, join)
    claimed = await _claimed_task(worker)

    directive = await directives.issue(
        participant=admin.participant,
        command=IssueDirectiveCommand(
            target_participant_id=worker.participant.id,
            action=DirectiveAction.STOP,
            task_id=claimed.id,
            reason="stop",
        ),
    )

    hydrated = await projections.hydrate(room_id=room.room.id, recipient=worker.participant)
    assert next(iter(hydrated)) == "directives_for_you", "told before it is briefed"
    assert [d["id"] for d in hydrated["directives_for_you"]] == [directive.id]

    snapshot = await projections.snapshot(room_id=room.room.id, recipient=worker.participant)
    assert [d["id"] for d in snapshot["directives_for_you"]] == [directive.id]

    before = await db.fetch_one("SELECT * FROM tasks WHERE id = ?", (claimed.id,))
    acked = await directives.acknowledge(
        participant=worker.participant,
        command=AcknowledgeDirectiveCommand(directive_id=directive.id, note="stopping"),
    )
    after = await db.fetch_one("SELECT * FROM tasks WHERE id = ?", (claimed.id,))

    assert acked.acknowledged_at is not None
    assert acked.effect_status is EffectStatus.APPLIED
    assert dict(before) == dict(after), "acknowledging must not touch the task at all"

    # And it drops out of what is waiting for the worker.
    assert await directives.open_for(worker.participant.id) == []


async def test_only_the_target_may_acknowledge(make_room, join):
    """Someone else acknowledging would be evidence of nothing."""
    room = await make_room()
    admin = await _admin(room, join)
    worker = await _worker(room, join)
    claimed = await _claimed_task(worker)

    directive = await directives.issue(
        participant=admin.participant,
        command=IssueDirectiveCommand(
            target_participant_id=worker.participant.id,
            action=DirectiveAction.STOP,
            task_id=claimed.id,
            reason="stop",
        ),
    )

    from app.core.errors import Forbidden

    with pytest.raises(Forbidden):
        await directives.acknowledge(
            participant=admin.participant,
            command=AcknowledgeDirectiveCommand(directive_id=directive.id),
        )


async def test_acknowledging_twice_is_not_a_second_observation(make_room, join):
    room = await make_room()
    admin = await _admin(room, join)
    worker = await _worker(room, join)
    claimed = await _claimed_task(worker)
    directive = await directives.issue(
        participant=admin.participant,
        command=IssueDirectiveCommand(
            target_participant_id=worker.participant.id,
            action=DirectiveAction.STOP,
            task_id=claimed.id,
            reason="stop",
        ),
    )
    first = await directives.acknowledge(
        participant=worker.participant,
        command=AcknowledgeDirectiveCommand(directive_id=directive.id),
    )
    second = await directives.acknowledge(
        participant=worker.participant,
        command=AcknowledgeDirectiveCommand(directive_id=directive.id),
    )
    assert first.acknowledged_at == second.acknowledged_at
    count = await db.fetch_value(
        "SELECT COUNT(*) FROM room_events WHERE type = 'directive.acknowledged'"
    )
    assert count == 1


# ---------------------------------------------------------------------------
# 3. Authority is a grant, not a look
# ---------------------------------------------------------------------------


async def test_a_same_seat_worker_cannot_issue_a_privileged_stop(make_room, join):
    """The security point that produced the correction, tested from the worker side.

    A non-admin participant cannot stop anyone — including itself — through the
    control plane, however human its identity looks. Human-ness is provenance, and
    provenance is attribution rather than verification.
    """
    room = await make_room()
    worker = await _worker(room, join)
    claimed = await _claimed_task(worker)
    assert Scope.ROOM_ADMIN not in worker.participant.scopes

    with pytest.raises(ExecutorConflict):
        await directives.issue(
            participant=worker.participant,
            command=IssueDirectiveCommand(
                target_participant_id=worker.participant.id,
                action=DirectiveAction.STOP,
                task_id=claimed.id,
                reason="stopping myself on my human's behalf, honest",
            ),
        )


async def test_human_origin_is_recorded_and_never_accepted_from_the_caller(make_room, join):
    """It is derived, and it is not what let the issuer through.

    Both halves matter: an admin agent may steer and is stamped `human_origin` false,
    so the audit trail says what actually happened rather than what would look good.
    """
    room = await make_room()
    admin = await _admin(room, join)
    agent_admin = await join(
        room, display_name="Ops agent", kind=PrincipalKind.AGENT, role=ParticipantRole.OWNER
    )
    worker = await _worker(room, join)
    claimed = await _claimed_task(worker)

    by_human = await directives.issue(
        participant=admin.participant,
        command=IssueDirectiveCommand(
            target_participant_id=worker.participant.id,
            action=DirectiveAction.PAUSE,
            task_id=claimed.id,
            reason="hold",
        ),
    )
    by_agent = await directives.issue(
        participant=agent_admin.participant,
        command=IssueDirectiveCommand(
            target_participant_id=worker.participant.id,
            action=DirectiveAction.RESUME,
            task_id=claimed.id,
            reason="carry on",
        ),
    )
    assert by_human.human_origin is True
    assert by_agent.human_origin is False, "an admin agent is authorized, and is not a human"

    assert "human_origin" not in IssueDirectiveCommand.model_fields


async def test_halting_someone_requires_a_reason(make_room, join):
    room = await make_room()
    admin = await _admin(room, join)
    worker = await _worker(room, join)
    claimed = await _claimed_task(worker)

    with pytest.raises(InvalidCommand):
        await directives.issue(
            participant=admin.participant,
            command=IssueDirectiveCommand(
                target_participant_id=worker.participant.id,
                action=DirectiveAction.STOP,
                task_id=claimed.id,
            ),
        )


async def test_a_control_directive_without_a_task_is_refused(make_room, join):
    """Pausing "in general" is not enforceable, and an unenforceable directive is a
    message wearing a uniform."""
    room = await make_room()
    admin = await _admin(room, join)
    worker = await _worker(room, join)

    with pytest.raises(InvalidCommand):
        await directives.issue(
            participant=admin.participant,
            command=IssueDirectiveCommand(
                target_participant_id=worker.participant.id,
                action=DirectiveAction.PAUSE,
                reason="stop everything",
            ),
        )


# ---------------------------------------------------------------------------
# 4. INPUT is the one action where waiting is legitimate
# ---------------------------------------------------------------------------


async def test_input_stays_pending_until_the_worker_consumes_it(make_room, join):
    """No pretend `applied` before the target has it.

    There is no room state to halt, so nothing has happened yet — and saying
    otherwise would make `applied` mean two different things depending on the
    action, which is exactly the fake lifecycle we avoided elsewhere.
    """
    room = await make_room()
    admin = await _admin(room, join)
    worker = await _worker(room, join)

    directive = await directives.issue(
        participant=admin.participant,
        command=IssueDirectiveCommand(
            target_participant_id=worker.participant.id,
            action=DirectiveAction.INPUT,
            reason="use the staging credentials, not production",
        ),
    )
    assert directive.effect_status is EffectStatus.PENDING
    assert directive.applied_at is None

    assert [d.id for d in await directives.open_for(worker.participant.id)] == [directive.id]

    consumed = await directives.acknowledge(
        participant=worker.participant,
        command=AcknowledgeDirectiveCommand(directive_id=directive.id, note="using staging"),
    )
    assert consumed.effect_status is EffectStatus.APPLIED
    assert consumed.applied_at is not None
    assert await directives.open_for(worker.participant.id) == []


async def test_a_worker_may_decline_input_and_the_refusal_is_visible(make_room, join):
    """An agent may say no. The room's job is to show it, not to argue."""
    room = await make_room()
    admin = await _admin(room, join)
    worker = await _worker(room, join)

    directive = await directives.issue(
        participant=admin.participant,
        command=IssueDirectiveCommand(
            target_participant_id=worker.participant.id,
            action=DirectiveAction.INPUT,
            reason="also refactor the auth module while you are in there",
        ),
    )
    refused = await directives.acknowledge(
        participant=worker.participant,
        command=AcknowledgeDirectiveCommand(
            directive_id=directive.id, rejected=True, note="out of scope for this task"
        ),
    )
    assert refused.effect_status is EffectStatus.REJECTED
    assert refused.acknowledged_at is not None


async def test_declining_a_control_directive_does_not_undo_it(make_room, join):
    """`rejected` is an opinion about an instruction, never a veto over an effect."""
    room = await make_room()
    admin = await _admin(room, join)
    worker = await _worker(room, join)
    claimed = await _claimed_task(worker)

    directive = await directives.issue(
        participant=admin.participant,
        command=IssueDirectiveCommand(
            target_participant_id=worker.participant.id,
            action=DirectiveAction.STOP,
            task_id=claimed.id,
            reason="stop",
        ),
    )
    result = await directives.acknowledge(
        participant=worker.participant,
        command=AcknowledgeDirectiveCommand(
            directive_id=directive.id, rejected=True, note="I disagree"
        ),
    )
    assert result.effect_status is EffectStatus.APPLIED, "the effect already landed"
    row = await db.fetch_one("SELECT * FROM tasks WHERE id = ?", (claimed.id,))
    assert row["steering"] == Steering.STOPPED.value


# ---------------------------------------------------------------------------
# 5. The ChatGPT-compatible read path — no new tool on the critical path
# ---------------------------------------------------------------------------


async def test_the_cached_get_room_state_shape_carries_directives(make_room, join):
    """A capability nobody can discover is a capability nobody has (D-040).

    The connector caches its tool list, so this had to arrive through a payload it
    already calls rather than through a tool it would never see.
    """
    from app.adapters.mcp import compact

    room = await make_room()
    admin = await _admin(room, join)
    worker = await _worker(room, join)
    claimed = await _claimed_task(worker)

    directive = await directives.issue(
        participant=admin.participant,
        command=IssueDirectiveCommand(
            target_participant_id=worker.participant.id,
            action=DirectiveAction.STOP,
            task_id=claimed.id,
            reason="prod freeze",
        ),
    )

    snapshot = await projections.snapshot(room_id=room.room.id, recipient=worker.participant)
    state = compact.room_state(snapshot)
    assert [d["id"] for d in state["directives_for_you"]] == [directive.id]
    assert next(iter(state)) == "directives_for_you", "read first, not found by searching"

    resume = await projections.hydrate(room_id=room.room.id, recipient=worker.participant)
    assert [d["id"] for d in resume["directives_for_you"]] == [directive.id]

    # And an uninvolved participant is told nothing about it.
    other = await join(room, display_name="Bystander")
    theirs = await projections.snapshot(room_id=room.room.id, recipient=other.participant)
    assert theirs["directives_for_you"] == []


async def test_directives_arrive_oldest_first(make_room, join):
    """They are instructions. A worker that reads the newest and stops has skipped
    the ones before it — the opposite of how room events are best read."""
    room = await make_room()
    admin = await _admin(room, join)
    worker = await _worker(room, join)

    ids_ = []
    for note in ("first", "second", "third"):
        d = await directives.issue(
            participant=admin.participant,
            command=IssueDirectiveCommand(
                target_participant_id=worker.participant.id,
                action=DirectiveAction.INPUT,
                reason=note,
            ),
        )
        ids_.append(d.id)

    assert [d.id for d in await directives.open_for(worker.participant.id)] == ids_


async def test_the_log_can_say_the_worker_was_told_and_when(make_room, join):
    """What an incident review actually needs: told at seq Y, acted at seq X."""
    room = await make_room()
    admin = await _admin(room, join)
    worker = await _worker(room, join)
    claimed = await _claimed_task(worker)

    directive = await directives.issue(
        participant=admin.participant,
        command=IssueDirectiveCommand(
            target_participant_id=worker.participant.id,
            action=DirectiveAction.STOP,
            task_id=claimed.id,
            reason="prod freeze",
        ),
    )
    await directives.acknowledge(
        participant=worker.participant,
        command=AcknowledgeDirectiveCommand(directive_id=directive.id),
    )

    rows = await db.fetch_all(
        "SELECT * FROM room_events WHERE type IN "
        "('directive.issued','directive.acknowledged','task.steered') "
        "AND room_id = ? ORDER BY seq",
        (room.room.id,),
    )
    types = [r["type"] for r in rows]
    assert types == ["task.steered", "directive.issued", "directive.acknowledged"]

    issued_seq = rows[1]["seq"]
    ack_payload = db.loads(rows[2]["payload"], {})
    assert ack_payload["issued_at_seq"] == issued_seq == directive.created_seq


# ---------------------------------------------------------------------------
# Defects the first live stop test exposed (D-045)
# ---------------------------------------------------------------------------


async def test_a_stopped_task_does_not_read_as_available(make_room, join):
    """The compact board said `open`, which means "take me". It was refused.

    Stop clears the claim, so `status` alone reads as available — and the compact
    view omitted steering entirely. A human watching that board would have seen
    the single most consequential state they can put a task into rendered as its
    opposite.
    """
    from app.adapters.mcp import compact

    room = await make_room()
    admin = await _admin(room, join)
    worker = await _worker(room, join)
    claimed = await _claimed_task(worker)

    await directives.issue(
        participant=admin.participant,
        command=IssueDirectiveCommand(
            target_participant_id=worker.participant.id,
            action=DirectiveAction.STOP,
            task_id=claimed.id,
            reason="prod freeze",
        ),
    )

    snapshot = await projections.snapshot(room_id=room.room.id, recipient=admin.participant)
    entry = next(t for t in compact.room_state(snapshot)["tasks"] if t["task_id"] == claimed.id)
    assert entry["steering"] == "stopped"
    assert entry["claimable"] is False
    assert "prod freeze" in entry["steering_reason"]


async def test_stopping_a_task_ends_its_work_declaration(make_room, join):
    """A work card must not outlive the task it describes.

    After the first live stop the board still read "Working: deploy the staging
    environment" against a task nobody held and nobody could claim — asserting
    activity that had been forbidden minutes earlier, which is worse than showing
    nothing at all.
    """
    from app.core import work as work_service
    from app.domain.commands import DeclareWorkCommand

    room = await make_room()
    admin = await _admin(room, join)
    worker = await _worker(room, join)
    claimed = await _claimed_task(worker)

    declared = await work_service.declare(
        participant=worker.participant,
        command=DeclareWorkCommand(
            headline="Working: deploy the staging environment", task_id=claimed.id
        ),
    )
    assert declared.ended_at is None

    await directives.issue(
        participant=admin.participant,
        command=IssueDirectiveCommand(
            target_participant_id=worker.participant.id,
            action=DirectiveAction.STOP,
            task_id=claimed.id,
            reason="prod freeze",
        ),
    )

    row = await db.fetch_one("SELECT * FROM work_declarations WHERE id = ?", (declared.id,))
    assert row["ended_at"] is not None
    assert row["end_reason"] == "superseded"


async def test_a_worker_can_say_it_has_started_over_http(make_room, join):
    """A worker got 405 trying to report progress, and the route existed.

    It was `PATCH /tasks` while every sibling — claim, renew, release, complete,
    cancel, take-over, steer — is `POST /tasks/<verb>`. The worker followed the
    pattern its neighbours set and was refused, so the board could not distinguish
    *held* from *being worked* for any client that had not read the route table.

    An API whose shape cannot be inferred from its own siblings is a defect even
    when every individual route is defensible.
    """
    from app.api.routes import router

    paths = {getattr(r, "path", "") for r in router.routes}
    assert "/api/rooms/{room_id}/tasks/update" in paths

    room = await make_room()
    worker = await _worker(room, join)
    claimed = await _claimed_task(worker)
    assert claimed.claim is not None

    from app.domain.commands import UpdateTaskCommand

    updated = await tasks.update(
        participant=worker.participant,
        command=UpdateTaskCommand(
            task_id=claimed.id,
            fence=claimed.claim.fence,
            in_progress=True,
            connection_id=worker.connection_id,
        ),
    )
    assert updated.status.value == "in_progress"
