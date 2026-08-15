"""The attachment state axis: which *runtime* of a seat is executing (D-032 → D-035).

Written alongside the feature rather than after it, per D-033. The axis is not
"connected or not" — it is the cross product of: does this client have a durable
runtime identity, does it claim continuity for it, is that runtime still live, and
is the caller the same runtime or a sibling sharing the seat.

The failure this whole area exists to prevent is not a database race. It is two
runtimes of the same participant both believing they are doing one task, and each
performing the external half of it. Cottage fencing cannot recall a deployment.
"""

from __future__ import annotations

import pytest

from app.core import presence, store, tasks
from app.core.errors import (
    AmbiguousExecutor,
    ExecutorConflict,
    InvalidCommand,
    StaleFence,
)
from app.db import database as db
from app.domain.capabilities import Capability, HostClass
from app.domain.commands import (
    ClaimTaskCommand,
    CompleteTaskCommand,
    ConnectCommand,
    CreateTaskCommand,
    ReleaseClaimCommand,
    TakeOverExecutionCommand,
)
from app.domain.identity import PrincipalKind
from app.domain.room import Liveness, ParticipantRole, Scope

from .conftest import ATTENDED_CAPABILITIES, FULL_CAPABILITIES

pytestmark = pytest.mark.asyncio


async def _connect(
    member,
    *,
    label: str | None = None,
    resumable: bool = True,
    capabilities=None,
    host_class: HostClass = HostClass.PERSISTENT_LOCAL,
    transport: str = "sse",
) -> str:
    negotiated = await presence.connect(
        participant=member.participant,
        command=ConnectCommand(
            capabilities=capabilities or FULL_CAPABILITIES,
            host_class=host_class,
            attachment_label=label,
            attachment_resumable=resumable,
        ),
        transport=transport,
    )
    return negotiated.connection.id


async def _close(connection_id: str) -> None:
    """Close a connection without going through disconnect.

    Disconnect releases the seat's claims when it was the last connection, which
    would destroy the very state these tests are about. What is being simulated
    here is one *runtime* of a still-connected seat going away.
    """
    await db.execute(
        "UPDATE connections SET closed_at = '2020-01-01T00:00:00.000Z' WHERE id = ?",
        (connection_id,),
    )


async def _new_task(member, title: str = "Deploy the thing"):
    return await tasks.create(
        participant=member.participant,
        command=CreateTaskCommand(title=title),
    )


async def _row(task_id: str):
    return await db.fetch_one("SELECT * FROM tasks WHERE id = ?", (task_id,))


# ---------------------------------------------------------------------------
# What identity a connection resolves to
# ---------------------------------------------------------------------------


async def test_no_label_is_ephemeral_and_the_connection_is_the_executor(make_room, join):
    """The honest default. No durable runtime declared, so none is invented."""
    room = await make_room()
    worker = await join(room, display_name="Worker")

    task = await _new_task(worker)
    await tasks.claim(participant=worker.participant, command=ClaimTaskCommand(task_id=task.id))

    row = await _row(task.id)
    assert row["executor_attachment_id"] is None
    assert row["executor_connection_id"] == worker.connection_id
    assert await db.fetch_value("SELECT COUNT(*) FROM attachments") == 0


async def test_a_label_creates_one_attachment_that_reconnects_land_on(make_room, join):
    """Reattachment is a lookup on `UNIQUE (participant_id, label)`, not a guess."""
    room = await make_room()
    worker = await join(room, display_name="Worker", connect=False)

    first = await _connect(worker, label="worker-main")
    second = await _connect(worker, label="worker-main")

    rows = await db.fetch_all("SELECT * FROM attachments")
    assert len(rows) == 1, "the same label must not accumulate identities"

    conn_a = await store.load_connection(first)
    conn_b = await store.load_connection(second)
    assert conn_a.attachment_id == conn_b.attachment_id == rows[0]["id"]


