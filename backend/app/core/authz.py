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

from ..domain.identity import IdentityProvenance, TrustTier
from ..domain.room import (
    ROLE_SCOPES,
    UNTRUSTED_DENIED_SCOPES,
    MembershipState,
    Participant,
    ParticipantRole,
    Room,
    RoomRole,
    Scope,
)
from .errors import Forbidden, InvalidCommand, RoomClosed


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
    if not room.is_writable:
        raise RoomClosed(
            "This room is no longer accepting writes.",
            room_id=room.id,
            status=room.status.value,
            expires_at=room.expires_at,
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


def require_orchestrator(
    participant: Participant,
    room_role: RoomRole,
    *,
    action: str,
    reason: str,
) -> None:
    """The gate on every room-level coordination act (D-088).

    Three conditions, and each is doing separate work — the same three-part shape a
    control directive already uses (D-045), for the same reason:

    * **`room.admin`.** The authority is a *grant*, never an inference from the
      hierarchy label. If the position alone were enough, a coordination role would be
      minting privileges, which is precisely what ADR-013 records going wrong twice in
      one day.
    * **The orchestrator position.** `room.admin` is held by every owner, and a room
      with two owners must still have one coordinator. This is the part that says
      *which* admin coordinates.
    * **A stated reason.** Replacing a supervisor's whole goal, or moving a job away
      from the seat that asked for it, is exactly the kind of act a room needs to be
      able to explain afterwards. An unexplained reallocation is indistinguishable
      from a mistake.

    Note what this deliberately is *not*: permission to act **as** another
    participant. An orchestrator directs supervisors; it never posts as one, never
    reads their private context, and never touches their host. `require_owns` still
    applies everywhere it applied before.
    """
    require_admin(participant)
    if room_role is not RoomRole.ORCHESTRATOR:
        raise Forbidden(
            f"Only the room's orchestrator may {action}. "
            "A supervisor may propose it, and post a job the orchestrator can allocate.",
            room_role=room_role.value,
            required_room_role=RoomRole.ORCHESTRATOR.value,
        )
    if not reason.strip():
        raise InvalidCommand(
            f"A stated reason is required to {action}.",
            action=action,
        )


def require_room_role(
    room_role: RoomRole,
    expected: RoomRole,
    *,
    action: str,
) -> None:
    """Refuse an act that only makes sense from one position in the hierarchy.

    Authority checks belong in `require_orchestrator` and `require_scope`; this one is
    about coherence. A seat with no goal acknowledging a goal, or an observer
    registering workers, is not a privilege violation — it is a caller that has
    misunderstood what it is, and saying so plainly is more useful than a silent no-op.
    """
    if room_role is not expected:
        raise Forbidden(
            f"Only a {expected.value} may {action}.",
            room_role=room_role.value,
            required_room_role=expected.value,
        )


def is_org_member(participant: Participant, room: Room) -> bool:
    """Whether this participant belongs to the room's owning org.

    Tenancy only — a comparison of org ids. It answers "same tenant?", not "is this
    someone the org vouches for?", and `can_see_org_internal` needs the second question.
    """
    return participant.org_id == room.org_id


def can_see_org_internal(participant: Participant, room: Room) -> bool:
    """Whether `org_internal` payloads may be disclosed to this participant.

    Same tenant **and** an identity that an account holder stands behind.

    The second condition is not redundant, and leaving it out was a real hole. An
    invitation link provisions its guest inside the *inviting room's* org (that is where
    the authorization comes from), so a link-holder passes the tenancy check while being
    exactly the stranger `org_internal` exists to exclude. `docs/SECURITY.md` §1 describes
    this tier as "user authenticated into their own org", which a link-holder is not.

    So provenance decides it: an identity created for an account, or bound by one at a
    consent screen, is a member; one provisioned by redeeming a link is a guest, whatever
    org row it happens to live in (D-025).
    """
    return (
        is_org_member(participant, room)
        and participant.identity.provenance == IdentityProvenance.ACCOUNT
    )
