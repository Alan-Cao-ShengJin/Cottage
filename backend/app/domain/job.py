"""The job board: durable human intent, and where it went (D-088).

A **job** is what a human asked for, recorded so that it cannot quietly disappear. A
**task** is the lease-bearing unit of execution that eventually does it. They are
separate types on purpose, and the separation is the point of the board:

* A task's `status` is normalised on read — an expired claim reads back as `open` —
  and its lifecycle is about *who holds it right now*. Assignment history, human
  provenance and supersession have nowhere to live there.
* A job outlives any number of tasks. It can be decomposed, reassigned between
  supervisors, superseded by a better formulation, or rejected with a reason, and each
  of those must remain readable afterwards.

So `tasks` keeps the fence, the lease and the executor — the room must never have two
answers to "who holds this" — and `jobs` keeps intent, provenance and allocation.

**A supervisor does not own the work merely because its human asked.** A request
arrives, the supervisor records it as a job, and the orchestrator decides who executes
it against room priorities and supervisor capacity. That indirection is what stops a
room becoming N independent queues that happen to share a log.

A job stays represented until it reaches a terminal state *with an attributable
reason*. There is no path that removes one.
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, ConfigDict, Field

from .room import PrivacyClass


class JobState(str, Enum):
    """Where a job is on the board.

    `posted` and `assigned` are separated because "the room knows this is wanted" and
    "a named seat is accountable for it" are different facts, and the gap between them
    is exactly what the orchestrator works in.
    """

    #: On the board, unowned. The orchestrator has not allocated it yet.
    POSTED = "posted"
    #: Allocated to a supervisor. Not yet acknowledged by it.
    ASSIGNED = "assigned"
    #: The assigned supervisor accepted ownership.
    ACCEPTED = "accepted"
    #: An execution task exists and is being worked.
    ACTIVE = "active"
    #: Paused by the orchestrator; still owned, still on the board.
    PAUSED = "paused"
    #: Waiting on a named dependency or an unanswered question.
    BLOCKED = "blocked"
    #: The owning supervisor reviewed the work and reported it done.
    COMPLETED = "completed"
    #: Called off. The reason is on the event and on the row.
    CANCELLED = "cancelled"
    #: Replaced by a better formulation, which is named.
    SUPERSEDED = "superseded"
    #: Refused, with a reason. Refusal is an outcome, not a deletion.
    REJECTED = "rejected"


#: States from which no further work will be done. A job in one of these keeps its
#: history and its reason forever; nothing prunes it.
TERMINAL_JOB_STATES: frozenset[JobState] = frozenset(
    {JobState.COMPLETED, JobState.CANCELLED, JobState.SUPERSEDED, JobState.REJECTED}
)

#: States where a named supervisor is accountable.
OWNED_JOB_STATES: frozenset[JobState] = frozenset(
    {JobState.ASSIGNED, JobState.ACCEPTED, JobState.ACTIVE, JobState.PAUSED, JobState.BLOCKED}
)


class JobOrigin(str, Enum):
    """What caused this job to exist.

    `human_steer` is the case the board exists for and the reason `human_instruction`
    is stored verbatim: a normalised outcome is what the room coordinates against, but
    the words the person actually used are what an argument about intent is settled
    with, and a paraphrase cannot be un-paraphrased later.
    """

    HUMAN_STEER = "human_steer"
    #: A supervisor or the orchestrator identified work the room needs.
    AGENT_PROPOSAL = "agent_proposal"
    #: Split out of another job. `parent_job_id` names it.
    DECOMPOSITION = "decomposition"
    #: Written by the one-time backfill for rooms that predate D-088.
    MIGRATION = "migration"


class Job(BaseModel):
    """A durable board entry. Free-form fields are disclosure-checked like any other
    room content — the shape of this model prevents no exfiltration on its own."""

    model_config = ConfigDict(extra="forbid")

    id: str
    room_id: str
    title: str
    #: The normalised outcome: what the room would observe if this were done.
    desired_outcome: str = ""
    #: The human's own words, kept unedited. Empty for agent-proposed jobs.
    human_instruction: str = ""
    #: How this serves the room charter, in the poster's words.
    room_goal_relationship: str = ""
    constraints: tuple[str, ...] = ()
    acceptance_criteria: tuple[str, ...] = ()
    #: Opaque scoped identifiers — repo paths, service names, ticket ids. Shares the
    #: namespace with `Task.targets` and `WorkDeclaration.targets`, which is what
    #: makes overlap detectable across jobs, tasks and live work.
    targets: tuple[str, ...] = ()
    #: Urgency as *requested*. `priority` is what the orchestrator decided; keeping
    #: both is what lets a supervisor see that its urgent request was ranked below
    #: something else, rather than silently ignored.
    requested_urgency: int = 0
    priority: int = 0
    state: JobState = JobState.POSTED
    origin: JobOrigin = JobOrigin.HUMAN_STEER

    #: PROVENANCE. Which seat posted it, on whose behalf, and under which goal.
    posted_by_participant_id: str
    #: The human participant whose instruction this represents, when there is one.
    on_behalf_of_participant_id: str | None = None
    #: The goal version the poster was operating under. A job outlives the wording
    #: it came from, so this is recorded rather than joined.
    source_goal_id: str | None = None
    source_goal_version: int | None = None
    parent_job_id: str | None = None

    #: ALLOCATION.
    assigned_to_participant_id: str | None = None
    assigned_by_participant_id: str | None = None
    assigned_at: str | None = None
    accepted_at: str | None = None
    #: The goal version through which the assignment was delivered, so a supervisor's
    #: output can be attributed to the direction it was given.
    assigned_goal_version: int | None = None
    #: The lease-bearing task, once this is execution rather than a listing.
    task_id: str | None = None

    #: TERMINATION. Always both, or neither.
    terminal_reason: str = ""
    terminated_by_participant_id: str | None = None
    superseded_by_job_id: str | None = None

    privacy_class: PrivacyClass = PrivacyClass.ROOM_PUBLIC
    created_at: str
    updated_at: str
    closed_at: str | None = None
    #: Append-only history of every state transition, oldest first.
    history: tuple[JobEvent, ...] = ()

    @property
    def is_terminal(self) -> bool:
        return self.state in TERMINAL_JOB_STATES

    @property
    def is_owned(self) -> bool:
        return self.state in OWNED_JOB_STATES and self.assigned_to_participant_id is not None


class JobEvent(BaseModel):
    """One recorded transition of a job. Written once, never edited.

    The event log is the source of truth for all of this; these rows exist so the
    board can answer "how did this job get here" without replaying a room.
    """

    model_config = ConfigDict(extra="forbid")

    job_id: str
    room_id: str
    #: Monotonic per job, starting at 1.
    ordinal: int = Field(ge=1)
    from_state: JobState | None = None
    to_state: JobState
    actor_participant_id: str | None = None
    reason: str = ""
    #: The room `seq` of the event that recorded this transition.
    seq: int = Field(ge=0)
    created_at: str


Job.model_rebuild()