async def test_declining_resumability_is_recorded_without_changing_affinity(make_room, join):
    """`is_resumable=False` is a declaration about *process* restarts, not a switch.

    Making it select connection-scoping instead was the first implementation, and
    it was wrong in a way worth keeping written down: with several connections of
    one non-resumable runtime there is no principled way to pick which connection
    is "the" executor, so the flag reintroduced exactly the guess that
    `AmbiguousExecutor` exists to refuse. Affinity keys on the attachment either
    way and lapses when nothing of it is live — which is already what "cannot
    resume" means operationally.
    """
    room = await make_room()
    worker = await join(room, display_name="Worker", connect=False)
    await _connect(worker, label="chat-turn", resumable=False)
    await _connect(worker, label="chat-turn", resumable=False)

    task = await _new_task(worker)
    await tasks.claim(participant=worker.participant, command=ClaimTaskCommand(task_id=task.id))

    row = await _row(task.id)
    assert row["executor_attachment_id"] is not None
    assert row["executor_connection_id"] is None
    assert await db.fetch_value("SELECT is_resumable FROM attachments") == 0


async def test_two_runtimes_with_no_name_given_is_refused_not_guessed(make_room, join):
    """The whole reason `AmbiguousExecutor` exists.

    Picking the most recent connection would record an executor that is not doing
    the work, and every later affinity check would then be answered about the wrong
    runtime — confidently.
    """
    room = await make_room()
    seat = await join(room, display_name="Shared seat", connect=False)
    await _connect(seat, label="worker")
    await _connect(seat, label="chat")

    task = await _new_task(seat)
    with pytest.raises(AmbiguousExecutor) as exc:
        await tasks.claim(participant=seat.participant, command=ClaimTaskCommand(task_id=task.id))
    assert len(exc.value.details["connection_ids"]) == 2


async def test_two_connections_of_one_attachment_are_not_ambiguous(make_room, join):
    """Two transports of one runtime are one executor, so nothing needs naming."""
    room = await make_room()
    worker = await join(room, display_name="Worker", connect=False)
    await _connect(worker, label="worker-main")
    await _connect(worker, label="worker-main")

    task = await _new_task(worker)
    await tasks.claim(participant=worker.participant, command=ClaimTaskCommand(task_id=task.id))

    row = await _row(task.id)
    assert row["executor_attachment_id"] is not None


async def test_naming_a_connection_that_is_not_yours_is_rejected(make_room, join):
    room = await make_room()
    a = await join(room, display_name="A")
    b = await join(room, display_name="B")

    task = await _new_task(a)
    with pytest.raises(InvalidCommand):
        await tasks.claim(
            participant=a.participant,
            command=ClaimTaskCommand(task_id=task.id, connection_id=b.connection_id),
        )


# ---------------------------------------------------------------------------
# Affinity: a sibling runtime of the same seat
# ---------------------------------------------------------------------------


async def test_a_live_sibling_runtime_cannot_complete_the_executors_work(make_room, join):
    """The holder is the seat; the executor is one runtime of it. D-035."""
    room = await make_room()
    seat = await join(room, display_name="Shared seat", connect=False)
    worker_conn = await _connect(seat, label="worker")
    chat_conn = await _connect(seat, label="chat")

    task = await _new_task(seat)
    claimed = await tasks.claim(
        participant=seat.participant,
        command=ClaimTaskCommand(task_id=task.id, connection_id=worker_conn),
    )
    assert claimed.claim is not None

    with pytest.raises(ExecutorConflict):
        await tasks.complete(
            participant=seat.participant,
            command=CompleteTaskCommand(
                task_id=task.id, fence=claimed.claim.fence, connection_id=chat_conn
            ),
        )


async def test_a_live_sibling_runtime_cannot_release_the_executors_work(make_room, join):
    """D-034 said release was harmless; D-035 reversed it, and this is the reason.

    Releasing frees a third party to start the same external action, which is the
    same end state as seizing the lease.
    """
    room = await make_room()
    seat = await join(room, display_name="Shared seat", connect=False)
    worker_conn = await _connect(seat, label="worker")
    chat_conn = await _connect(seat, label="chat")

    task = await _new_task(seat)
    claimed = await tasks.claim(
        participant=seat.participant,
        command=ClaimTaskCommand(task_id=task.id, connection_id=worker_conn),
    )
    assert claimed.claim is not None

    with pytest.raises(ExecutorConflict):
        await tasks.release(
            participant=seat.participant,
            command=ReleaseClaimCommand(
                task_id=task.id, fence=claimed.claim.fence, connection_id=chat_conn
            ),
        )


