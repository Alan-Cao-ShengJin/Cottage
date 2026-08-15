"""Worker → human, which the control plane cannot express by construction (D-051).

Directives run one way on purpose. Issuing one requires `room.admin` precisely so a
worker cannot manufacture instructions — so the symmetrical-looking move, "a
question is a directive with the target and issuer swapped", would hand every worker
the authority that scope check exists to withhold. A question is therefore its own
primitive with its own, much weaker, authority model: whoever may speak in the room
may ask one, because asking commands nobody.

Answering is likewise its own act rather than an `input` directive. Routing replies
through the control plane would mean only room admins could ever unblock a worker,
which turns an ordinary conversation into an administrative privilege.

**Blocking is opt-in and costs the asker its lease.** By default nothing changes:
the worker keeps its claim and carries on with everything else, because a worker
that halts on every uncertainty cannot work unattended, which is the entire reason
to have one. When it genuinely cannot proceed it says so, and the room does three
things in one transaction — checkpoint, park the task as `waiting_input`, release
the claim. All three together or none: a task parked with no record of where its
worker had got to is precisely the state a resume needs and a crash would destroy.
"""

from __future__ import annotations

import logging
from typing import Any

from ..db import database as db
from ..domain import ids
from ..domain.commands import AnswerQuestionCommand, AskQuestionCommand
from ..domain.disclosure import Audience, Disclosure
from ..domain.events import EventEnvelope, EventType
from ..domain.question import Answer, Question
from ..domain.room import Participant, PrivacyClass, Scope
from ..domain.task import TaskStatus
from ..util import utcnow_iso
from . import authz, checkpoints, eventlog, presence, privacy, store, tasks
from .actors import actor_for
from .dispatch import CommandOutcome, execute_command
from .errors import Forbidden, InvalidCommand, NotFound

log = logging.getLogger(__name__)

#: Enough for "what is waiting on me" without becoming a second inbox.
MAX_OPEN_QUESTIONS = 25


def _same_runtime(asked_by: str | None, answering: str | None) -> bool:
    """Whether the answer is coming from the runtime that asked (D-055).

    The refusal is scoped to the **runtime**, not the seat. Scoped to the seat it
    also blocked the person who most obviously should answer: a human's chat surface
    and their companion worker are one participant, so "you cannot answer your own
    question" meant the human could not answer their own worker. Found by running it.

    Where either side is unidentifiable the answer is *false* — permit rather than
    refuse. An unknown runtime is not evidence of self-answering, and refusing on an
    absence would make the check fire hardest against clients that declare least,
    which is the opposite of the incentive this project wants.

    This is a weaker guarantee than it looks, and that is worth stating: a worker
    determined to unblock itself could attach a second runtime. It gains nothing by
    doing so — it could simply never have blocked — and the attempt is recorded, with
    the answering runtime stamped on the event. Attribution, not prevention (D-025).
    """
    return bool(asked_by) and bool(answering) and asked_by == answering


