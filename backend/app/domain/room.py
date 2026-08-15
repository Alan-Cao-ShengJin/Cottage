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

from .capabilities import (
    DEFAULT_HEARTBEAT_INTERVAL_SECONDS,
    Capability,
    CapabilityProfile,
    DeliveryMode,
    HostClass,
    RuntimePolicy,
)
from .identity import IdentitySummary, TrustTier


class Scope(str, Enum):
    """Per-participant grants, checked in `core`, so every transport inherits them."""

    ROOM_READ = "room.read"
    EVENTS_SUBSCRIBE = "events.subscribe"
    MESSAGE_POST = "message.post"
    WORK_DECLARE = "work.declare"
    TASK_READ = "task.read"
    TASK_PROPOSE = "task.propose"
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

    ttl_seconds: int | None = None
    purge_on_close: bool = False
    max_event_age_days: int | None = None


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
        return self.status == RoomStatus.OPEN


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
    scopes: list[Scope]
    trust: TrustTier
    state: MembershipState
    identity: IdentitySummary
    joined_at: str | None = None
    left_at: str | None = None

    def has(self, scope: Scope) -> bool:
        return scope in self.scopes

    @property
    def is_active(self) -> bool:
        return self.state == MembershipState.JOINED


class Connection(BaseModel):
    id: str
    room_id: str
    participant_id: str
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