async def test_a_sibling_may_act_once_the_executor_is_no_longer_live(make_room, join):
    """Leases, not locks. A crashed worker must not strand the work forever.

    This is the branch that makes the rule survivable: affinity is a guard against
    *concurrent* execution, never a permanent reservation.
    """
    room = await make_room()
    seat = await join(room, display_name="Shared seat", connect=False)
    worker_conn = await _connect(seat, label="worker")
    chat_conn = await _connect(seat, label="chat")

    task = await _new_task(seat)
    claimed = await tasks.claim(
        participant=seat.participant,
        command=ClaimTaskCommand(task_id=task.id, connection_id=worker_conn),
    )
    assert claimed.claim is not None
    await _close(worker_conn)

    done = await tasks.complete(
        participant=seat.participant,
        command=CompleteTaskCommand(
            task_id=task.id,
            fence=claimed.claim.fence,
            result="finished after the worker died",
            connection_id=chat_conn,
        ),
    )
    assert done.status.value == "done"


async def test_a_sibling_cannot_silently_become_the_executor_by_re_claiming(make_room, join):
    """The idempotent re-claim branch matches on the *seat*, which is not enough.

    Without this check the cheapest possible takeover is also the most invisible
    one: re-claim your own seat's lease and the executor column quietly changes
    under a runtime that is still working.
    """
    room = await make_room()
    seat = await join(room, display_name="Shared seat", connect=False)
    worker_conn = await _connect(seat, label="worker")
    chat_conn = await _connect(seat, label="chat")

    task = await _new_task(seat)
    await tasks.claim(
        participant=seat.participant,
        command=ClaimTaskCommand(task_id=task.id, connection_id=worker_conn),
    )

    with pytest.raises(ExecutorConflict):
        await tasks.claim(
            participant=seat.participant,
            command=ClaimTaskCommand(task_id=task.id, connection_id=chat_conn),
        )

    row = await _row(task.id)
    executor = await presence.executor_of(row)
    worker = await store.load_connection(worker_conn)
    assert executor.ref == worker.attachment_id


async def test_the_executor_itself_may_re_claim_and_finish(make_room, join):
    """Affinity must not obstruct the runtime it belongs to."""
    room = await make_room()
    seat = await join(room, display_name="Shared seat", connect=False)
    worker_conn = await _connect(seat, label="worker")
    await _connect(seat, label="chat")

    task = await _new_task(seat)
    await tasks.claim(
        participant=seat.participant,
        command=ClaimTaskCommand(task_id=task.id, connection_id=worker_conn),
    )
    again = await tasks.claim(
        participant=seat.participant,
        command=ClaimTaskCommand(task_id=task.id, connection_id=worker_conn),
    )
    assert again.claim is not None

    done = await tasks.complete(
        participant=seat.participant,
        command=CompleteTaskCommand(
            task_id=task.id, fence=again.claim.fence, connection_id=worker_conn
        ),
    )
    assert done.status.value == "done"


async def test_affinity_survives_the_executors_transport_dying_and_reconnecting(make_room, join):
    """The point of a durable attachment: transport churn is not identity churn.

    Keyed on `connection_id` this would clear on every reconnect — which is the
    measurement from the ChatGPT participant that produced D-032 in the first place.
    """
    room = await make_room()
    seat = await join(room, display_name="Shared seat", connect=False)
    worker_conn = await _connect(seat, label="worker")
    chat_conn = await _connect(seat, label="chat")

    task = await _new_task(seat)
    claimed = await tasks.claim(
        participant=seat.participant,
        command=ClaimTaskCommand(task_id=task.id, connection_id=worker_conn),
    )
    assert claimed.claim is not None

    await _close(worker_conn)
    await _connect(seat, label="worker")  # same runtime, new transport

    with pytest.raises(ExecutorConflict):
        await tasks.complete(
            participant=seat.participant,
            command=CompleteTaskCommand(
                task_id=task.id, fence=claimed.claim.fence, connection_id=chat_conn
            ),
        )


