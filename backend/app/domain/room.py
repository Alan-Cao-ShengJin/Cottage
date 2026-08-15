"""Rooms, invitations, participants, connections, presence.

Two ideas here are load-bearing and easy to conflate, so they are separate types:

* **Participant** — membership. Durable. "You are allowed in this room."
* **Connection** — one live transport attachment. Ephemeral, many per participant.

Presence is *derived* from connections and never stored as a mutable flag, because
a stored flag is wrong the moment a process dies without saying goodbye.
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, ConfigDict, Field

from ..util import is_past
from .capabilities import (
    DEFAULT_HEARTBEAT_INTERVAL_SECONDS,
    Capability,
    CapabilityProfile,
    DeliveryMode,
    HostClass,
    RuntimePolicy,
)
from .identity import IdentitySummary, TrustTier

MIN_ROOM_TTL_SECONDS = 60
MAX_ROOM_TTL_SECONDS = 90 * 24 * 3600
MIN_EVENT_AGE_DAYS = 1
MAX_EVENT_AGE_DAYS = 365
MIN_ROOM_EXTENSION_SECONDS = 60
MAX_ROOM_EXTENSION_SECONDS = 30 * 24 * 3600


class Scope(str, Enum):
    """Per-participant grants, checked in `core`, so every transport inherits them."""

    ROOM_READ = "room.read"
    EVENTS_SUBSCRIBE = "events.subscribe"
    MESSAGE_POST = "message.post"
    WORK_DECLARE = "work.declare"
    TASK_READ = "task.read"
    TASK_PROPOSE = "task.propose"
    #: Report progress on work you hold: move it to `in_progress`, revise its
    #: description or targets. Split out of `task.propose` because one scope was
    #: doing two unrelated jobs — "may say how my own work is going" and "may
    #: create tasks and hand them to other people" — which made the runtime
    #: credential carry authority no unattended executor needs (D-048).
    TASK_PROGRESS = "task.progress"
    TASK_CLAIM = "task.claim"
    STATE_READ = "state.read"
    STATE_WRITE = "state.write"
    ARTIFACT_WRITE = "artifact.write"
    ROOM_ADMIN = "room.admin"


class ParticipantRole(str, Enum):
    OBSERVER = "observer"
    COLLABORATOR = "collaborator"
    OWNER = "owner"


OBSERVER_SCOPES: tuple[Scope, ...] = (
    Scope.ROOM_READ,
    Scope.EVENTS_SUBSCRIBE,
    Scope.TASK_READ,
    Scope.STATE_READ,
)

COLLABORATOR_SCOPES: tuple[Scope, ...] = (
    *OBSERVER_SCOPES,
    Scope.MESSAGE_POST,
    Scope.WORK_DECLARE,
    Scope.TASK_PROPOSE,
    Scope.TASK_PROGRESS,
    Scope.TASK_CLAIM,
    Scope.STATE_WRITE,
    Scope.ARTIFACT_WRITE,
)

OWNER_SCOPES: tuple[Scope, ...] = (*COLLABORATOR_SCOPES, Scope.ROOM_ADMIN)

ROLE_SCOPES: dict[ParticipantRole, tuple[Scope, ...]] = {
    ParticipantRole.OBSERVER: OBSERVER_SCOPES,
    ParticipantRole.COLLABORATOR: COLLABORATOR_SCOPES,
    ParticipantRole.OWNER: OWNER_SCOPES,
}

#: Ordering for "never reduce standing". Redeeming an invitation must not demote an
#: existing participant — an owner clicking their own room's collaborator link would
#: otherwise lock themselves out of their own room.
ROLE_RANK: dict[ParticipantRole, int] = {
    ParticipantRole.OBSERVER: 0,
    ParticipantRole.COLLABORATOR: 1,
    ParticipantRole.OWNER: 2,
}

#: What a runtime credential may ever carry, however broad the seat is (D-048).
#:
#: An unattended executor needs to see the room, follow the stream, take and finish
#: work assigned to it, and say what it is doing. It does not need to reconfigure
#: the room, invite anyone, or publish artifacts, so those stay with the human's
#: own surface.
#:
#: `task.progress` rather than `task.propose`: reporting progress on work you hold
#: and creating work for other people were one scope until this credential made the
#: difference matter, and a least-privilege token that could hand out tasks would
#: not have deserved the name.
RUNTIME_SCOPES: frozenset[Scope] = frozenset(
    {
        Scope.ROOM_READ,
        Scope.EVENTS_SUBSCRIBE,
        Scope.TASK_READ,
        Scope.TASK_CLAIM,
        Scope.TASK_PROGRESS,
        Scope.WORK_DECLARE,
        Scope.STATE_READ,
        Scope.MESSAGE_POST,
    }
)

#: Scopes an untrusted identity may never hold, whatever its role says
#: (`docs/SECURITY.md` §5).
UNTRUSTED_DENIED_SCOPES: frozenset[Scope] = frozenset(
    {
        Scope.TASK_CLAIM,
        Scope.STATE_WRITE,
        Scope.ARTIFACT_WRITE,
        Scope.ROOM_ADMIN,
    }
)


class PrivacyClass(str, Enum):
    """`docs/SECURITY.md` §6. Filtered per recipient at projection and fanout time."""

    ROOM_PUBLIC = "room_public"
    ORG_INTERNAL = "org_internal"
    PARTICIPANT_PRIVATE = "participant_private"


class RoomVisibility(str, Enum):
    INTERNAL = "internal"
    CROSS_ORG = "cross_org"


class RoomStatus(str, Enum):
    OPEN = "open"
    CLOSED = "closed"
    PURGED = "purged"


class MembershipState(str, Enum):
    INVITED = "invited"
    JOINED = "joined"
    LEFT = "left"
    REMOVED = "removed"


class LeaveReason(str, Enum):
    GRACEFUL = "graceful"
    TIMEOUT = "timeout"
    REMOVED = "removed"


class Liveness(str, Enum):
    """Ordered worst → best; `core.presence` grades a participant across connections."""

    DISCONNECTED = "disconnected"
    STALE = "stale"
    IDLE = "idle"
    #: Present, but only reachable while a human is engaged with it.
    ATTENDED = "attended"
    LIVE_POLL = "live_poll"
    LIVE_PUSH = "live_push"


#: Rank for "best connection wins" grading. Higher is better.
LIVENESS_RANK: dict[Liveness, int] = {
    Liveness.DISCONNECTED: 0,
    Liveness.STALE: 1,
    Liveness.IDLE: 2,
    Liveness.ATTENDED: 3,
    Liveness.LIVE_POLL: 4,
    Liveness.LIVE_PUSH: 5,
}

#: Liveness a *healthy* connection implies, given how it receives events. Derived
#: from the negotiated delivery mode, never from a provider label.
DELIVERY_MODE_LIVENESS: dict[DeliveryMode, Liveness] = {
    DeliveryMode.PUSH: Liveness.LIVE_PUSH,
    DeliveryMode.LONG_POLL: Liveness.LIVE_POLL,
    DeliveryMode.ATTENDED_PULL: Liveness.ATTENDED,
    DeliveryMode.NONE: Liveness.IDLE,
}

#: Heartbeat multiples at which a connection degrades. See `docs/PROTOCOL.md` §3.
IDLE_AFTER_INTERVALS = 1
STALE_AFTER_INTERVALS = 3


class RetentionPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ttl_seconds: int | None = Field(default=None, ge=MIN_ROOM_TTL_SECONDS, le=MAX_ROOM_TTL_SECONDS)
    purge_on_close: bool = False
    max_event_age_days: int | None = Field(
        default=None, ge=MIN_EVENT_AGE_DAYS, le=MAX_EVENT_AGE_DAYS
    )


class RoomPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: May a participant that cannot act unattended hold an exclusive lease? Off by
    #: default: nobody can renew such a lease if its human walks away mid-task, so
    #: the room would stall until expiry. A room can opt in knowingly.
    allow_attended_claims: bool = False
    default_lease_seconds: int = 900
    max_lease_seconds: int = 3600
    heartbeat_interval_s: int = DEFAULT_HEARTBEAT_INTERVAL_SECONDS
    require_approval_for_cross_org_state_write: bool = False
    #: Bounds how long a work declaration survives with no heartbeat at all.
    work_stale_after_seconds: int = 120
    #: Bounds how long a declaration survives with a live heartbeat but no evidence of
    #: progress — no declare, update, or checkpoint. Necessarily longer than the
    #: heartbeat window, because a single model-backed step is expected to fit inside
    #: it; it is the honest upper bound on how long the board can be wrong about a
    #: wedged worker (D-059). Matched to `default_lease_seconds` so a stuck worker's
    #: card and its lease come up for question on the same timescale.
    work_progress_stale_after_seconds: int = 900
    #: Hard caps that bound disclosure volume as well as storage.
    max_message_chars: int = 8000
    max_state_value_bytes: int = 64_000


class Room(BaseModel):
    id: str
    org_id: str
    name: str
    #: What this room is for. Context for participants — not an instruction to them.
    purpose: str = ""
    visibility: RoomVisibility
    status: RoomStatus
    #: Highest allocated event seq. The room row *is* the sequencer.
    event_seq: int = 0
    #: Oldest seq still retained; a cursor below this gets `resume_gap`.
    retained_from_seq: int = 1
    policy: RoomPolicy = Field(default_factory=RoomPolicy)
    retention: RetentionPolicy = Field(default_factory=RetentionPolicy)
    created_at: str
    created_by_user_id: str
    expires_at: str | None = None
    closed_at: str | None = None

    @property
    def is_writable(self) -> bool:
        return self.status == RoomStatus.OPEN and not is_past(self.expires_at)


class InvitationTargetKind(str, Enum):
    EMAIL = "email"
    ORG = "org"
    #: Anyone holding the link, bounded by `max_redemptions` and expiry.
    LINK = "link"


class Invitation(BaseModel):
    """The only path to membership. The token is shown once, stored hashed, and
    never appears in an event payload (`docs/PROTOCOL.md` §2)."""

    id: str
    room_id: str
    target_kind: InvitationTargetKind
    target_value: str | None = None
    role: ParticipantRole
    scopes: list[Scope]
    max_redemptions: int = 1
    redemptions: int = 0
    expires_at: str | None = None
    created_at: str
    created_by_participant_id: str | None = None
    revoked_at: str | None = None

    @property
    def is_exhausted(self) -> bool:
        return self.redemptions >= self.max_redemptions


class Participant(BaseModel):
    id: str
    room_id: str
    agent_identity_id: str
    org_id: str
    role: ParticipantRole
    #: What this *caller* may do, which is not always what the seat may do: a
    #: runtime credential resolves to the same participant with a narrower set.
    scopes: list[Scope]
    trust: TrustTier
    state: MembershipState
    identity: IdentitySummary
    joined_at: str | None = None
    left_at: str | None = None
    #: Set when this caller authenticated with a runtime credential rather than the
    #: seat's own token. Recorded rather than inferred, because two things depend on
    #: knowing: a credential may never mint another one, and an audit reading
    #: "the participant did X" deserves to know which runtime of it actually did.
    credential_id: str | None = None

    def has(self, scope: Scope) -> bool:
        return scope in self.scopes

    @property
    def is_active(self) -> bool:
        return self.state == MembershipState.JOINED


class RuntimeCredential(BaseModel):
    """A narrow, expiring token for one runtime of a seat (D-048).

    Never carries the token itself. It exists once, at mint time, and is stored
    only as a hash — so this model is what the room can safely show, and what a
    human uses to decide which of their runtimes to revoke.
    """

    id: str
    room_id: str
    participant_id: str
    label: str = ""
    scopes: list[Scope] = Field(default_factory=list)
    created_at: str
    expires_at: str
    revoked_at: str | None = None
    last_used_at: str | None = None


class RuntimeRole(str, Enum):
    """What a runtime of a seat is *for*. Descriptive, never a permission.

    A seat may have a surface where its human works and a process that keeps working
    when they close the laptop. Both are the same participant with the same
    authority; the difference is who is watching, and the room should say which is
    which rather than leave a reader to infer it from a host label (D-054).

    Nothing branches on this. Behaviour comes from negotiated capabilities and
    nothing else (principle 4) — a room that started routing work by `role` would
    have reinvented vendor labels with extra steps.
    """

    #: Somebody is at the keyboard, at least intermittently.
    CONTROL_SURFACE = "control_surface"
    #: Runs on its own. Nobody is watching, by design.
    COMPANION = "companion"
    UNSPECIFIED = "unspecified"


class Attachment(BaseModel):
    """A durable runtime identity, between the seat and the transport (D-032).

    The layering is: logical agent → participant (the seat, which holds leases) →
    **attachment** (this: one runtime, addressable across transport loss) → many
    connections over time. A row exists only when a client supplied a stable label;
    a client that cannot is ephemeral and is identified by its connection instead.

    `is_resumable` is a *separate declaration*, not an inference from the label
    existing. It answers "will this label address the same runtime after transport
    loss?" — nothing here attests that the runtime remembers what it did, which is
    a different layer again (D-038).
    """

    id: str
    room_id: str
    participant_id: str
    #: Stable and client-chosen. Unique per participant, which is what makes a
    #: reattach land on this row rather than creating a second identity.
    label: str
    #: Descriptive; recorded for display and telemetry only.
    host_class: HostClass
    is_resumable: bool = False
    #: What this runtime says it is for, and how it says it does the work. Recorded,
    #: never verified, and never consulted for a behaviour decision (D-054).
    runtime_role: RuntimeRole = RuntimeRole.UNSPECIFIED
    executor_kind: str = ""
    executor_model: str = ""
    created_at: str
    last_seen_at: str


class Connection(BaseModel):
    id: str
    room_id: str
    participant_id: str
    #: The durable runtime this transport belongs to, when the client declared one.
    #: NULL means no durable runtime — never "no executor" (D-034).
    attachment_id: str | None = None
    #: Descriptive; recorded for display and telemetry only.
    host_class: HostClass
    profile: CapabilityProfile
    delivery_mode: DeliveryMode
    heartbeat_interval_s: int
    opened_at: str
    last_heartbeat_at: str
    #: How far this connection has been told about. Advisory: replay is driven by
    #: the cursor the client sends, not by this.
    last_delivered_seq: int = 0
    closed_at: str | None = None

    @property
    def negotiated_capabilities(self) -> list[Capability]:
        return self.profile.to_capabilities()


class RuntimeDeclaration(BaseModel):
    """What a runtime says about itself. The room records it and checks none of it."""

    role: RuntimeRole = RuntimeRole.UNSPECIFIED
    #: How this runtime performs work: `human`, `echo`, `subprocess`, a model adapter.
    #: A free label on purpose — the room must not need editing when a new kind of
    #: executor appears, and it never branches on this value.
    executor_kind: str = ""
    #: Provider or model, when the runtime chooses to say. Often absent, and absent
    #: is the honest default: a worker that shells out to a CLI frequently does not
    #: know which model answered.
    model: str = ""
    host_class: HostClass = HostClass.UNKNOWN
    is_resumable: bool = False


class RuntimeView(BaseModel):
    """One runtime of one seat, as the room may describe it.

    Split between what the room *derived* and what the client *declared*, because
    those are worth different amounts and merging them would hide which is which.
    `liveness` is computed from open connections and cannot be asserted; everything
    under `declared` is the client's own account of itself.

    What this deliberately does not say: that a companion runtime is the human's
    session, or shares its context. It is the same Cottage identity with bounded
    shared task state — the executor sees its own task and its own history and
    nothing else — and implying otherwise would misdescribe the one boundary the
    executor exists to hold (`docs/SECURITY.md`).
    """

    #: `attachment_id` for a durable runtime, else the connection id.
    ref: str
    is_attachment: bool
    label: str = ""
    liveness: Liveness
    connection_count: int
    delivery_modes: list[DeliveryMode] = Field(default_factory=list)
    last_seen_at: str | None = None
    #: Self-reported and unverifiable, which is why it is nested under a name that
    #: says so. Attribution, not verification — the same rule as a display name from
    #: an invitation (D-025).
    declared: RuntimeDeclaration = Field(default_factory=RuntimeDeclaration)


class PresenceView(BaseModel):
    """Derived, per participant. What the presence rail renders.

    `runtime` is included so every participant can see *why* another may or may not
    take work — coordination against a false assumption is the failure this
    prevents.
    """

    participant_id: str
    liveness: Liveness
    connection_count: int
    delivery_modes: list[DeliveryMode]
    negotiated_capabilities: list[Capability]
    runtime: RuntimePolicy | None = None
    last_seen_at: str | None = None
    #: Per runtime, because "this seat is live" answers the wrong question when a
    #: seat is a chat window plus a background worker. A human deciding whether to
    #: expect a prompt reply, and a worker deciding whether a sibling is executing,
    #: both need to know *which* runtime is live rather than that one of them is.
    runtimes: list[RuntimeView] = Field(default_factory=list)
