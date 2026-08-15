"""Durable progress on a task, so a restart is not an amnesia (D-050).

Three decisions here are load-bearing and worth reading before changing anything.

**A checkpoint is fenced like every other claim about work in flight.** It is not a
comment; it asserts that a particular run of a particular task reached a particular
point. A runtime whose lease has moved on must not be able to append to that record,
for the same reason it must not be able to complete the task.

**Two audiences means two events.** An event carries exactly one audience, so a
record with a public half and a private half is two frames appended in one
transaction — never one frame that projections remember to redact. The redaction
approach fails the way this codebase has already failed four times (D-049): it is
correct in the three places someone thought of and absent in the fourth.

**Room admins can still audit the private half**, because they can audit every
directed payload in a room they administer (`docs/SECURITY.md` §6). That is stated
plainly rather than quietly, since the alternative — a second visibility rule that
applies to this one table — would be a claim the system does not actually enforce
anywhere else.
"""

from __future__ import annotations

import logging
from typing import Any

from ..db import database as db
from ..domain import ids
from ..domain.checkpoint import Checkpoint, ResumeState
from ..domain.commands import AppendCheckpointCommand
from ..domain.disclosure import Audience, Disclosure, DisclosureDecision
from ..domain.events import EventEnvelope, EventType
from ..domain.room import Participant, PrivacyClass, Scope
from ..util import utcnow_iso
from . import authz, eventlog, presence, privacy, store, tasks
from .actors import actor_for
from .dispatch import CommandOutcome, execute_command
from .errors import NotFound

log = logging.getLogger(__name__)

#: How many checkpoints a resume projection carries. A worker needs where it got to,
#: not its whole history — and every line returned is spent context for the model
#: reading it, which on a metered host is the user's money (`docs/INTEROP.md` §4).
DEFAULT_LATEST = 5
MAX_PAGE = 100


