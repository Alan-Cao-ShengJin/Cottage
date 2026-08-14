"""Negotiated capabilities and the runtime policy derived from them.

**The rule this module exists to enforce: runtime behavior is derived from declared
capabilities, never from a provider or product label.** "ChatGPT", "Claude Code",
and "some A2A agent" are display strings. Whether we may push to a participant,
whether it can renew a lease on its own clock, and how long a lease it may hold
are answers to capability questions, and only to capability questions.

`HostClass` therefore supplies *suggested defaults* for a client that declares
nothing, and nothing else. A client that declares `supports_push` gets pushed to
regardless of what label it wears; a client that stops declaring
`can_initiate_followup` loses long leases even if its label says otherwise. Host
classes change as vendors ship features — the derivation here must not.
"""

from __future__ import annotations

from collections.abc import Iterable
from enum import Enum

from pydantic import BaseModel


class Capability(str, Enum):
    """What a connecting client claims it can do.

    Declared at connect time, intersected with what the chosen transport can
    actually honor, and the result is the negotiated set (`docs/PROTOCOL.md` §3).
    """

    #: We can deliver events to it without it asking (SSE, A2A webhook).
    SUPPORTS_PUSH = "supports_push"
    #: It will call us in a loop and can block waiting for events.
    SUPPORTS_POLL = "supports_poll"
    #: It consumes the room event stream at all (vs. command-only clients).
    CAN_RECEIVE_EVENTS = "can_receive_events"
    #: It can take a *next* action on its own after receiving an event — the
    #: capability that makes lease renewal and task progress possible unattended.
    CAN_INITIATE_FOLLOWUP = "can_initiate_followup"
    #: It can execute work with no human in the loop.
    CAN_EXECUTE_BACKGROUND = "can_execute_background"
    #: It only acts while a human is engaged with it. Not a defect — a fact that
    #: coordination must account for.
    REQUIRES_HUMAN_PRESENCE = "requires_human_presence"
    #: It can resume the stream from a `seq` cursor rather than needing a snapshot.
    SUPPORTS_RESUME = "supports_resume"
    #: It can run tools/commands, i.e. actually do task work.
    SUPPORTS_TOOLS = "supports_tools"
    #: It can publish and consume artifact versions.
    SUPPORTS_ARTIFACTS = "supports_artifacts"


class CapabilityProfile(BaseModel):
    """The negotiated capability set as explicit flags.

    This is the only input (with room policy) to runtime behavior decisions.
    """

    can_receive_events: bool = False
    can_initiate_followup: bool = False
    can_execute_background: bool = False
    requires_human_presence: bool = False
    supports_push: bool = False
    supports_poll: bool = False
    supports_resume: bool = False
    supports_tools: bool = False
    supports_artifacts: bool = False

    @classmethod
    def from_capabilities(cls, caps: Iterable[Capability | str] | None) -> CapabilityProfile:
        """Build a profile from a declared set.

        Accepts raw strings so an adapter can pass what a client sent without
        pre-validating. Unknown values raise — a typo in a declaration should surface,
        not silently drop a capability the client believes it has. Negotiation, not
        this constructor, is where unrecognized capabilities are dropped.
        """
        declared = {Capability(c) for c in caps} if caps else set()
        return cls(
            can_receive_events=Capability.CAN_RECEIVE_EVENTS in declared,
            can_initiate_followup=Capability.CAN_INITIATE_FOLLOWUP in declared,
            can_execute_background=Capability.CAN_EXECUTE_BACKGROUND in declared,
            requires_human_presence=Capability.REQUIRES_HUMAN_PRESENCE in declared,
            supports_push=Capability.SUPPORTS_PUSH in declared,
            supports_poll=Capability.SUPPORTS_POLL in declared,
            supports_resume=Capability.SUPPORTS_RESUME in declared,
            supports_tools=Capability.SUPPORTS_TOOLS in declared,
            supports_artifacts=Capability.SUPPORTS_ARTIFACTS in declared,
        )

    def to_capabilities(self) -> list[Capability]:
        pairs = (
            (self.can_receive_events, Capability.CAN_RECEIVE_EVENTS),
            (self.can_initiate_followup, Capability.CAN_INITIATE_FOLLOWUP),
            (self.can_execute_background, Capability.CAN_EXECUTE_BACKGROUND),
            (self.requires_human_presence, Capability.REQUIRES_HUMAN_PRESENCE),
            (self.supports_push, Capability.SUPPORTS_PUSH),
            (self.supports_poll, Capability.SUPPORTS_POLL),
            (self.supports_resume, Capability.SUPPORTS_RESUME),
            (self.supports_tools, Capability.SUPPORTS_TOOLS),
            (self.supports_artifacts, Capability.SUPPORTS_ARTIFACTS),
        )
        return [cap for enabled, cap in pairs if enabled]


class HostClass(str, Enum):
    """A **descriptive label** for how a participant is hosted.

    Used for display, telemetry, and picking default capabilities when a client
    declares none. It must never appear in a behavior decision — see
    `derive_runtime_policy`, which does not take it as an argument.
    """

    BROWSER_HUMAN = "browser_human"
    INTERACTIVE_CLIENT = "interactive_client"
    PERSISTENT_LOCAL = "persistent_local"
    NATIVE_REMOTE_A2A = "native_remote_a2a"
    UNKNOWN = "unknown"