async def ask(*, participant: Participant, command: AskQuestionCommand) -> Question:
    """Ask something, optionally standing down from the work until it is answered."""
    room = await store.load_room(participant.room_id)
    # Speaking, not commanding. A question carries no authority, which is exactly why
    # it needs no grant beyond the one that lets this participant say anything at all.
    authz.require_scope(participant, Scope.MESSAGE_POST)
    authz.require_writable(room)

    if command.blocking and command.task_id is None:
        raise InvalidCommand(
            "A blocking question needs a task. Blocking means this work cannot "
            "proceed, and a task is the only thing the room knows how to park.",
        )
    if command.blocking and command.fence is None:
        raise InvalidCommand(
            "A blocking question releases your lease, so it must present the fence "
            "you hold — otherwise a runtime whose lease already moved on could park "
            "work it no longer has.",
            task_id=command.task_id,
        )

    target = None
    if command.to_participant_id is not None:
        target = await store.load_participant(command.to_participant_id)
        if target.room_id != room.id:
            raise NotFound(
                "That participant is not in this room.",
                to_participant_id=command.to_participant_id,
            )

    # Room-public even when addressed: addressing says who is *expected* to answer.
    # Restricting the body would mean an unanswered question is invisible to the one
    # person who happened to know, which is how questions go stale.
    decision = privacy.check_disclosure(
        room=room,
        participant=participant,
        disclosure=Disclosure(privacy_class=PrivacyClass.ROOM_PUBLIC, audience=Audience.ROOM),
        content=[command.body, command.checkpoint_summary],
    )

    question_id = ids.new_id(ids.QUESTION)
    now = utcnow_iso()

    async def body(tx: db.Tx) -> CommandOutcome:
        events: list[EventEnvelope] = []
        asking = await tasks.caller_executor(participant, command.connection_id, tx=tx)
        if command.blocking:
            events += await _park_task_tx(
                tx,
                room_id=room.id,
                participant=participant,
                command=command,
                question_id=question_id,
            )

        asked = await eventlog.append(
            tx,
            room_id=room.id,
            type_=EventType.QUESTION_ASKED,
            actor=actor_for(participant),
            payload={
                "question_id": question_id,
                "task_id": command.task_id,
                "to_participant_id": command.to_participant_id,
                "body": command.body,
                "blocking": command.blocking,
            },
            disclosure=decision,
            causation_id=command.command_id,
        )
        events.append(asked)

        await tx.execute(
            """
            INSERT INTO questions (
                id, room_id, task_id, asked_by_participant_id, asked_by_attachment_id,
                to_participant_id, body, blocking, created_seq, created_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?)
            """,
            (
                question_id,
                room.id,
                command.task_id,
                participant.id,
                asking.ref,
                command.to_participant_id,
                command.body,
                1 if command.blocking else 0,
                asked.seq,
                now,
            ),
        )
        return CommandOutcome(result={"question_id": question_id}, events=events)

    await execute_command(
        command_id=command.command_id,
        command_type="question.ask",
        room_id=room.id,
        participant_id=participant.id,
        body=body,
    )
    return await load(question_id)


async def _park_task_tx(
    tx: db.Tx,
    *,
    room_id: str,
    participant: Participant,
    command: AskQuestionCommand,
    question_id: str,
) -> list[EventEnvelope]:
    """Checkpoint, park, release — atomically, in that order.

    The order is not cosmetic. The checkpoint is written while the lease is still
    held, so the record of where the work got to is made by the runtime that has the
    right to make it. Releasing first would mean writing history about a task you no
    longer hold.

    `fence` is deliberately not reset, exactly as for a stop (D-045): the parked
    runtime's fence stays unusable so a late write from it cannot land after someone
    else picks the work up.
    """
    assert command.task_id is not None and command.fence is not None
    row = await tx.fetch_one(
        "SELECT * FROM tasks WHERE id = ? AND room_id = ?", (command.task_id, room_id)
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
        action="park on a blocking question",
    )

    executor = await presence.executor_of(row, tx=tx)
    summary = command.checkpoint_summary or (
        f"Standing down to ask: {command.body[:200]}"
        if len(command.body) > 200
        else f"Standing down to ask: {command.body}"
    )
    events = await checkpoints.append_tx(
        tx,
        room_id=room_id,
        task_id=command.task_id,
        participant=participant,
        fence=command.fence,
        summary=summary,
        resume_state=command.resume_state,
        attachment_id=executor.attachment_id,
    )

    now = utcnow_iso()
    affected = await tx.execute(
        """
        UPDATE tasks
        SET status = ?, claim_lease_id = NULL, claim_participant_id = NULL,
            claim_fence = NULL, claim_claimed_at = NULL, claim_expires_at = NULL,
            claim_heartbeat_interval_s = NULL, claim_renewed_at = NULL,
            executor_attachment_id = NULL, executor_connection_id = NULL,
            updated_at = ?
        WHERE id = ? AND claim_lease_id = ?
        """,
        (TaskStatus.WAITING_INPUT.value, now, command.task_id, row["claim_lease_id"]),
    )
    if affected == 0:
        # Somebody else's write landed between the read and here. Engine-neutral by
        # construction: the row count is the arbiter, never a lock (ADR-009).
        raise InvalidCommand(
            "This task changed while you were standing down; re-read it before asking.",
            task_id=command.task_id,
        )

    from . import work as work_service

    # The work card must not outlive the work. This is the same defect the first live
    # stop exposed — a board asserting activity that had already ceased (D-049).
    events += await work_service.end_for_task_tx(
        tx,
        room=await store.load_room(room_id, tx=tx),
        task_id=command.task_id,
        actor=participant,
        reason="waiting on a blocking question",
    )
    events.append(
        await eventlog.append(
            tx,
            room_id=room_id,
            type_=EventType.TASK_AWAITING_INPUT,
            actor=actor_for(participant),
            payload={
                "task_id": command.task_id,
                "question_id": question_id,
                "participant_id": participant.id,
                "fence": command.fence,
                "released": True,
            },
        )
    )
    return events