async def test_a_lease_with_no_recorded_executor_imposes_no_affinity(make_room, join):
    """Absence is not a constraint.

    Every lease taken before these columns existed has NULL in them. Reading that
    as "nobody may finish this" would have stranded live work on deploy.
    """
    room = await make_room()
    seat = await join(room, display_name="Shared seat", connect=False)
    worker_conn = await _connect(seat, label="worker")
    chat_conn = await _connect(seat, label="chat")

    task = await _new_task(seat)
    claimed = await tasks.claim(
        participant=seat.participant,
        command=ClaimTaskCommand(task_id=task.id, connection_id=worker_conn),
    )
    assert claimed.claim is not None
    await db.execute(
        "UPDATE tasks SET executor_attachment_id = NULL, executor_connection_id = NULL "
        "WHERE id = ?",
        (task.id,),
    )

    done = await tasks.complete(
        participant=seat.participant,
        command=CompleteTaskCommand(
            task_id=task.id, fence=claimed.claim.fence, connection_id=chat_conn
        ),
    )
    assert done.status.value == "done"


async def test_a_different_participant_is_still_a_lease_conflict_not_an_executor_one(
    make_room, join
):
    """Affinity is an intra-seat rule. It must not soften the inter-seat one."""
    room = await make_room()
    a = await join(room, display_name="A")
    b = await join(room, display_name="B")

    task = await _new_task(a)
    claimed = await tasks.claim(
        participant=a.participant, command=ClaimTaskCommand(task_id=task.id)
    )
    assert claimed.claim is not None

    from app.core.errors import LeaseConflict

    with pytest.raises(LeaseConflict):
        await tasks.claim(participant=b.participant, command=ClaimTaskCommand(task_id=task.id))


# ---------------------------------------------------------------------------
# The escape hatches: nothing may hold work hostage
# ---------------------------------------------------------------------------


async def test_taking_over_moves_execution_and_makes_the_old_fence_stale(make_room, join):
    """The visible alternative to a silent re-claim.

    The fence increment is the safety property, not bookkeeping: the displaced
    runtime's next mutation must fail rather than land late on work it no longer
    owns.
    """
    room = await make_room()
    seat = await join(room, display_name="Shared seat", connect=False)
    worker_conn = await _connect(seat, label="worker")
    chat_conn = await _connect(seat, label="chat")

    task = await _new_task(seat)
    claimed = await tasks.claim(
        participant=seat.participant,
        command=ClaimTaskCommand(task_id=task.id, connection_id=worker_conn),
    )
    assert claimed.claim is not None
    old_fence = claimed.claim.fence

    taken = await tasks.take_over_execution(
        participant=seat.participant,
        command=TakeOverExecutionCommand(
            task_id=task.id,
            fence=old_fence,
            reason="worker is wedged on a prompt nobody will answer",
            connection_id=chat_conn,
        ),
    )
    assert taken.claim is not None
    assert taken.claim.fence > old_fence
    assert taken.claim.participant_id == seat.participant.id, "the holder did not change"

    chat = await store.load_connection(chat_conn)
    assert taken.claim.executor_attachment_id == chat.attachment_id

    with pytest.raises(StaleFence):
        await tasks.complete(
            participant=seat.participant,
            command=CompleteTaskCommand(
                task_id=task.id, fence=old_fence, connection_id=worker_conn
            ),
        )


async def test_taking_over_says_in_the_room_that_it_happened(make_room, join):
    """An override that leaves no trace is indistinguishable from a bug."""
    room = await make_room()
    seat = await join(room, display_name="Shared seat", connect=False)
    worker_conn = await _connect(seat, label="worker")
    chat_conn = await _connect(seat, label="chat")

    task = await _new_task(seat)
    claimed = await tasks.claim(
        participant=seat.participant,
        command=ClaimTaskCommand(task_id=task.id, connection_id=worker_conn),
    )
    assert claimed.claim is not None
    await tasks.take_over_execution(
        participant=seat.participant,
        command=TakeOverExecutionCommand(
            task_id=task.id,
            fence=claimed.claim.fence,
            reason="human said stop",
            connection_id=chat_conn,
        ),
    )

    rows = await db.fetch_all(
        "SELECT * FROM room_events WHERE type = 'task.executor_changed' AND room_id = ?",
        (room.room.id,),
    )
    assert len(rows) == 1
    payload = db.loads(rows[0]["payload"], {})
    assert payload["reason"] == "human said stop"
    assert payload["previous_executor_live"] is True, "the log must say it was a live seizure"


