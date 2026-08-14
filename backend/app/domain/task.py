"""Task graph, claims-as-leases, and conflicts.

The lease is the load-bearing mechanism (`docs/PROTOCOL.md` §4, ADR-004). Two
properties matter and both are tested as invariants:

* **At most one valid claim per task.** Enforced by a conditional write, not by a
  process-level lock, so the guarantee survives a swap of storage engine.
* **A stale claimant cannot mutate.** Every mutation of a claimed task must present
  the current `fence`, which is monotonic per task and never reused. A TTL alone
  cannot do this: a process that lost its lease and then woke up would still hold a
  plausible-looking `lease_id`.
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field

from .room import PrivacyClass


class TaskStatus(str, Enum):
    #: Offered to a specific participant, not yet accepted.
    PROPOSED = "proposed"
    #: Available for anyone with `task.claim` to take.
    OPEN = "open"
    CLAIMED = "claimed"
    IN_PROGRESS = "in_progress"
    BLOCKED = "blocked"
    DONE = "done"
    CANCELLED = "cancelled"


TERMINAL_TASK_STATUSES: frozenset[TaskStatus] = frozenset({TaskStatus.DONE, TaskStatus.CANCELLED})

#: Statuses in which the task is held by a claimant.
HELD_TASK_STATUSES: frozenset[TaskStatus] = frozenset(
    {TaskStatus.CLAIMED, TaskStatus.IN_PROGRESS, TaskStatus.BLOCKED}
)


class DependencyKind(str, Enum):
    BLOCKS = "blocks"
    RELATES = "relates"
    DUPLICATES = "duplicates"


class TaskClaim(BaseModel):
    """An exclusive, expiring lease on a task."""

    lease_id: str
    participant_id: str
    #: Monotonic per task, never reused. The anti-zombie token.
    fence: int
    claimed_at: str
    expires_at: str
    heartbeat_interval_s: int
    renewed_at: str | None = None


class Task(BaseModel):
    id: str
    room_id: str
    title: str
    description: str = ""
    status: TaskStatus
    #: Same namespace as `WorkDeclaration.targets`; drives duplicate/overlap checks.
    targets: list[str] = Field(default_factory=list)
    priority: int = 0
    created_by_participant_id: str
    #: Monotonic claim counter for this task. Persisted even when unclaimed, so a
    #: fence value is never reissued after a release.
    fence: int = 0
    claim: TaskClaim | None = None
    result: str = ""
    privacy_class: PrivacyClass = PrivacyClass.ROOM_PUBLIC
    created_at: str
    updated_at: str
    completed_at: str | None = None

    @property
    def is_held(self) -> bool:
        return self.claim is not None and self.status in HELD_TASK_STATUSES


class TaskDependency(BaseModel):
    room_id: str
    from_task_id: str
    to_task_id: str
    kind: DependencyKind
    created_at: str
    created_by_participant_id: str


class ProposalResolution(str, Enum):
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    DELEGATED = "delegated"
    EXPIRED = "expired"


class TaskProposal(BaseModel):
    """An offer of a task to a specific participant.

    The room proposes; the participant decides. Delegation records the onward
    target and creates a new proposal, so the chain stays auditable.
    """

    id: str
    room_id: str
    task_id: str
    to_participant_id: str
    proposed_by_participant_id: str
    note: str = ""
    created_at: str
    expires_at: str | None = None
    resolution: ProposalResolution | None = None
    resolved_at: str | None = None
    delegated_to_participant_id: str | None = None
    #: The proposal this one was delegated from, if any.
    delegated_from_proposal_id: str | None = None


class ConflictKind(str, Enum):
    DUPLICATE_TASK = "duplicate_task"
    OVERLAPPING_WORK = "overlapping_work"
    CLAIM_RACE = "claim_race"
    STATE_CAS_FAILURE = "state_cas_failure"
    ARTIFACT_DIVERGENCE = "artifact_divergence"


class ConflictStatus(str, Enum):
    OPEN = "open"
    RESOLVED = "resolved"
    DISMISSED = "dismissed"


class Conflict(BaseModel):
    """An explicit record that two contributions collided.

    Conflicts are advisory: the room surfaces them and never silently resolves
    them. Losing a race is not an error condition to be hidden — it is information
    the participants need.
    """

    id: str
    room_id: str
    kind: ConflictKind
    status: ConflictStatus = ConflictStatus.OPEN
    #: Entity ids involved, e.g. two task ids or a task id and two participant ids.
    subject_refs: list[str] = Field(default_factory=list)
    participant_ids: list[str] = Field(default_factory=list)
    #: Why the detector fired, in plain language, for the humans reading the board.
    detail: str = ""
    detected_at: str
    resolved_at: str | None = None
    resolution: str = ""