async def answer(*, participant: Participant, command: AnswerQuestionCommand) -> Answer:
    """Reply, and release whatever the question parked."""
    room = await store.load_room(participant.room_id)
    authz.require_scope(participant, Scope.MESSAGE_POST)
    authz.require_writable(room)

    decision = privacy.check_disclosure(
        room=room,
        participant=participant,
        disclosure=Disclosure(privacy_class=PrivacyClass.ROOM_PUBLIC, audience=Audience.ROOM),
        content=[command.body],
    )

    answer_id = ids.new_id(ids.ANSWER)
    now = utcnow_iso()

    async def body(tx: db.Tx) -> CommandOutcome:
        row = await tx.fetch_one(
            "SELECT * FROM questions WHERE id = ? AND room_id = ?",
            (command.question_id, room.id),
        )
        if row is None:
            raise NotFound("Question does not exist.", question_id=command.question_id)
        answering = await tasks.caller_executor(participant, command.connection_id, tx=tx)
        same_seat = row["asked_by_participant_id"] == participant.id
        if same_seat and _same_runtime(row["asked_by_attachment_id"], answering.ref):
            raise Forbidden(
                "A runtime cannot answer its own question. Standing down and then "
                "telling yourself to carry on is not waiting for anybody — it is a "
                "pause you can end whenever you like. Another runtime of this seat "
                "may answer, and so may anyone else in the room.",
                question_id=command.question_id,
            )
        if row["answered_at"]:
            raise InvalidCommand(
                "That question is already answered.",
                question_id=command.question_id,
                answered_by_participant_id=row["answered_by_participant_id"],
            )

        events: list[EventEnvelope] = []
        answered = await eventlog.append(
            tx,
            room_id=room.id,
            type_=EventType.QUESTION_ANSWERED,
            actor=actor_for(participant),
            payload={
                "question_id": command.question_id,
                "answer_id": answer_id,
                "task_id": row["task_id"],
                "asked_by_participant_id": row["asked_by_participant_id"],
                "body": command.body,
                "asked_at_seq": int(row["created_seq"]),
                # Stated rather than hidden. An answer from the asker's own seat is
                # legitimate — a human's control surface answering their own worker is
                # the ordinary case — but a reader deciding how much independent input
                # a worker actually received needs to know which it was (D-055).
                "same_seat": same_seat,
                "answered_by_attachment_id": answering.ref,
            },
            disclosure=decision,
            causation_id=command.command_id,
        )
        events.append(answered)

        await tx.execute(
            """
            INSERT INTO answers (
                id, room_id, question_id, answered_by_participant_id, body, seq, created_at
            ) VALUES (?,?,?,?,?,?,?)
            """,
            (
                answer_id,
                room.id,
                command.question_id,
                participant.id,
                command.body,
                answered.seq,
                now,
            ),
        )
        updated = await tx.execute(
            """
            UPDATE questions
            SET answered_at = ?, answered_by_participant_id = ?, answer_id = ?,
                answered_by_attachment_id = ?
            WHERE id = ? AND answered_at IS NULL
            """,
            (now, participant.id, answer_id, answering.ref, command.question_id),
        )
        if updated == 0:
            raise InvalidCommand(
                "That question was answered while you were replying.",
                question_id=command.question_id,
            )

        if row["blocking"] and row["task_id"]:
            events += await _unpark_task_tx(
                tx,
                room_id=room.id,
                task_id=row["task_id"],
                participant=participant,
                question_id=command.question_id,
            )
        return CommandOutcome(result={"answer_id": answer_id}, events=events)

    await execute_command(
        command_id=command.command_id,
        command_type="question.answer",
        room_id=room.id,
        participant_id=participant.id,
        body=body,
    )
    return await load_answer(answer_id)