async def test_taking_over_what_you_already_execute_is_idempotent(make_room, join):
    """A retry after an ambiguous failure must not read as a second takeover."""
    room = await make_room()
    worker = await join(room, display_name="Worker", connect=False)
    conn = await _connect(worker, label="worker")

    task = await _new_task(worker)
    claimed = await tasks.claim(
        participant=worker.participant,
        command=ClaimTaskCommand(task_id=task.id, connection_id=conn),
    )
    assert claimed.claim is not None

    unchanged = await tasks.take_over_execution(
        participant=worker.participant,
        command=TakeOverExecutionCommand(
            task_id=task.id, fence=claimed.claim.fence, reason="retry", connection_id=conn
        ),
    )
    assert unchanged.claim is not None
    assert unchanged.claim.fence == claimed.claim.fence
    assert (
        await db.fetch_value(
            "SELECT COUNT(*) FROM room_events WHERE type = 'task.executor_changed'"
        )
        == 0
    )


async def test_being_a_human_principal_is_not_authorization(make_room, join):
    """The correction that mattered most in this area, so it gets its own test.

    The first implementation accepted `identity.kind == HUMAN` as authority to
    override. That looked safe — the field is stamped server-side, so a caller
    cannot forge it — but unforgeable is not the property required. `kind` records
    whose identity this is; it says nothing about who is at the keyboard, so an
    unattended runtime holding a human-kind participant's credentials would have
    manufactured "a human said stop" out of its own token.

    Provenance is attribution, not verification (`docs/SECURITY.md`). This was
    authorization. They are different questions and only one of them is answerable.
    """
    room = await make_room()
    human = await join(room, display_name="Alan", kind=PrincipalKind.HUMAN, connect=False)
    worker_conn = await _connect(human, label="worker")
    chat_conn = await _connect(human, label="chat")

    task = await _new_task(human)
    claimed = await tasks.claim(
        participant=human.participant,
        command=ClaimTaskCommand(task_id=task.id, connection_id=worker_conn),
    )
    assert claimed.claim is not None
    assert Scope.ROOM_ADMIN not in human.participant.scopes

    with pytest.raises(ExecutorConflict):
        await tasks.release(
            participant=human.participant,
            command=ReleaseClaimCommand(
                task_id=task.id,
                fence=claimed.claim.fence,
                force=True,
                reason="I am a person, let me through",
                connection_id=chat_conn,
            ),
        )


async def test_a_room_admin_may_force_release_but_not_silently(make_room, join):
    """Human preemption, which the steering channel depends on — with a reason."""
    room = await make_room()
    admin = await join(
        room,
        display_name="Alan",
        kind=PrincipalKind.HUMAN,
        role=ParticipantRole.OWNER,
        connect=False,
    )
    worker_conn = await _connect(admin, label="worker")
    chat_conn = await _connect(admin, label="chat")
    assert Scope.ROOM_ADMIN in admin.participant.scopes

    task = await _new_task(admin)
    claimed = await tasks.claim(
        participant=admin.participant,
        command=ClaimTaskCommand(task_id=task.id, connection_id=worker_conn),
    )
    assert claimed.claim is not None

    with pytest.raises(InvalidCommand):
        await tasks.release(
            participant=admin.participant,
            command=ReleaseClaimCommand(
                task_id=task.id, fence=claimed.claim.fence, force=True, connection_id=chat_conn
            ),
        )

    released = await tasks.release(
        participant=admin.participant,
        command=ReleaseClaimCommand(
            task_id=task.id,
            fence=claimed.claim.fence,
            force=True,
            reason="stopping the deploy, requirements changed",
            connection_id=chat_conn,
        ),
    )
    assert released.claim is None

    rows = await db.fetch_all(
        "SELECT * FROM room_events WHERE type = 'task.claim_released' AND room_id = ?",
        (room.room.id,),
    )
    payload = db.loads(rows[-1]["payload"], {})
    assert payload["forced"] is True
    assert "requirements changed" in payload["reason"]