async def append(*, participant: Participant, command: AppendCheckpointCommand) -> Checkpoint:
    """Record progress on work this caller holds.

    Deliberately **not** blocked by steering. `pause` means do not progress; writing
    down where you got to is the opposite of progressing, and it is exactly what a
    paused worker should do before it stops touching anything. `stop` releases the
    lease, so a stopped worker cannot reach here — which is why a worker should
    checkpoint after each step rather than only at the end, and why `next_action` is
    part of the bookmark.
    """
    room = await store.load_room(participant.room_id)
    # The same scope that reports progress through `task.update`: this is progress
    # reporting, and giving it its own scope would mean a runtime credential that
    # can say "in progress" but not say what happened (D-048).
    authz.require_scope(participant, Scope.TASK_PROGRESS)
    authz.require_writable(room)

    resume = command.resume_state
    decision = privacy.check_disclosure(
        room=room,
        participant=participant,
        disclosure=_public_disclosure(),
        content=[command.summary],
    )
    if resume is not None:
        # Inspected even though it is same-seat: the entropy screen exists to catch a
        # credential pasted into free text, and a bookmark is a plausible place for
        # one to end up. Privacy of the *audience* is not a reason to skip the
        # content check — those are the two separate controls in `docs/SECURITY.md` §2.
        privacy.inspect_content(
            resume.phase,
            resume.next_action,
            resume.completed_step_ids,
            resume.artifact_refs,
            resume.pending_tool_calls,
        )

    checkpoint_id = ids.new_id(ids.CHECKPOINT)
    now = utcnow_iso()

    async def body(tx: db.Tx) -> CommandOutcome:
        row = await tx.fetch_one(
            "SELECT * FROM tasks WHERE id = ? AND room_id = ?", (command.task_id, room.id)
        )
        if row is None:
            raise NotFound("Task does not exist.", task_id=command.task_id)
        tasks.assert_fence(row, command.fence)
        tasks.require_live_lease(row, participant)
        await tasks.require_executor_or_dead(
            tx,
            row,
            participant=participant,
            connection_id=command.connection_id,
            action="checkpoint",
        )

        executor = await presence.executor_of(row, tx=tx)
        events: list[EventEnvelope] = []
        public = await eventlog.append(
            tx,
            room_id=room.id,
            type_=EventType.TASK_CHECKPOINTED,
            actor=actor_for(participant),
            payload={
                "checkpoint_id": checkpoint_id,
                "task_id": command.task_id,
                "participant_id": participant.id,
                "attachment_id": executor.attachment_id,
                "fence": command.fence,
                "summary": command.summary,
                # Public on purpose: "there is state you cannot see" is not itself a
                # secret, and hiding the fact would make the room's account of a
                # worker's progress quietly incomplete.
                "has_resume_state": resume is not None,
            },
            disclosure=decision,
            causation_id=command.command_id,
        )
        events.append(public)

        await tx.execute(
            """
            INSERT INTO task_checkpoints (
                id, room_id, task_id, participant_id, attachment_id, fence,
                summary, resume_state, seq, created_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?)
            """,
            (
                checkpoint_id,
                room.id,
                command.task_id,
                participant.id,
                executor.attachment_id,
                command.fence,
                command.summary,
                db.dumps(resume.model_dump(mode="json")) if resume is not None else None,
                public.seq,
                now,
            ),
        )

        if resume is not None:
            events.append(
                await eventlog.append(
                    tx,
                    room_id=room.id,
                    type_=EventType.TASK_RESUME_STATE_RECORDED,
                    actor=actor_for(participant),
                    payload={
                        "checkpoint_id": checkpoint_id,
                        "task_id": command.task_id,
                        "resume_state": resume.model_dump(mode="json"),
                    },
                    disclosure=_seat_only_disclosure(participant),
                )
            )
        return CommandOutcome(result={"checkpoint_id": checkpoint_id}, events=events)

    outcome = await execute_command(
        command_id=command.command_id,
        command_type="task.checkpoint",
        room_id=room.id,
        participant_id=participant.id,
        body=body,
    )
    # The id from the *outcome*, not the one generated above: on a replay the body
    # never ran, so the local id names a checkpoint that was never written. Returning
    # it would turn a safe retry into a confusing 404 — the failure this exact line
    # produced the first time it was tested.
    return await load(outcome.result["checkpoint_id"], recipient=participant)


async def append_tx(
    tx: db.Tx,
    *,
    room_id: str,
    task_id: str,
    participant: Participant,
    fence: int,
    summary: str,
    resume_state: ResumeState | None,
    attachment_id: str | None,
) -> list[EventEnvelope]:
    """Write a checkpoint inside someone else's transaction.

    Exists for the one operation that must checkpoint and release atomically: a
    blocking question (D-051). Splitting that into two commands would leave a window
    where the task is parked with no record of where its worker had got to — which is
    the exact state a resume needs and the exact state a crash would destroy.

    Authorization is the caller's job here. This is the primitive, not the command.
    """
    checkpoint_id = ids.new_id(ids.CHECKPOINT)
    now = utcnow_iso()
    public = await eventlog.append(
        tx,
        room_id=room_id,
        type_=EventType.TASK_CHECKPOINTED,
        actor=actor_for(participant),
        payload={
            "checkpoint_id": checkpoint_id,
            "task_id": task_id,
            "participant_id": participant.id,
            "attachment_id": attachment_id,
            "fence": fence,
            "summary": summary,
            "has_resume_state": resume_state is not None,
        },
    )
    await tx.execute(
        """
        INSERT INTO task_checkpoints (
            id, room_id, task_id, participant_id, attachment_id, fence,
            summary, resume_state, seq, created_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?)
        """,
        (
            checkpoint_id,
            room_id,
            task_id,
            participant.id,
            attachment_id,
            fence,
            summary,
            db.dumps(resume_state.model_dump(mode="json")) if resume_state is not None else None,
            public.seq,
            now,
        ),
    )
    events = [public]
    if resume_state is not None:
        events.append(
            await eventlog.append(
                tx,
                room_id=room_id,
                type_=EventType.TASK_RESUME_STATE_RECORDED,
                actor=actor_for(participant),
                payload={
                    "checkpoint_id": checkpoint_id,
                    "task_id": task_id,
                    "resume_state": resume_state.model_dump(mode="json"),
                },
                disclosure=_seat_only_disclosure(participant),
            )
        )
    return events