async def _unpark_task_tx(
    tx: db.Tx, *, room_id: str, task_id: str, participant: Participant, question_id: str
) -> list[EventEnvelope]:
    """Return a parked task to `open` so its worker can claim it again.

    Back to `open` rather than straight back to its old holder. The worker may have
    died while waiting, and handing a lease to a runtime that is not there would
    reproduce exactly the stuck-work failure leases exist to avoid. It re-claims
    through the normal path, which also means someone better placed may take it —
    the answer is now in the room, so the next claimant is not blind.
    """
    affected = await tx.execute(
        "UPDATE tasks SET status = 'open', updated_at = ? WHERE id = ? AND status = ?",
        (utcnow_iso(), task_id, TaskStatus.WAITING_INPUT.value),
    )
    if affected == 0:
        # Already moved on — cancelled, or another answer unparked it. Answering
        # stays valid; only the side effect is skipped.
        return []
    return [
        await eventlog.append(
            tx,
            room_id=room_id,
            type_=EventType.TASK_UNBLOCKED,
            actor=actor_for(participant),
            payload={
                "task_id": task_id,
                "note": "the blocking question was answered",
                "question_id": question_id,
            },
        )
    ]


async def load(question_id: str) -> Question:
    row = await db.fetch_one("SELECT * FROM questions WHERE id = ?", (question_id,))
    if row is None:
        raise NotFound("Question does not exist.", question_id=question_id)
    return store.to_question(row)


async def load_answer(answer_id: str) -> Answer:
    row = await db.fetch_one("SELECT * FROM answers WHERE id = ?", (answer_id,))
    if row is None:
        raise NotFound("Answer does not exist.", answer_id=answer_id)
    return store.to_answer(row)


async def open_for(participant_id: str, *, room_id: str, tx: db.Tx | None = None) -> list[Question]:
    """Unanswered questions this participant should act on or is waiting on.

    Both directions in one list on purpose. A resuming runtime needs "what am I
    blocked on" and a human needs "what is waiting on me", and splitting them into
    two projections is how one of them ends up never being read.
    """
    sql = (
        "SELECT * FROM questions WHERE room_id = ? AND answered_at IS NULL "
        "AND (to_participant_id = ? OR to_participant_id IS NULL "
        "     OR asked_by_participant_id = ?) "
        "ORDER BY created_seq ASC LIMIT ?"
    )
    args = (room_id, participant_id, participant_id, MAX_OPEN_QUESTIONS)
    rows = await (tx.fetch_all(sql, args) if tx else db.fetch_all(sql, args))
    return [store.to_question(r) for r in rows]


async def answers_for(
    participant_id: str, *, room_id: str, tx: db.Tx | None = None
) -> list[dict[str, Any]]:
    """Answers to questions this participant asked, newest last.

    Exists because a runtime that restarts must still find the reply to a question
    it asked before it died. Reading answers off the event stream alone cannot do
    that — a fresh process starts at the current cursor, so the one event it most
    needs is the one already behind it — and a projection of *open* questions loses
    the answer at the exact moment it arrives. So the resume path carries the answer
    itself.
    """
    sql = (
        "SELECT a.id AS answer_id, a.body AS body, a.seq AS seq, "
        "       q.id AS question_id, q.task_id AS task_id, q.body AS question "
        "FROM answers a JOIN questions q ON q.id = a.question_id "
        "WHERE a.room_id = ? AND q.asked_by_participant_id = ? "
        "ORDER BY a.seq DESC LIMIT ?"
    )
    args = (room_id, participant_id, MAX_OPEN_QUESTIONS)
    rows = await (tx.fetch_all(sql, args) if tx else db.fetch_all(sql, args))
    return [
        {
            "answer_id": r["answer_id"],
            "question_id": r["question_id"],
            "task_id": r["task_id"],
            "question": r["question"],
            "body": r["body"],
            "seq": int(r["seq"]),
        }
        for r in reversed(rows)
    ]


async def open_for_task(task_id: str, *, tx: db.Tx | None = None) -> list[Question]:
    sql = (
        "SELECT * FROM questions WHERE task_id = ? AND answered_at IS NULL "
        "ORDER BY created_seq ASC LIMIT ?"
    )
    rows = await (
        tx.fetch_all(sql, (task_id, MAX_OPEN_QUESTIONS))
        if tx
        else db.fetch_all(sql, (task_id, MAX_OPEN_QUESTIONS))
    )
    return [store.to_question(r) for r in rows]