# ---------------------------------------------------------------------------
# The executor never outlives its lease
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("ending", ["complete", "release"])
async def test_the_executor_is_cleared_wherever_the_claim_is_cleared(make_room, join, ending):
    room = await make_room()
    worker = await join(room, display_name="Worker", connect=False)
    conn = await _connect(worker, label="worker")

    task = await _new_task(worker)
    claimed = await tasks.claim(
        participant=worker.participant,
        command=ClaimTaskCommand(task_id=task.id, connection_id=conn),
    )
    assert claimed.claim is not None
    assert claimed.claim.executor_attachment_id is not None

    if ending == "complete":
        await tasks.complete(
            participant=worker.participant,
            command=CompleteTaskCommand(task_id=task.id, fence=claimed.claim.fence),
        )
    else:
        await tasks.release(
            participant=worker.participant,
            command=ReleaseClaimCommand(task_id=task.id, fence=claimed.claim.fence),
        )

    row = await _row(task.id)
    assert row["executor_attachment_id"] is None
    assert row["executor_connection_id"] is None


async def test_lease_expiry_clears_the_executor_but_the_event_keeps_it(make_room, join):
    """The row forgets; the log does not.

    A recovery claim has to be able to say which runtime went quiet mid-flight
    (D-036), and after expiry the event is the only place that still knows.
    """
    room = await make_room()
    worker = await join(room, display_name="Worker", connect=False)
    conn = await _connect(worker, label="worker")

    task = await _new_task(worker)
    claimed = await tasks.claim(
        participant=worker.participant,
        command=ClaimTaskCommand(task_id=task.id, connection_id=conn),
    )
    assert claimed.claim is not None
    attachment_id = claimed.claim.executor_attachment_id

    await db.execute(
        "UPDATE tasks SET claim_expires_at = '2020-01-01T00:00:00.000Z' WHERE id = ?",
        (task.id,),
    )
    events = await tasks.reap_expired_leases()

    row = await _row(task.id)
    assert row["executor_attachment_id"] is None
    expired = [e for e in events if e.type.value == "task.claim_expired"]
    assert expired and expired[0].payload["executor_attachment_id"] == attachment_id


# ---------------------------------------------------------------------------
# Capabilities are a property of the runtime, not of the seat
# ---------------------------------------------------------------------------


async def test_a_resumed_attachment_is_judged_on_what_it_declares_now(make_room, join):
    """Redeployed with different abilities is a truthful new declaration, not a lie.

    Pinning an attachment to its first declaration would make the durable-identity
    feature actively harmful: a worker that gained the ability to run unattended
    would be refused on the strength of a claim it no longer makes.
    """
    room = await make_room()
    worker = await join(room, display_name="Worker", connect=False)

    await _connect(
        worker,
        label="worker",
        capabilities=ATTENDED_CAPABILITIES,
        host_class=HostClass.INTERACTIVE_CLIENT,
        transport="long_poll",
    )
    attached = await db.fetch_one("SELECT * FROM attachments")
    attended_policy = await presence.runtime_policy_for(worker.participant, room.room)

    await db.execute("UPDATE connections SET closed_at = '2020-01-01T00:00:00.000Z'")
    await _connect(worker, label="worker", capabilities=FULL_CAPABILITIES)

    after = await db.fetch_all("SELECT * FROM attachments")
    assert len(after) == 1 and after[0]["id"] == attached["id"], "same runtime, same row"

    unattended_policy = await presence.runtime_policy_for(worker.participant, room.room)
    assert attended_policy.max_lease_seconds < unattended_policy.max_lease_seconds