def _public_disclosure() -> Disclosure:
    return Disclosure(privacy_class=PrivacyClass.ROOM_PUBLIC, audience=Audience.ROOM)


def _seat_only_disclosure(participant: Participant) -> DisclosureDecision:
    """The private half's ruling, built directly rather than negotiated.

    `check_disclosure` resolves an audience a *caller* asked for. Here the server
    decides: a resume bookmark is addressed to the seat that wrote it, and that is
    not a preference a client may widen.
    """
    return DisclosureDecision(
        privacy_class=PrivacyClass.PARTICIPANT_PRIVATE,
        audience=Audience.PARTICIPANT,
        to_participant_id=participant.id,
        restricted_to_participant_ids=[participant.id],
        checks_passed=["server_assigned_seat_only"],
    )


def _to_checkpoint(row: Any, *, include_resume: bool) -> Checkpoint:
    raw = row["resume_state"]
    resume: ResumeState | None = None
    if include_resume and raw:
        resume = ResumeState.model_validate(db.loads(raw, {}))
    return Checkpoint(
        id=row["id"],
        room_id=row["room_id"],
        task_id=row["task_id"],
        participant_id=row["participant_id"],
        attachment_id=row["attachment_id"],
        fence=int(row["fence"]),
        summary=row["summary"],
        resume_state=resume,
        seq=int(row["seq"]),
        created_at=row["created_at"],
    )


def _may_see_resume(row: Any, recipient: Participant | None) -> bool:
    """Only the seat that wrote it — or an admin exercising the audit right.

    The admin clause is not a convenience. It mirrors `privacy.visible_to`, and a
    projection that were *stricter* than the event filter would be worse than
    useless: the same bytes would be readable from the log and hidden from the view,
    so anyone reasoning about what an admin can see would be reasoning about the
    wrong one of two answers.
    """
    if recipient is None:
        return False
    if row["participant_id"] == recipient.id:
        return True
    return recipient.has(Scope.ROOM_ADMIN) and recipient.room_id == row["room_id"]


async def load(checkpoint_id: str, *, recipient: Participant | None = None) -> Checkpoint:
    row = await db.fetch_one("SELECT * FROM task_checkpoints WHERE id = ?", (checkpoint_id,))
    if row is None:
        raise NotFound("Checkpoint does not exist.", checkpoint_id=checkpoint_id)
    return _to_checkpoint(row, include_resume=_may_see_resume(row, recipient))


async def latest_for_task(
    task_id: str,
    *,
    recipient: Participant | None = None,
    limit: int = DEFAULT_LATEST,
    tx: db.Tx | None = None,
) -> list[Checkpoint]:
    """The most recent checkpoints, oldest-first within the window.

    Newest-first from the database so the *latest* N are the ones kept, then
    reversed so a reader gets them in the order they happened. Both halves matter:
    truncating from the wrong end returns ancient history, and returning it backwards
    makes a progress record read as a countdown.
    """
    limit = max(1, min(int(limit), MAX_PAGE))
    sql = "SELECT * FROM task_checkpoints WHERE task_id = ? ORDER BY seq DESC LIMIT ?"
    rows = await (
        tx.fetch_all(sql, (task_id, limit)) if tx else db.fetch_all(sql, (task_id, limit))
    )
    out = [_to_checkpoint(r, include_resume=_may_see_resume(r, recipient)) for r in reversed(rows)]
    return out


async def count_for_task(task_id: str, *, tx: db.Tx | None = None) -> int:
    """So a truncated list can say it truncated (D-043)."""
    sql = "SELECT COUNT(*) FROM task_checkpoints WHERE task_id = ?"
    value = await (tx.fetch_value(sql, (task_id,)) if tx else db.fetch_value(sql, (task_id,)))
    return int(value or 0)
