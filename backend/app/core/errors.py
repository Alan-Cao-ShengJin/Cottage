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


class StaleFence(RoomError):
    """The caller's fence is behind the task's. It lost the lease and must re-read."""

    code = "stale_fence"
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
