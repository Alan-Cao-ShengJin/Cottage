"""Authorization: scopes, ownership, trust, and room writability.

Every check lives here rather than at the transport edge, so HTTP, MCP, and A2A
inherit identical rules and a new adapter cannot accidentally ship with a gap.

Three checks are distinct and all three are needed — conflating them is the classic
way authorization goes wrong:

* **Scope** — "may this kind of participant do this kind of thing at all?"
* **Ownership** — "is this *their* work declaration / *their* claim / a proposal
  addressed to *them*?" A participant with `work.declare` may not end someone
  else's declaration.
* **Trust** — "is this identity vouched for?" An untrusted identity is denied a
  fixed set of scopes regardless of the role someone granted it.
"""

from __future__ import annotations

from ..domain.identity import TrustTier
from ..domain.room import (
    ROLE_SCOPES,
    UNTRUSTED_DENIED_SCOPES,
    MembershipState,
    Participant,
    ParticipantRole,
    Room,
    RoomStatus,
    Scope,
)
from .errors import Forbidden, RoomClosed


def effective_scopes(
    role: ParticipantRole,
    requested: list[Scope] | None,
    trust: TrustTier,
) -> list[Scope]:
    """Resolve the scopes a participant actually gets.

    Narrowing only: a request may subset the role's defaults but never exceed them,
    so a malformed or hostile invitation cannot mint privileges. Untrusted
    identities then lose the denied set on top of that (`docs/SECURITY.md` §5).
    """
    allowed = set(ROLE_SCOPES[role])
    granted = allowed if requested is None else {s for s in requested if s in allowed}
    if trust == TrustTier.UNTRUSTED:
        granted -= UNTRUSTED_DENIED_SCOPES
    # Keep the role's canonical order so stored scope lists are comparable.
    return [s for s in ROLE_SCOPES[role] if s in granted]


def require_active(participant: Participant) -> None:
    if participant.state != MembershipState.JOINED:
        raise Forbidden(
            "You are not an active participant in this room.",
            state=participant.state.value,
        )


def require_scope(participant: Participant, scope: Scope) -> None:
    require_active(participant)
    if not participant.has(scope):
        raise Forbidden(
            f"This action requires the `{scope.value}` scope.",
            required_scope=scope.value,
            granted_scopes=[s.value for s in participant.scopes],
        )


def require_writable(room: Room) -> None:
    """Reads survive closure; writes do not (`docs/PRODUCT.md` §6)."""
    if room.status != RoomStatus.OPEN:
        raise RoomClosed(
            "This room is no longer accepting writes.",
            room_id=room.id,
            status=room.status.value,
        )


def require_owns(participant: Participant, owner_participant_id: str, *, what: str) -> None:
    """Ownership check, separate from scope.

    Admins are *not* exempt: `room.admin` grants administration, not the ability to
    speak or work as another participant. Rewriting someone else's declaration
    would forge attribution, and attribution is the product's only integrity
    guarantee (`docs/SECURITY.md` §1).
    """
    if participant.id != owner_participant_id:
        raise Forbidden(
            f"You can only modify your own {what}.",
            owner_participant_id=owner_participant_id,
        )


def require_admin(participant: Participant) -> None:
    require_scope(participant, Scope.ROOM_ADMIN)


def is_org_member(participant: Participant, room: Room) -> bool:
    """Whether this participant belongs to the room's owning org.

    Gates `org_internal` disclosure and visibility.
    """
    return participant.org_id == room.org_id


def can_see_org_internal(participant: Participant, room: Room) -> bool:
    return is_org_member(participant, room)