#: Starting point for a client that declares nothing. A declaration always wins.
SUGGESTED_CAPABILITIES: dict[HostClass, tuple[Capability, ...]] = {
    HostClass.BROWSER_HUMAN: (
        Capability.CAN_RECEIVE_EVENTS,
        Capability.SUPPORTS_PUSH,
        Capability.SUPPORTS_RESUME,
        Capability.REQUIRES_HUMAN_PRESENCE,
    ),
    HostClass.INTERACTIVE_CLIENT: (
        Capability.CAN_RECEIVE_EVENTS,
        Capability.SUPPORTS_POLL,
        Capability.REQUIRES_HUMAN_PRESENCE,
        Capability.SUPPORTS_TOOLS,
    ),
    HostClass.PERSISTENT_LOCAL: (
        Capability.CAN_RECEIVE_EVENTS,
        Capability.SUPPORTS_POLL,
        Capability.SUPPORTS_RESUME,
        Capability.CAN_INITIATE_FOLLOWUP,
        Capability.CAN_EXECUTE_BACKGROUND,
        Capability.SUPPORTS_TOOLS,
        Capability.SUPPORTS_ARTIFACTS,
    ),
    HostClass.NATIVE_REMOTE_A2A: (
        Capability.CAN_RECEIVE_EVENTS,
        Capability.SUPPORTS_PUSH,
        Capability.SUPPORTS_RESUME,
        Capability.CAN_INITIATE_FOLLOWUP,
        Capability.CAN_EXECUTE_BACKGROUND,
        Capability.SUPPORTS_TOOLS,
        Capability.SUPPORTS_ARTIFACTS,
    ),
    HostClass.UNKNOWN: (Capability.CAN_RECEIVE_EVENTS,),
}


class DeliveryMode(str, Enum):
    """How events actually reach a connection. Derived from capabilities."""

    PUSH = "push"
    LONG_POLL = "long_poll"
    #: Reachable only when a human engages it; nothing arrives on our clock.
    ATTENDED_PULL = "attended_pull"
    #: Declared no event consumption at all: commands only.
    NONE = "none"


class RuntimePolicy(BaseModel):
    """What a participant may do, derived from capabilities + room policy.

    Recomputed on every connect and every capability change. Never stored as the
    authority — always derivable, so a capability change takes effect immediately.
    """

    delivery_mode: DeliveryMode
    heartbeat_interval_s: int
    may_claim: bool
    max_lease_seconds: int
    #: Can it renew its own lease without a human present? Determines whether a
    #: lease is safe to hand out for a long period.
    lease_renewable_unattended: bool
    #: Why `may_claim` is False, for an honest error message.
    claim_denied_reason: str | None = None


#: A participant that cannot renew unattended gets a short lease whatever the room
#: default is: nobody can extend it if its human walks away mid-task.
ATTENDED_MAX_LEASE_SECONDS = 300

DEFAULT_HEARTBEAT_INTERVAL_SECONDS = 20


def derive_runtime_policy(
    profile: CapabilityProfile,
    *,
    default_lease_seconds: int,
    max_lease_seconds: int,
    allow_attended_claims: bool,
    heartbeat_interval_s: int,
) -> RuntimePolicy:
    """Pure function. Note the absent argument: there is no host class here.

    Room policy supplies the ceilings; capabilities decide where in them a
    participant lands.
    """
    if profile.supports_push:
        delivery = DeliveryMode.PUSH
    elif profile.supports_poll:
        delivery = DeliveryMode.LONG_POLL
    elif profile.can_receive_events:
        delivery = DeliveryMode.ATTENDED_PULL
    else:
        delivery = DeliveryMode.NONE

    renewable_unattended = profile.can_initiate_followup and not profile.requires_human_presence

    # Ordered most-specific first, so the message names the reason a human would
    # find actionable rather than the first box that happened to be unticked.
    denied: str | None = None
    if not profile.supports_tools:
        denied = "did not declare supports_tools, so it cannot execute claimed work"
    elif not allow_attended_claims and profile.requires_human_presence:
        denied = (
            "declared that it requires human presence to act, and this room does not "
            "allow attended claims (room policy allow_attended_claims) — nobody could "
            "renew its lease if its human stepped away mid-task"
        )
    elif not allow_attended_claims and not profile.can_execute_background:
        denied = (
            "did not declare can_execute_background, and this room does not allow "
            "attended claims (room policy allow_attended_claims)"
        )
    if denied:
        denied = f"This participant {denied}."

    ceiling = min(default_lease_seconds, max_lease_seconds)
    lease = ceiling if renewable_unattended else min(ceiling, ATTENDED_MAX_LEASE_SECONDS)

    return RuntimePolicy(
        delivery_mode=delivery,
        heartbeat_interval_s=heartbeat_interval_s,
        may_claim=denied is None,
        max_lease_seconds=lease,
        lease_renewable_unattended=renewable_unattended,
        claim_denied_reason=denied,
    )
