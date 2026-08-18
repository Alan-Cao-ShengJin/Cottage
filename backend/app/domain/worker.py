"""Workers: downstream execution a supervisor is accountable for (D-077, D-088).

A worker is **not a participant.** D-077 settled that: downstream workers stay outside
the room boundary and receive their briefs through their supervisor, and a worker that
does connect as its own seat is a participant like any other agent rather than a
worker record. Three reasons that boundary holds here:

* Membership has exactly one entry path - redeeming an invitation. A supervisor that
  could mint participants would be minting membership.
* One provisioned companion would otherwise show the room N seats, which breaks
  presence grading, executor affinity and every "who holds this" answer.
* A worker's authority is its supervisor's. Giving it a seat would give it scopes,
  and nothing about bounded execution needs them.

So a worker is a **declared record**: the supervisor's own account of an executor it
owns, recorded so the room can answer who created it, what it is for, which goal
version caused it, and what happened to its output. Nothing here is verified, and
nothing here is presence - a `WorkerState` is the supervisor's last claim, not an
observation, and it must never be rendered as liveness (principle 5). Where a worker
*is* a durable runtime of the supervisor's own seat, `attachment_id` points at it and
liveness comes from `core.presence` as it does for anything else.
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, ConfigDict, Field


class WorkerState(str, Enum):
    """The supervisor's last claim about one worker.

    Declared, never observed. A worker that dies silently stays `WORKING` in this
    field until its supervisor notices - which is exactly why the room shows the
    supervisor's `last_activity_at` beside it rather than presenting the state alone.
    """

    #: Spawn requested; the process may not exist yet.
    STARTING = "starting"
    WORKING = "working"
    #: Blocked on a named dependency, an answer, or a review.
    WAITING = "waiting"
    #: Finished its assignment and returned a result. Awaiting supervisor review.
    COMPLETED = "completed"
    #: Ended without a usable result. `result_reference` holds the evidence.
    FAILED = "failed"
    #: Stop requested; the supervisor has not yet confirmed it ended.
    STOPPING = "stopping"
    STOPPED = "stopped"


#: States where the worker is no longer consuming a concurrency slot.
TERMINAL_WORKER_STATES: frozenset[WorkerState] = frozenset(
    {WorkerState.COMPLETED, WorkerState.FAILED, WorkerState.STOPPED}
)

#: States that occupy one of the supervisor's declared slots.
ACTIVE_WORKER_STATES: frozenset[WorkerState] = frozenset(
    {WorkerState.STARTING, WorkerState.WORKING, WorkerState.WAITING, WorkerState.STOPPING}
)


class WorkerProvenance(str, Enum):
    #: A durable runtime of the supervisor's own seat, visible to the room through
    #: `attachments`. Its liveness is derived, not declared.
    ROOM_ATTACHMENT = "room_attachment"
    #: Lives behind the supervisor. Cottage has never seen it; everything recorded
    #: about it is the supervisor's account (D-054's rule, applied one level down).
    DECLARED = "declared"


class Worker(BaseModel):
    """One downstream executor, owned by exactly one supervisor seat."""

    model_config = ConfigDict(extra="forbid")

    id: str
    room_id: str
    #: The accountable seat. A worker never acts on the room directly: its supervisor
    #: reports for it, reviews it, and answers for its output.
    supervisor_participant_id: str
    #: Which runtime of that seat spawned it, so a restarted supervisor can tell its
    #: own workers from a previous run's.
    supervisor_attachment_id: str | None = None
    #: Stable and supervisor-chosen, so re-declaring the same worker lands on the same
    #: row instead of minting a second identity - the `attachments.label` rule.
    label: str
    display_name: str = ""
    provenance: WorkerProvenance = WorkerProvenance.DECLARED
    #: Set only for `room_attachment` provenance.
    attachment_id: str | None = None

    #: THE ASSIGNMENT. Bounded, and recorded so the room can say what this worker was
    #: for even after it is gone.
    assignment: str = ""
    related_job_id: str | None = None
    related_task_id: str | None = None
    related_work_id: str | None = None
    #: The goal version that caused this worker to exist. Output from a worker spawned
    #: under an older version keeps that provenance, which is what stops stale work
    #: from completing a newer goal.
    created_by_goal_version: int | None = None

    #: DECLARED RUNTIME DETAIL. Self-reported; nothing branches on it (D-054).
    declared_runtime: str = ""
    declared_model: str = ""

    state: WorkerState = WorkerState.STARTING
    #: The supervisor's own account of what this worker is doing now.
    summary: str = ""
    #: Why it is waiting, when it is. Required by the same rule that requires it of a
    #: runtime's `waiting` posture.
    waiting_reason: str = ""
    #: Where the result lives: a checkpoint id, an artifact version, a task id.
    result_reference: str = ""
    #: Retries the supervisor has already spent on this assignment.
    attempts: int = Field(default=0, ge=0)

    created_at: str
    started_at: str | None = None
    #: The supervisor's last claim, not an observation. Never rendered as presence.
    last_activity_at: str | None = None
    completed_at: str | None = None
    retired_at: str | None = None

    @property
    def is_active(self) -> bool:
        return self.retired_at is None and self.state in ACTIVE_WORKER_STATES


class SupervisorCapacity(str, Enum):
    """How much more this supervisor can take on - a first-class allocation signal.

    Deliberately not a raw count. "Two workers running" says nothing about whether a
    third would help: the supervisor may be blocked on a dependency, its host may be
    saturated, or its goal may forbid parallelism. So the supervisor publishes a
    judgement and the room publishes the counts beside it, and the orchestrator can
    see both.

    `OFFLINE` is never declared. It is derived from connection liveness, because a
    runtime that has stopped beating cannot be trusted to tell you it is gone.
    """

    AVAILABLE = "available"
    PARTIALLY_ALLOCATED = "partially_allocated"
    FULLY_ALLOCATED = "fully_allocated"
    #: Cannot make progress on what it holds, whatever its slot count says.
    BLOCKED = "blocked"
    OFFLINE = "offline"


class CapacityReport(BaseModel):
    """A supervisor's declared capacity, plus the counts the room derived itself."""

    model_config = ConfigDict(extra="forbid")

    room_id: str
    supervisor_participant_id: str
    #: What the supervisor says. Clamped by the room to OFFLINE when its presence says
    #: otherwise, so a stale declaration cannot advertise availability.
    declared: SupervisorCapacity = SupervisorCapacity.AVAILABLE
    max_concurrent_workers: int = Field(default=1, ge=0)
    #: Free text: why it is blocked, or what it is waiting for.
    note: str = ""
    declared_at: str | None = None

    #: DERIVED - counted by the room from its own rows, never accepted from a caller.
    active_workers: int = Field(default=0, ge=0)
    owned_jobs: int = Field(default=0, ge=0)
    blocked_workers: int = Field(default=0, ge=0)
    #: The published value: `declared`, overridden to OFFLINE when presence disagrees.
    effective: SupervisorCapacity = SupervisorCapacity.AVAILABLE

    @property
    def free_slots(self) -> int:
        return max(0, self.max_concurrent_workers - self.active_workers)
