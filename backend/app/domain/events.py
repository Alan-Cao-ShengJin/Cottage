"""The event type registry and envelope.

The registry is closed and authoritative: adding a type means editing this module
and the table in `docs/PROTOCOL.md` §2 in the same change. A test asserts the two
agree, so the docs cannot drift from the code.
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field

from .disclosure import Audience
from .identity import PrincipalKind
from .room import PrivacyClass

PROTOCOL_VERSION = "arp/1"


class EventType(str, Enum):
    ROOM_CREATED = "room.created"
    ROOM_CLOSED = "room.closed"
    ROOM_EXPIRY_EXTENDED = "room.expiry_extended"
    ROOM_PURGED = "room.purged"
    ROOM_POLICY_CHANGED = "room.policy_changed"
    ROOM_CHARTER_UPDATED = "room.charter_updated"

    INVITATION_CREATED = "invitation.created"
    INVITATION_REVOKED = "invitation.revoked"
    INVITATION_REDEEMED = "invitation.redeemed"

    PARTICIPANT_JOINED = "participant.joined"
    PARTICIPANT_LEFT = "participant.left"
    PARTICIPANT_SCOPES_CHANGED = "participant.scopes_changed"

    #: A seat issued a narrow token to one of its runtimes. The grant is logged;
    #: the token never is, because a credential in the log is a credential in every
    #: replay and export of that log.
    CREDENTIAL_MINTED = "credential.minted"
    CREDENTIAL_REVOKED = "credential.revoked"
    PRESENCE_CHANGED = "presence.changed"
    #: A durable runtime identified itself to the room for the first time. Rare —
    #: once per runtime, not once per connection — but it is a state change, and
    #: state changes are events (principle 1). It is also what lets a reader
    #: distinguish "the worker restarted" from "a second worker appeared".
    ATTACHMENT_REGISTERED = "presence.attachment_registered"

    #: A runtime was told to stop, and the room will refuse its work from here on.
    #: Emitted for the *decision*, not for the process ending — the room never learns
    #: whether the process ended, and a design that waited to find out would be waiting
    #: on a machine it does not own (D-062). `runtime.resumed` is the explicit,
    #: separately-authorized undo; there is deliberately no automatic one.
    RUNTIME_DRAINED = "presence.runtime_drained"
    RUNTIME_RESUMED = "presence.runtime_resumed"
    #: A durable runtime changed its validated work posture. This never asserts
    #: liveness; consumers compose it with connection-derived presence.
    RUNTIME_STATE_CHANGED = "runtime.state_changed"

    MESSAGE_POSTED = "message.posted"

    #: One breadcrumb of live narration from a runtime (D-082). It writes no mutable
    #: projection: the log is the feed and fresh snapshots derive its latest visible
    #: value. Genuine coordination state remains in the events that already model it.
    ACTIVITY_NOTED = "activity.noted"

    WORK_DECLARED = "work.declared"
    WORK_UPDATED = "work.updated"
    WORK_ENDED = "work.ended"
    WORK_STALE = "work.stale"

    TASK_CREATED = "task.created"
    TASK_UPDATED = "task.updated"
    TASK_CANCELLED = "task.cancelled"
    TASK_COMPLETED = "task.completed"
    TASK_PROPOSED = "task.proposed"
    TASK_PROPOSAL_RESOLVED = "task.proposal_resolved"
    TASK_CLAIMED = "task.claimed"
    TASK_CLAIM_RENEWED = "task.claim_renewed"
    TASK_CLAIM_RELEASED = "task.claim_released"
    #: Execution moved between two runtimes of one participant. Distinct from
    #: `task.claimed` because the holder did not change — only who is doing it.
    TASK_EXECUTOR_CHANGED = "task.executor_changed"
    TASK_CLAIM_EXPIRED = "task.claim_expired"
    #: A human paused, stopped, resumed or reprioritised work in flight. The
    #: holder and the executor are unchanged: steering directs, it does not seize.
    TASK_STEERED = "task.steered"
    #: A human directed a participant: control-plane intent, durable and targeted.
    #: Separate from `task.steered`, which is the *effect* on the task — one
    #: directive may produce both, and an incident needs to tell them apart.
    DIRECTIVE_ISSUED = "directive.issued"
    #: The target observed it. Evidence, never permission: the effect already landed.
    DIRECTIVE_ACKNOWLEDGED = "directive.acknowledged"
    #: Durable progress on a task: the room-visible half, carrying the summary and
    #: metadata only. Whether a private bookmark accompanied it is stated as a
    #: boolean, because "there is state you cannot see" is itself public (D-050).
    TASK_CHECKPOINTED = "task.checkpointed"
    #: The same-seat half. An event has exactly one audience, so a record with two
    #: audiences is two events — this one is restricted to the seat that wrote it.
    TASK_RESUME_STATE_RECORDED = "task.resume_state_recorded"
    #: Worker → human. Carries no authority, which is why it needs no admin grant
    #: and why it cannot be a directive with the ends swapped (D-051).
    QUESTION_ASKED = "question.asked"
    QUESTION_ANSWERED = "question.answered"
    #: A blocking question released the lease and parked the task. Distinct from
    #: `task.steered`: nobody steered it, its holder stood down and said why.
    TASK_AWAITING_INPUT = "task.awaiting_input"
    TASK_BLOCKED = "task.blocked"
    TASK_UNBLOCKED = "task.unblocked"

    DEPENDENCY_ADDED = "dependency.added"
    DEPENDENCY_REMOVED = "dependency.removed"

    STATE_SET = "state.set"
    STATE_DELETED = "state.deleted"

    ARTIFACT_VERSION_PUBLISHED = "artifact.version_published"
    ARTIFACT_DIVERGENCE_DETECTED = "artifact.divergence_detected"

    CONFLICT_DETECTED = "conflict.detected"
    CONFLICT_RESOLVED = "conflict.resolved"

    # --- the coordination hierarchy (D-088) -------------------------------
    #: A seat took a position in the room hierarchy. One event covers orchestrator and
    #: supervisor because the fact is the same shape either way, and a reader that
    #: wants "who orchestrates" wants to see promotions and demotions in one stream.
    ROOM_ROLE_ASSIGNED = "participant.room_role_assigned"

    #: Durable human intent reaching the board. `human_instruction` is the person's own
    #: words, kept unedited, because a normalised outcome cannot be un-normalised later.
    JOB_POSTED = "job.posted"
    #: A revision of what the job asks for, or of where it ranks. The payload names the
    #: fields that changed so a reader need not diff two snapshots.
    JOB_UPDATED = "job.updated"
    #: Allocation *and* reallocation. `previous_assignee_participant_id` is what makes
    #: the second case legible, and the orchestrator must state a reason for both.
    JOB_ASSIGNED = "job.assigned"
    #: The owning supervisor took accountability. Distinct from assignment because
    #: "told to own it" and "owns it" are different facts and the gap is diagnostic.
    JOB_ACCEPTED = "job.accepted"
    #: A non-terminal move: active, paused, blocked. Terminal moves are `job.closed`.
    JOB_STATE_CHANGED = "job.state_changed"
    #: The end of a job, with the outcome in `state` and always an attributable
    #: `reason` — completed, cancelled, superseded (naming its replacement) or
    #: rejected. One type rather than four, the same choice `work.ended` makes.
    JOB_CLOSED = "job.closed"

    #: The orchestrator set or wholly replaced a supervisor's active goal. Carries
    #: `previous_version` and `new_version`, so supersession is readable without a
    #: join, and `worker_disposition`, because a replacement silent about work already
    #: in flight is how a superseded goal keeps executing.
    GOAL_REPLACED = "supervisor.goal_replaced"
    #: Evidence the target supervisor observed a version. Never permission for the
    #: effect — the goal took effect when it was written (ADR-012).
    GOAL_ACKNOWLEDGED = "supervisor.goal_acknowledged"
    #: The goal was met or stood down. `status` and a reason.
    GOAL_CLOSED = "supervisor.goal_closed"
    #: A supervisor's declared capacity changed, or the room re-derived its counts.
    #: `effective` is what the orchestrator allocates against; `declared` is what the
    #: supervisor said, and the two differ when presence contradicts a declaration.
    CAPACITY_CHANGED = "supervisor.capacity_changed"

    #: A supervisor declared a downstream worker it owns. Recorded, never verified: a
    #: worker is not a participant (D-077), and this is the supervisor's account of an
    #: executor the room has never seen.
    WORKER_REGISTERED = "worker.registered"
    #: A declared worker's state moved — starting, working, waiting, stopping. Never
    #: presence: nothing here is observed by the room.
    WORKER_STATE_CHANGED = "worker.state_changed"
    #: A worker ended. `state` is completed, failed or stopped, and
    #: `result_reference` points at the evidence. Completion here is *not* completion
    #: of the job: its supervisor still has to review it.
    WORKER_FINISHED = "worker.finished"


#: Frames the transport may emit that are not log entries. They carry no `seq` of
#: their own and must never be confused with events (`docs/PROTOCOL.md` §5).
class ControlFrame(str, Enum):
    SNAPSHOT = "snapshot"
    RESUME_GAP = "resume_gap"
    KEEPALIVE = "keepalive"
    #: Opening frame for a filtered subscriber, carrying only the cursor it starts from.
    #: A wake channel wants the position without the board: it is not building a view,
    #: so a full snapshot would be the single most expensive frame it ever received and
    #: would arrive before anything had happened.
    READY = "ready"


class EventActor(BaseModel):
    """Who caused this event. `None` participant means the room itself acted —
    a reaper expiring a lease, a janitor closing an expired room."""

    participant_id: str | None = None
    display_name: str = "room"
    kind: PrincipalKind | None = None
    org_id: str | None = None


class EventEnvelope(BaseModel):
    protocol: str = PROTOCOL_VERSION
    room_id: str
    #: Monotonic per room, gapless, allocated in the mutation's transaction.
    seq: int
    id: str
    type: EventType
    ts: str
    actor: EventActor
    privacy_class: PrivacyClass = PrivacyClass.ROOM_PUBLIC
    audience: Audience = Audience.ROOM
    #: When the audience is narrower than the room, exactly who may receive it.
    #: Enforced at fanout; a recipient outside this list never sees the frame.
    restricted_to_participant_ids: list[str] | None = None
    #: The `command_id` that caused this event, for idempotency and tracing.
    causation_id: str | None = None
    payload: dict = Field(default_factory=dict)
