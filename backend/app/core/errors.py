"""Typed domain errors.

Every error here is *information an agent can act on*, not a crash. A lease
conflict means "someone else is doing it, look at something else"; a stale fence
means "you lost your claim, re-read the task". Adapters render these as structured
data with the code intact (`docs/PROTOCOL.md` §9), because a coordinating agent
needs to branch on the reason, and a string it has to pattern-match is a protocol
failure.
"""

from __future__ import annotations

from typing import Any


class RoomError(Exception):
    """Base class. `code` is the stable protocol identifier; `message` is prose."""

    code = "room_error"
    status_code = 400

    def __init__(self, message: str, **details: Any) -> None:
        super().__init__(message)
        self.message = message
        self.details = details

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"ok": False, "error": self.code, "message": self.message}
        if self.details:
            payload["details"] = self.details
        return payload


class Unauthenticated(RoomError):
    code = "unauthenticated"
    status_code = 401


class Forbidden(RoomError):
    code = "forbidden"
    status_code = 403


class PaymentRequired(RoomError):
    """The caller is authenticated but lacks the paid creator entitlement."""

    code = "payment_required"
    status_code = 402


class NotFound(RoomError):
    """Also returned when a thing exists but is invisible to this participant —
    distinguishing the two would leak the existence of other tenants' rooms."""

    code = "not_found"
    status_code = 404


class RoomClosed(RoomError):
    code = "room_closed"
    status_code = 409


class InvalidCommand(RoomError):
    code = "invalid_command"
    status_code = 422


class LeaseConflict(RoomError):
    """Another participant holds a valid lease. Includes who and until when, so the
    caller can decide to wait, pick different work, or raise it in the room."""

    code = "lease_conflict"
    status_code = 409


class LeaseRequired(RoomError):
    """The caller holds no lease on work that may only be finished by its holder.

    Distinct from `lease_conflict` on purpose. "Someone else holds this" and "you
    hold nothing" call for different responses — wait versus claim — and collapsing
    them into one code would leave an agent retrying against a lease that does not
    exist. Absence of a holder is an authorization failure, not a vacuous success
    (D-027).
    """

    code = "lease_required"
    status_code = 409


class AmbiguousExecutor(RoomError):
    """Several runtimes of this seat are connected and none was named.

    Refusing is the point. The alternative — picking the most recently active
    connection — would silently record the wrong executor, and every later
    affinity check would then be answered about a runtime that is not doing the
    work. A wrong answer that looks authoritative is worse than a refusal that
    says exactly what to send (D-034).
    """

    code = "executor_ambiguous"
    status_code = 409


class ExecutorConflict(RoomError):
    """Another live runtime of the same seat is executing this lease.

    Not a lease conflict: the caller *does* hold the lease. What it does not hold
    is the work, which a sibling runtime started and may still be performing
    outside this system. The fence protects room state; it cannot recall an
    external action already in flight (D-035).
    """

    code = "executor_conflict"
    status_code = 409


class SteeringHalted(RoomError):
    """A human paused or stopped this work, so the worker may not proceed.

    Enforced rather than advertised. The alternative — returning the directive as
    a field and trusting the runtime to honour it — makes human control a
    convention, and this codebase has now three times replaced a convention with a
    constraint after discovering the convention was not being kept.
    """

    code = "steering_halted"
    status_code = 409


class StaleFence(RoomError):
    """The caller's fence is behind the task's. It lost the lease and must re-read."""

    code = "stale_fence"
    status_code = 409


class StaleRuntime(RoomError):
    """This runtime was drained. It may still be running; it may no longer act.

    The one guarantee a coordination server can actually make about a process it
    does not own. Containment primitives — process groups, job objects, cgroups —
    all assume the runtime is on our machine, and in the hosted product it never
    is. So the server stops trying to end the process and instead refuses its
    work, which needs no cooperation from the process and no privilege on its host.

    Distinct from `stale_fence`, and the distinction is the entire point: a fence
    says another run of the *lease* superseded yours, and re-reading fixes it. This
    says the *runtime* was told to stop, and re-reading fixes nothing — a drained
    runtime that reconnects is the same drained runtime, because the drain is
    sticky and reconnecting is not a way to become allowed again (D-062).
    """

    code = "stale_runtime"
    status_code = 409


class RevisionConflict(RoomError):
    code = "revision_conflict"
    status_code = 409


class ArtifactDivergence(RoomError):
    code = "artifact_divergence"
    status_code = 409


class CapabilityUnsupported(RoomError):
    """The participant's negotiated capabilities (or room policy) do not permit
    this. The message always states which capability was missing."""

    code = "capability_unsupported"
    status_code = 409


class PrivacyViolation(RoomError):
    """The disclosure check rejected the payload. Never a silent scrub: a scrub
    would teach the caller the channel is safe for that content
    (`docs/SECURITY.md` §2)."""

    code = "privacy_violation"
    status_code = 422


class InvalidCursor(RoomError):
    code = "invalid_cursor"
    status_code = 400


class ResumeGap(RoomError):
    """Cursor is below the room's retained floor; the client must re-snapshot."""

    code = "resume_gap"
    status_code = 409


class RateLimited(RoomError):
    code = "rate_limited"
    status_code = 429
