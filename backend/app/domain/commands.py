"""ARP command payloads — the inbound contract every adapter translates into.

`command_id` is a client-generated idempotency key. Replaying a command with the
same `command_id` returns the original result and appends no new event
(`docs/PROTOCOL.md` §2), which is what makes a long-poll client safe to retry
after a timeout it could not distinguish from a failure.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from .capabilities import Capability, HostClass
from .disclosure import Disclosure, Provenance
from .identity import PrincipalKind
from .room import (
    InvitationTargetKind,
    ParticipantRole,
    RetentionPolicy,
    RoomPolicy,
    RoomVisibility,
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


# ---------------------------------------------------------------------------
# Rooms & membership
# ---------------------------------------------------------------------------


class CreateRoomCommand(CommandMeta):
    name: str = Field(min_length=1, max_length=140)
    purpose: str = Field(default="", max_length=4000)
    visibility: RoomVisibility = RoomVisibility.INTERNAL
    policy: RoomPolicy | None = None
    retention: RetentionPolicy | None = None


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
