"""ARP command payloads — the inbound contract every adapter translates into.

`command_id` is a client-generated idempotency key. Replaying a command with the
same `command_id` returns the original result and appends no new event
(`docs/PROTOCOL.md` §2), which is what makes a long-poll client safe to retry
after a timeout it could not distinguish from a failure.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from .capabilities import Capability, HostClass
from .checkpoint import MAX_SUMMARY_CHARS, ResumeState
from .directive import DirectiveAction
from .disclosure import Disclosure, Provenance
from .identity import PrincipalKind
from .question import MAX_ANSWER_CHARS, MAX_QUESTION_CHARS
from .room import (
    MAX_ROOM_EXTENSION_SECONDS,
    MIN_ROOM_EXTENSION_SECONDS,
    InvitationTargetKind,
    ParticipantRole,
    RetentionPolicy,
    RoomPolicy,
    RoomVisibility,
    RuntimeRole,
    Scope,
)
from .task import DependencyKind
from .work import WorkStatus


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
    visibility: RoomVisibility = RoomVisibility.INTERNAL
    policy: RoomPolicy | None = None
    retention: RetentionPolicy | None = None


class ExtendRoomCommand(CommandMeta):
    extend_seconds: int = Field(ge=MIN_ROOM_EXTENSION_SECONDS, le=MAX_ROOM_EXTENSION_SECONDS)


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


class DeclareWorkCommand(CommandMeta):
    headline: str = Field(min_length=1, max_length=200)
    targets: list[str] = Field(default_factory=list, max_length=50)
    status: WorkStatus = WorkStatus.ACTIVE
    task_id: str | None = None
    note: str = Field(default="", max_length=2000)
    expected_done_by: str | None = None
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