async def test_a_chat_runtime_does_not_borrow_its_seats_workers_standing(make_room, join):
    """Honest capabilities, applied to a seat with two very different runtimes.

    Derived from the *best* connection of the participant, an attended chat surface
    claiming work would inherit the background worker's lease ceiling — a lie
    produced by nothing more than sharing a seat.
    """
    room = await make_room()
    seat = await join(room, display_name="Shared seat", connect=False)
    worker_conn = await _connect(seat, label="worker", capabilities=FULL_CAPABILITIES)
    chat_conn = await _connect(
        seat,
        label="chat",
        capabilities=ATTENDED_CAPABILITIES,
        host_class=HostClass.INTERACTIVE_CLIENT,
        transport="long_poll",
    )

    worker_executor = await presence.resolve_executor(
        participant=seat.participant, connection_id=worker_conn
    )
    chat_executor = await presence.resolve_executor(
        participant=seat.participant, connection_id=chat_conn
    )
    worker_policy = await presence.runtime_policy_for(
        seat.participant, room.room, executor=worker_executor
    )
    chat_policy = await presence.runtime_policy_for(
        seat.participant, room.room, executor=chat_executor
    )

    assert chat_policy.max_lease_seconds < worker_policy.max_lease_seconds


async def test_a_stale_executor_is_not_a_live_one(make_room, join):
    """Live means heard from, not merely `closed_at IS NULL`.

    A process that stopped heartbeating three intervals ago is not evidence that
    anything is still executing, and treating an unclosed socket as proof of work
    in flight would block recovery on exactly the failure recovery is for.
    """
    room = await make_room()
    seat = await join(room, display_name="Shared seat", connect=False)
    worker_conn = await _connect(seat, label="worker")
    chat_conn = await _connect(seat, label="chat")

    task = await _new_task(seat)
    claimed = await tasks.claim(
        participant=seat.participant,
        command=ClaimTaskCommand(task_id=task.id, connection_id=worker_conn),
    )
    assert claimed.claim is not None

    await db.execute(
        "UPDATE connections SET last_heartbeat_at = '2020-01-01T00:00:00.000Z' WHERE id = ?",
        (worker_conn,),
    )
    row = await _row(task.id)
    executor = await presence.executor_of(row)
    # STALE, not DISCONNECTED: the socket is still open, which is precisely the
    # case this test exists for. `closed_at IS NULL` would have called it live.
    assert presence.grade_connection(executor.connections[0]) is Liveness.STALE
    assert not executor.is_live

    done = await tasks.complete(
        participant=seat.participant,
        command=CompleteTaskCommand(
            task_id=task.id, fence=claimed.claim.fence, connection_id=chat_conn
        ),
    )
    assert done.status.value == "done"


async def test_registering_a_runtime_is_an_event_and_reattaching_is_not(make_room, join):
    """A state change appends an event (principle 1); a lookup does not.

    Emitting one per connection would make the log unable to answer the question it
    exists for — whether a *new* worker appeared or an existing one reconnected.
    """
    room = await make_room()
    worker = await join(room, display_name="Worker", connect=False)

    await _connect(worker, label="worker-main")
    await _connect(worker, label="worker-main")
    await _connect(worker, label="worker-side")

    rows = await db.fetch_all(
        "SELECT * FROM room_events WHERE type = 'presence.attachment_registered' "
        "AND room_id = ? ORDER BY seq",
        (room.room.id,),
    )
    assert len(rows) == 2
    labels = [db.loads(r["payload"], {})["label"] for r in rows]
    assert labels == ["worker-main", "worker-side"]


async def test_capabilities_are_not_a_label(make_room, join):
    """Restates principle 4 on the new path: `unknown` host class, full capabilities.

    If the attachment work had made runtime policy consult `host_class` anywhere,
    this is where it would show.
    """
    room = await make_room()
    worker = await join(room, display_name="Worker", connect=False)
    await _connect(
        worker,
        label="worker",
        capabilities=FULL_CAPABILITIES,
        host_class=HostClass.UNKNOWN,
    )

    policy = await presence.runtime_policy_for(worker.participant, room.room)
    assert policy.may_claim
    assert Capability.CAN_EXECUTE_BACKGROUND in FULL_CAPABILITIES
