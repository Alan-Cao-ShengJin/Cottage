"""ARP command payloads — the inbound contract every adapter translates into.

`command_id` is a client-generated idempotency key. Replaying a command with the
same `command_id` returns the original result and appends no new event
(`docs/PROTOCOL.md` §2), which is what makes a long-poll client safe to retry
after a timeout it could not distinguish from a failure.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from .activity import (
    MAX_ACTIVITY_SUMMARY_CHARS,
    MAX_ACTIVITY_TOOL_CHARS,
    ActivityPhase,
)
from .capabilities import Capability, HostClass
from .checkpoint import MAX_SUMMARY_CHARS, ResumeState
from .directive import DirectiveAction
from .disclosure import Disclosure, Provenance
from .goal import GoalStatus, WorkerDisposition
from .identity import PrincipalKind
from .job import JobOrigin, JobState
from .message import Speaker
from .question import MAX_ANSWER_CHARS, MAX_QUESTION_CHARS
from .room import (
    MAX_ROOM_EXTENSION_SECONDS,
    MIN_ROOM_EXTENSION_SECONDS,
    InvitationTargetKind,
    ParticipantRole,
    RetentionPolicy,
    RoomPolicy,
    RoomRole,
    RoomVisibility,
    RuntimeOperationalState,
    RuntimeRole,
    Scope,
)
from .task import DependencyKind
from .work import WorkStatus
from .worker import SupervisorCapacity, WorkerProvenance, WorkerState


class CommandMeta(BaseModel):
    """Mixed into every command. Adapters must pass a stable `command_id`.

    Unknown fields are **rejected**, not ignored. Pydantic's default would drop them
    silently, and for this domain that is a privacy failure rather than a nuisance:
    `post_message(body=..., to_participant_id=...)` is a natural thing for an agent to
    write — `to_participant_id` is a real field on `Disclosure`, on messages and on task
    proposals — and silently dropping it publishes to the whole room content that was
    addressed to one participant. The control appears to work and does the opposite,
    which is the failure shape this codebase has shipped four times (D-024, D-026,
    D-027, D-030). A rejected command is a bad request; an ignored field is a leak.
    """

    model_config = ConfigDict(extra="forbid")

    command_id: str | None = None
    #: Which of the caller's runtimes is speaking. Cross-cutting rather than
    #: per-command, for the same reason `command_id` is: it identifies the *sender*,
    #: not the request. A participant with one open connection never needs it; one
    #: with several needs it wherever executor affinity is decided, because a seat
    #: shared by a chat surface and a background worker is two runtimes with one
    #: name (D-032, D-034).
    connection_id: str | None = None


# ---------------------------------------------------------------------------
# Rooms & membership
# ---------------------------------------------------------------------------


class CreateRoomCommand(CommandMeta):
    name: str = Field(min_length=1, max_length=140)
    purpose: str = Field(default="", max_length=4000)
    #: Durable cold-start context for a participant arriving without room history.
    charter: str = Field(default="", max_length=8000)
    #: `cross_org` by default. An `internal` room refuses a foreign-org identity
    #: outright at join (`core/rooms.py`), so an internal default made the product's
    #: own sentence — "invite someone over the internet" — fail on the path nobody
    #: passes an argument on. The privacy boundary is unaffected: content defaults to
    #: `room_public`, and an `org_internal` payload is still rejected here rather than
    #: downgraded. Pass `internal` deliberately for a single-org room.
    visibility: RoomVisibility = RoomVisibility.CROSS_ORG
    policy: RoomPolicy | None = None
    retention: RetentionPolicy | None = None


class UpdateRoomCharterCommand(CommandMeta):
    charter: str = Field(default="", max_length=8000)
    disclosure: Disclosure = Field(default_factory=Disclosure)


class ExtendRoomCommand(CommandMeta):
    extend_seconds: int = Field(ge=MIN_ROOM_EXTENSION_SECONDS, le=MAX_ROOM_EXTENSION_SECONDS)


class DrainRuntimeCommand(CommandMeta):
    """Stop accepting work from one of your own runtimes (D-062).

    Addressed to a runtime, not a task: it is not "stop this piece of work" but "stop
    believing this process". That is why there is no fence here — a drained runtime may
    be holding a perfectly valid one.
    """

    attachment_id: str = Field(min_length=1, max_length=64)
    #: Free text for the log. Say how the runtime was lost, because "drained" alone
    #: cannot later be told apart from a clean shutdown.
    reason: str = Field(default="", max_length=500)


class ResumeRuntimeCommand(CommandMeta):
    """Let a drained runtime act again.

    Deliberately not folded into reconnect. Reconnecting proves a process is alive;
    this asserts the *old* one is dead, which no server can observe and only the person
    who stopped it can vouch for.
    """

    attachment_id: str = Field(min_length=1, max_length=64)
    note: str = Field(default="", max_length=500)


class CreateInvitationCommand(CommandMeta):
    target_kind: InvitationTargetKind = InvitationTargetKind.LINK
    target_value: str | None = Field(default=None, max_length=320)
    role: ParticipantRole = ParticipantRole.COLLABORATOR
    #: Narrower than the role's defaults if given; never broader.
    scopes: list[Scope] | None = None
    max_redemptions: int = Field(default=1, ge=1, le=200)
    ttl_seconds: int | None = Field(default=None, ge=60, le=30 * 24 * 3600)


class JoinRoomCommand(CommandMeta):
    """Redeem an invitation and connect in one step.

    Capabilities are declared per connection, not per identity: the same agent may
    connect from a pushable transport now and a poll-only one later, and the room
    must react to what is true right now.
    """

    invitation_token: str = Field(min_length=8, max_length=512)
    display_name: str | None = Field(default=None, max_length=80)
    kind: PrincipalKind = PrincipalKind.AGENT
    #: Descriptive label only; supplies defaults when `capabilities` is omitted.
    host_class: HostClass = HostClass.UNKNOWN
    capabilities: list[Capability] | None = None
    description: str = Field(default="", max_length=2000)


class ConnectCommand(CommandMeta):
    """Open (or re-open) a connection for an existing participant."""

    host_class: HostClass = HostClass.UNKNOWN
    capabilities: list[Capability] | None = None
    since_seq: int = Field(default=0, ge=0)
    #: A stable, client-chosen name for the *runtime* behind this connection, so a
    #: reconnect can be recognised as the same executor rather than a new one
    #: (D-032). Omit it if you cannot guarantee stability: ephemeral is the honest
    #: default, and a label that changes every connection is worse than none because it
    #: accumulates dead identities that look resumable.
    attachment_label: str | None = Field(default=None, min_length=1, max_length=128)
    #: Whether that label will address the same runtime after a *process* restart,
    #: not merely a transport one. Recorded rather than acted on: affinity already
    #: keys on the attachment. It is what later lets a recovery claim tell "the
    #: worker came back" from "something reused the name" (D-036, D-038). Declare
    #: it false if you cannot promise it. Ignored when no label is given.
    attachment_resumable: bool = True
    #: How this client will actually receive events, so negotiation intersects
    #: against the right transport (D-047).
    #:
    #: `POST /connect` cannot tell whether you are about to open the SSE stream or
    #: poll `GET /events`, and it used to assume SSE for everyone. A polling worker
    #: therefore lost `supports_poll` in the intersection, fell through to
    #: `attended_pull`, and the room described a process with no human anywhere near
    #: it as **attended** — the exact opposite of the truth, produced by a rule that
    #: exists to keep declarations honest.
    #:
    #: Naming it is a promise like any other declaration here. A client that says
    #: `sse` and never opens the stream simply stops being heard from, and heartbeat
    #: grading takes it from there.
    transport: str | None = None
    #: What this runtime is *for*: a surface a human works at, or a process that
    #: keeps working when they close it (D-054). Descriptive only — no behaviour
    #: derives from it, because behaviour derives from negotiated capabilities and
    #: nothing else (principle 4). It exists so the room can say *which* runtime of
    #: a seat is live rather than only that one of them is.
    runtime_role: RuntimeRole = RuntimeRole.UNSPECIFIED
    #: How this runtime performs work — `human`, `echo`, `subprocess`, a model
    #: adapter. Free-form on purpose: the room must not need editing when a new kind
    #: of executor appears, and it never branches on the value.
    executor_kind: str = Field(default="", max_length=60)
    #: Provider or model, if this runtime chooses to say. Absent is the honest
    #: default and a common one: a worker delegating to an agent CLI usually does not
    #: know which model answered, and inventing a value would be a claim about
    #: someone else's system.
    executor_model: str = Field(default="", max_length=120)


class HeartbeatCommand(CommandMeta):
    connection_id: str


class SetRuntimeStateCommand(CommandMeta):
    """Change the work posture of the caller's own durable runtime.

    The attachment is intentionally absent: core resolves it from this live
    connection, preventing one runtime from writing another runtime's projection.
    """

    connection_id: str = Field(min_length=1, max_length=64)
    state: RuntimeOperationalState
    summary: str = Field(default="", max_length=280)
    waiting_reason: str = Field(default="", max_length=500)
    task_id: str | None = Field(default=None, max_length=64)
    work_id: str | None = Field(default=None, max_length=64)


class DisconnectCommand(CommandMeta):
    connection_id: str


class LeaveRoomCommand(CommandMeta):
    note: str = Field(default="", max_length=500)


# ---------------------------------------------------------------------------
# Coordination
# ---------------------------------------------------------------------------


class PostMessageCommand(CommandMeta):
    body: str = Field(min_length=1, max_length=8000)
    disclosure: Disclosure = Field(default_factory=Disclosure)
    #: What this message is about: a task id, work id, or artifact id.
    about_ref: str | None = Field(default=None, max_length=64)
    #: Whose words these are. `agent` — the default and the existing behaviour — is the
    #: participant's own account of the work. `human` says the participant is relaying its
    #: person, which is the case the room previously had no way to express: a person typing
    #: into their agent's interface arrived as an agent speaking (D-090).
    speaking_for: Speaker = Speaker.AGENT


class NoteActivityCommand(CommandMeta):
    """Say what you are doing right now (D-082).

    Cheap and frequent by design — this is the channel that keeps a room looking
    alive between the events that change state. It changes no mutable coordination
    projection, lease, or task status. A dropped delivery is recovered from the log.

    `summary` is what you would say out loud to a colleague at the next desk, in one
    line. Note what there is no field for: your reasoning, your plan, your prompt, or
    what you were thinking. That absence is deliberate and matches
    `AppendCheckpointCommand.resume_state` — a narration channel is the most inviting
    place in this product to paste a chain of thought, so the schema offers nowhere
    to put one and the disclosure boundary inspects what does arrive.
    """

    phase: ActivityPhase
    summary: str = Field(min_length=1, max_length=MAX_ACTIVITY_SUMMARY_CHARS)
    #: What is being run, for the two tool phases. A name and its target, never a
    #: full command line — those carry flags, paths and occasionally credentials.
    tool: str | None = Field(default=None, max_length=MAX_ACTIVITY_TOOL_CHARS)
    #: The task or work card this narrates, when there is one. Optional because the
    #: gap being filled includes a worker that is alive and between tasks.
    task_id: str | None = None
    work_id: str | None = None
    #: Identifies the runtime producing the note. Core derives the durable
    #: attachment from this live connection; callers cannot choose an attachment.
    #: Optional only for legacy participant-level narration.
    connection_id: str | None = None
    disclosure: Disclosure = Field(default_factory=Disclosure)


class DeclareWorkCommand(CommandMeta):
    headline: str = Field(min_length=1, max_length=200)
    targets: list[str] = Field(default_factory=list, max_length=50)
    status: WorkStatus = WorkStatus.ACTIVE
    task_id: str | None = None
    note: str = Field(default="", max_length=2000)
    expected_done_by: str | None = None
    #: The default models one participant's singular "current work": an identical
    #: reconnect reuses it and a changed declaration supersedes it. A runtime that
    #: genuinely performs several concurrent streams must say so explicitly.
    allow_parallel: bool = False
    disclosure: Disclosure = Field(default_factory=Disclosure)


class UpdateWorkCommand(CommandMeta):
    work_id: str
    headline: str | None = Field(default=None, max_length=200)
    targets: list[str] | None = Field(default=None, max_length=50)
    status: WorkStatus | None = None
    note: str | None = Field(default=None, max_length=2000)
    expected_done_by: str | None = None


class EndWorkCommand(CommandMeta):
    work_id: str
    note: str = Field(default="", max_length=500)


# ---------------------------------------------------------------------------
# Tasks & leases
# ---------------------------------------------------------------------------


class CreateTaskCommand(CommandMeta):
    title: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=8000)
    targets: list[str] = Field(default_factory=list, max_length=50)
    priority: int = Field(default=0, ge=-100, le=100)
    disclosure: Disclosure = Field(default_factory=Disclosure)
    #: Propose straight to a participant instead of leaving the task open.
    propose_to_participant_id: str | None = None
    claim_immediately: bool = False


class ClaimTaskCommand(CommandMeta):
    task_id: str
    #: Clamped to the runtime policy derived from this participant's capabilities.
    requested_lease_seconds: int | None = Field(default=None, ge=30, le=3600)


class RenewClaimCommand(CommandMeta):
    task_id: str
    #: The fence the caller believes it holds. A lower value is `stale_fence`.
    fence: int
    extend_seconds: int | None = Field(default=None, ge=30, le=3600)


class ReleaseClaimCommand(CommandMeta):
    task_id: str
    fence: int
    note: str = Field(default="", max_length=500)
    #: Release work another live runtime of your own seat is executing. Permitted
    #: only to a human principal or a room admin, and only with a reason, because
    #: the thing being overridden is a safety rule about the *world* rather than a
    #: database consistency rule (D-035). The reason is stamped on the event: an
    #: override that leaves no trace is indistinguishable from a bug.
    force: bool = False
    reason: str = Field(default="", max_length=500)


class MintCredentialCommand(CommandMeta):
    """Mint a runtime credential for one of your own attachments.

    `scopes` narrows further; omitting it takes everything the seat holds that a
    runtime is allowed to carry. It can never widen, and a credential can never
    mint another one.
    """

    label: str = Field(default="", max_length=120)
    scopes: list[Scope] | None = None
    #: Mandatory in effect: omitted means the default, never forever.
    ttl_seconds: int | None = Field(default=None, ge=300, le=90 * 24 * 3600)


class RevokeCredentialCommand(CommandMeta):
    credential_id: str
    reason: str = Field(default="", max_length=500)


class SetParticipantRoleCommand(CommandMeta):
    """Change what a participant may do in this room.

    Exists because B uncovered a real authority blocker: only the seat that created
    a room held `room.admin`, and there was no way to grant it — so the humans'
    *own* control surfaces could never steer their own workers. `participant.left`
    and `participant.scopes_changed` were both in the event registry; only one of
    them had a way to happen.

    Narrowing still applies: scopes may subset the new role's defaults, never exceed
    them, and an untrusted identity keeps losing the denied set. A grant is a
    promotion within the rules, not a bypass of them.
    """

    target_participant_id: str
    role: ParticipantRole
    #: Narrower than the role's defaults if given; never broader.
    scopes: list[Scope] | None = None
    reason: str = Field(default="", max_length=500)


class IssueDirectiveCommand(CommandMeta):
    """Direct a participant without becoming it.

    The whole point of the control plane: a human pauses, stops, redirects or
    answers a worker while the work stays that worker's job. Nothing here moves a
    lease or an executor, so a directive is not a covert takeover — and because
    `claim`, `complete` and `update` consult the resulting state, it is not a
    request the runtime may quietly decline either.

    `human_origin` is deliberately absent: it is derived server-side from the
    issuer's identity. A caller-supplied one would let a same-seat unattended
    runtime manufacture "a human said stop" out of its own credentials.
    """

    target_participant_id: str
    action: DirectiveAction
    #: Which work this is about. Required for everything except `input`, since
    #: pausing "in general" is not something the task layer can enforce.
    task_id: str | None = None
    #: Required for anything that halts work. Whoever finds a task stopped needs to
    #: know why without having to ask the person who stopped it.
    reason: str = Field(default="", max_length=2000)
    #: Only meaningful for `reprioritize`. It lives on this command rather than on
    #: `update` because `update` demands the lease, which someone steering
    #: deliberately does not hold.
    priority: int | None = Field(default=None, ge=-100, le=100)


class AcknowledgeDirectiveCommand(CommandMeta):
    """Record that the target saw a directive — and, for `input`, consumed it.

    Never undoes or re-applies an effect. Acknowledging a stop does not re-stop
    anything; it only means the room can now say the worker knew.
    """

    directive_id: str
    #: An agent may decline. The room's job is to make the refusal visible, not to
    #: argue with it. Ignored for control actions, whose effect has already landed.
    rejected: bool = False
    note: str = Field(default="", max_length=2000)


class TakeOverExecutionCommand(CommandMeta):
    """Become the executor of a lease your own seat already holds.

    The visible alternative to silently re-claiming. It moves execution between
    runtimes of one participant, increments the fence so the displaced runtime's
    next mutation fails as stale rather than succeeding late, and says why in the
    room. Nothing about *who holds* the lease changes — this is not a seizure from
    another participant, which is a different act with different rules (D-031).
    """

    task_id: str
    fence: int
    reason: str = Field(min_length=1, max_length=500)


class UpdateTaskCommand(CommandMeta):
    task_id: str
    #: Required whenever the task is held; omitted only for unheld tasks.
    fence: int | None = None
    title: str | None = Field(default=None, max_length=200)
    description: str | None = Field(default=None, max_length=8000)
    targets: list[str] | None = Field(default=None, max_length=50)
    priority: int | None = Field(default=None, ge=-100, le=100)
    in_progress: bool | None = None


class CompleteTaskCommand(CommandMeta):
    task_id: str
    fence: int
    result: str = Field(default="", max_length=8000)
    disclosure: Disclosure = Field(default_factory=Disclosure)


class CancelTaskCommand(CommandMeta):
    task_id: str
    reason: str = Field(default="", max_length=500)


class AppendCheckpointCommand(CommandMeta):
    """Record durable progress on work you hold (D-050).

    `summary` is room-visible and must read as an outcome — what was done, what it
    means, what is next. `resume_state` is the same-seat bookmark and is a closed
    schema on purpose: the field an executor most wants to add is "everything I was
    thinking", and that field must not exist.

    Retry is safe: `command_id` makes a replay return the original checkpoint rather
    than appending a second one, which matters because the natural moment to
    checkpoint is also the moment a worker is most likely to be interrupted.
    """

    task_id: str
    #: The lease generation the caller believes it holds. A checkpoint is a claim
    #: about work in progress, so it is fenced like every other one.
    fence: int
    summary: str = Field(min_length=1, max_length=MAX_SUMMARY_CHARS)
    resume_state: ResumeState | None = None


class AskQuestionCommand(CommandMeta):
    """Ask something, of a participant or of the room (D-051).

    Carries no authority: this is why any participant that may speak may ask, and
    why a question is not a directive with the ends swapped. Directives require
    `room.admin` precisely so a worker cannot manufacture instructions, and a
    reversed directive would hand back exactly that.
    """

    body: str = Field(min_length=1, max_length=MAX_QUESTION_CHARS)
    #: `None` addresses the room. Narrows who is expected to reply, never who may.
    to_participant_id: str | None = None
    task_id: str | None = None
    #: Opt-in, and it costs the asker its lease: the room checkpoints the task,
    #: parks it as `waiting_input`, and releases the claim. Default `False` because
    #: a worker that halts on every uncertainty cannot work unattended.
    blocking: bool = False
    #: Required when blocking, since the checkpoint written on the way down is what
    #: the next run resumes from.
    fence: int | None = None
    checkpoint_summary: str = Field(default="", max_length=MAX_SUMMARY_CHARS)
    resume_state: ResumeState | None = None


class AnswerQuestionCommand(CommandMeta):
    """Reply to a question, releasing the asker's task if it was parked.

    Also its own primitive rather than an `input` directive: answering is not an
    exercise of authority, and routing it through the control plane would mean only
    room admins could ever unblock a worker.
    """

    question_id: str
    body: str = Field(min_length=1, max_length=MAX_ANSWER_CHARS)


class AddDependencyCommand(CommandMeta):
    from_task_id: str
    to_task_id: str
    kind: DependencyKind = DependencyKind.BLOCKS


class ResolveProposalCommand(CommandMeta):
    proposal_id: str
    accept: bool = False
    reject: bool = False
    delegate_to_participant_id: str | None = None
    note: str = Field(default="", max_length=500)


# ---------------------------------------------------------------------------
# Shared state (contract fixed now; implementation lands in M2)
# ---------------------------------------------------------------------------


class SetStateCommand(CommandMeta):
    key: str = Field(min_length=1, max_length=200)
    value: object
    #: 0 asserts "create only". Omitting it on an existing key is rejected — there
    #: is no last-writer-wins path (`docs/PROTOCOL.md` §6).
    expected_revision: int | None = None
    provenance_source: str | None = Field(default=None, max_length=500)
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    derived_from: list[str] = Field(default_factory=list, max_length=50)
    disclosure: Disclosure = Field(default_factory=Disclosure)


__all__ = [
    "AddDependencyCommand",
    "CancelTaskCommand",
    "ClaimTaskCommand",
    "CommandMeta",
    "CompleteTaskCommand",
    "ConnectCommand",
    "CreateInvitationCommand",
    "CreateRoomCommand",
    "CreateTaskCommand",
    "DeclareWorkCommand",
    "DisconnectCommand",
    "EndWorkCommand",
    "ExtendRoomCommand",
    "HeartbeatCommand",
    "JoinRoomCommand",
    "LeaveRoomCommand",
    "PostMessageCommand",
    "Provenance",
    "ReleaseClaimCommand",
    "RenewClaimCommand",
    "ResolveProposalCommand",
    "SetStateCommand",
    "UpdateTaskCommand",
    "UpdateWorkCommand",
]


# ---------------------------------------------------------------------------
# The coordination hierarchy (D-088)
# ---------------------------------------------------------------------------


class AssignRoomRoleCommand(CommandMeta):
    """Place a seat in the coordination hierarchy, or stand it down.

    Deliberately separate from `SetParticipantRoleCommand`: that one rewrites a scope
    list, and conflating "where you sit" with "what you may do" is how a coordination
    label starts minting privileges (ADR-013).
    """

    target_participant_id: str
    room_role: RoomRole
    #: Required, and recorded. Promoting or demoting a coordinator is exactly the kind
    #: of act a room needs to be able to explain afterwards.
    reason: str = Field(min_length=1, max_length=2000)


class PostJobCommand(CommandMeta):
    """Put durable human intent on the board.

    A supervisor receiving a request from its human posts it here rather than starting
    work: the orchestrator allocates against room priorities and supervisor capacity,
    and the requesting supervisor may or may not be the one that ends up owning it.
    """

    title: str = Field(min_length=1, max_length=200)
    desired_outcome: str = Field(default="", max_length=8000)
    #: The human's own words, unedited. Kept because a paraphrase cannot be
    #: un-paraphrased once the intent is disputed.
    human_instruction: str = Field(default="", max_length=8000)
    room_goal_relationship: str = Field(default="", max_length=2000)
    on_behalf_of_participant_id: str | None = None
    origin: JobOrigin = JobOrigin.HUMAN_STEER
    #: Urgency as requested. What the orchestrator decides lands in `priority`, and
    #: both are kept so a supervisor can see it was ranked rather than ignored.
    requested_urgency: int = Field(default=0, ge=-100, le=100)
    targets: list[str] = Field(default_factory=list, max_length=50)
    constraints: list[str] = Field(default_factory=list, max_length=20)
    acceptance_criteria: list[str] = Field(default_factory=list, max_length=20)
    source_goal_id: str | None = None
    source_goal_version: int | None = Field(default=None, ge=1)
    parent_job_id: str | None = None
    disclosure: Disclosure = Field(default_factory=Disclosure)


class UpdateJobCommand(CommandMeta):
    """Revise what a job asks for, or where it ranks. Omitted fields are untouched."""

    job_id: str
    priority: int | None = Field(default=None, ge=-100, le=100)
    desired_outcome: str | None = Field(default=None, max_length=8000)
    targets: list[str] | None = Field(default=None, max_length=50)
    constraints: list[str] | None = Field(default=None, max_length=20)
    acceptance_criteria: list[str] | None = Field(default=None, max_length=20)
    disclosure: Disclosure = Field(default_factory=Disclosure)


class AssignJobCommand(CommandMeta):
    """Allocate or reallocate a job. Orchestrator only."""

    job_id: str
    to_participant_id: str
    reason: str = Field(min_length=1, max_length=2000)
    #: The assignee's goal version this allocation is delivered through, when the
    #: orchestrator is replacing a goal in the same breath.
    assigned_goal_version: int | None = Field(default=None, ge=1)


class AcceptJobCommand(CommandMeta):
    job_id: str
    note: str = Field(default="", max_length=2000)


class SetJobStateCommand(CommandMeta):
    """A non-terminal move: active, paused, blocked. Terminal moves use CloseJob."""

    job_id: str
    state: JobState
    reason: str = Field(default="", max_length=2000)
    #: The lease-bearing task this job became, supplied when it moves to `active`.
    #: The job records which task serves it; the task keeps the fence and the lease,
    #: so the room never has two answers to "who holds this".
    task_id: str | None = None


class CloseJobCommand(CommandMeta):
    """End a job with an attributable reason. There is no path that deletes one."""

    job_id: str
    state: JobState
    reason: str = Field(min_length=1, max_length=2000)
    #: Required when `state` is `superseded`: a supersession that does not name its
    #: replacement is indistinguishable from a cancellation.
    superseded_by_job_id: str | None = None


class ReplaceGoalCommand(CommandMeta):
    """Set or wholly replace a supervisor's active goal.

    `expected_version` is the fence. Omitting it on an existing goal is refused rather
    than treated as "latest": a blind overwrite is how a stale orchestrator turn undoes
    a newer decision, and the whole point of versioning is that the caller states which
    generation it is acting against.
    """

    target_supervisor_participant_id: str
    objective: str = Field(min_length=1, max_length=2000)
    instructions: str = Field(default="", max_length=8000)
    worker_plan: str = Field(default="", max_length=4000)
    related_job_ids: list[str] = Field(default_factory=list, max_length=50)
    dependencies: list[str] = Field(default_factory=list, max_length=50)
    constraints: list[str] = Field(default_factory=list, max_length=20)
    acceptance_criteria: list[str] = Field(default_factory=list, max_length=20)
    reporting_requirements: str = Field(default="", max_length=2000)
    #: What happens to workers spawned under the version being replaced. The
    #: orchestrator must say; silence is how a superseded goal keeps executing.
    worker_disposition: WorkerDisposition = WorkerDisposition.STOP
    priority: int = Field(default=0, ge=-100, le=100)
    reason: str = Field(default="", max_length=2000)
    #: None means "there is no goal yet". Any other value must match exactly.
    expected_version: int | None = Field(default=None, ge=1)
    disclosure: Disclosure = Field(default_factory=Disclosure)


class AcknowledgeGoalCommand(CommandMeta):
    """Record that the target supervisor observed a version.

    Never permission for the effect: the goal took effect when it was written. A
    supervisor may acknowledge *and* reject, which is information rather than a veto.
    """

    goal_id: str
    version: int = Field(ge=1)
    note: str = Field(default="", max_length=2000)
    rejected: bool = False


class CloseGoalCommand(CommandMeta):
    goal_id: str
    status: GoalStatus
    reason: str = Field(min_length=1, max_length=2000)


class ReportCapacityCommand(CommandMeta):
    """Declare how much more this supervisor can take on.

    `offline` is not accepted: it is derived from liveness, because a runtime that has
    stopped beating cannot be trusted to report that it is gone.
    """

    declared: SupervisorCapacity
    max_concurrent_workers: int = Field(default=1, ge=0, le=64)
    note: str = Field(default="", max_length=2000)


class RegisterWorkerCommand(CommandMeta):
    """Declare a downstream worker this seat owns and answers for.

    Recorded, never verified. Re-registering the same `label` updates that worker
    rather than minting a second one, the same rule an attachment label follows.
    """

    label: str = Field(min_length=1, max_length=80)
    display_name: str = Field(default="", max_length=120)
    assignment: str = Field(default="", max_length=8000)
    related_job_id: str | None = None
    related_task_id: str | None = None
    related_work_id: str | None = None
    provenance: WorkerProvenance = WorkerProvenance.DECLARED
    #: Required when provenance is `room_attachment`, and refused otherwise.
    attachment_id: str | None = None
    declared_runtime: str = Field(default="", max_length=120)
    declared_model: str = Field(default="", max_length=120)
    #: The goal version that caused this worker to exist. Output from a worker spawned
    #: under an older version keeps that provenance.
    created_by_goal_version: int | None = Field(default=None, ge=1)
    disclosure: Disclosure = Field(default_factory=Disclosure)


class UpdateWorkerCommand(CommandMeta):
    """Report a worker's non-terminal state. The supervisor's claim, never presence."""

    worker_id: str
    state: WorkerState
    summary: str = Field(default="", max_length=2000)
    #: Required when `state` is `waiting`, for the same reason a runtime's `waiting`
    #: posture requires it: an unexplained wait is indistinguishable from a hang.
    waiting_reason: str = Field(default="", max_length=2000)
    disclosure: Disclosure = Field(default_factory=Disclosure)


class FinishWorkerCommand(CommandMeta):
    """End a worker. Completion here is not completion of the job."""

    worker_id: str
    state: WorkerState
    summary: str = Field(default="", max_length=4000)
    #: Where the evidence lives: a checkpoint id, an artifact version, a task id.
    result_reference: str = Field(default="", max_length=200)
    disclosure: Disclosure = Field(default_factory=Disclosure)
