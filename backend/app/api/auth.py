"""Principal extraction for the HTTP transport.

The transport's only job is to turn a bearer token into a principal or a
participant. Every authorization decision past that point lives in `core/authz.py`,
so HTTP, MCP, and A2A cannot drift apart on who may do what.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Header

from ..core import rooms, store
from ..core.errors import Forbidden, Unauthenticated
from ..domain.room import Participant


def _bearer(authorization: str | None, explicit: str | None) -> str:
    token = explicit
    if not token and authorization and authorization.lower().startswith("bearer "):
        token = authorization[7:].strip()
    if not token:
        raise Unauthenticated("Missing token. Send it as `Authorization: Bearer <token>`.")
    return token


async def current_principal(
    authorization: Annotated[str | None, Header()] = None,
    x_arp_token: Annotated[str | None, Header()] = None,
) -> rooms.Principal:
    """An org-level principal: a user or an agent identity. Not yet room-scoped."""
    return await rooms.authenticate_principal(_bearer(authorization, x_arp_token))


async def join_credential(
    authorization: Annotated[str | None, Header()] = None,
    x_arp_token: Annotated[str | None, Header()] = None,
) -> rooms.Principal | rooms.InvitationCredential:
    """What may be presented to *join* a room: an account credential, or an invitation.

    Only this endpoint accepts the second kind, and that narrowness is the security
    property. An invitation authorizes one act — entering the room it names — so widening
    `current_principal` to accept one would have handed every principal-scoped endpoint
    (create a room, list the org's rooms) to anyone holding a link.

    Tried in order, principal first, because a principal token is the common case and an
    invitation is only meaningful here. A token that is neither fails as unauthenticated
    with the same message either way, so probing learns nothing about which kind it was.
    """
    token = _bearer(authorization, x_arp_token)
    try:
        return await rooms.authenticate_principal(token)
    except Unauthenticated:
        return await rooms.authenticate_invitation(token)


async def current_participant(
    authorization: Annotated[str | None, Header()] = None,
    x_participant_token: Annotated[str | None, Header()] = None,
) -> Participant:
    """A room-scoped participant, resolved from its participant token.

    Participant tokens are scoped to one room and revoked on leave, so possession of
    one cannot be replayed against a different room.
    """
    return await store.load_participant_by_token(_bearer(authorization, x_participant_token))


async def stream_participant(
    authorization: Annotated[str | None, Header()] = None,
    x_participant_token: Annotated[str | None, Header()] = None,
    token: str | None = None,
) -> Participant:
    """Participant resolution for the SSE endpoint, which also accepts `?token=`.

    The browser `EventSource` API cannot set request headers, so this one endpoint
    takes the participant token as a query parameter. The exposure is bounded — the
    token is scoped to a single room and revoked on leave — but it does reach server
    access logs, so a cookie-based stream session is on the M5 list. Every other
    endpoint is header-only.
    """
    return await store.load_participant_by_token(
        _bearer(authorization, x_participant_token or token)
    )


PrincipalDep = Annotated[rooms.Principal, Depends(current_principal)]
JoinCredentialDep = Annotated[
    "rooms.Principal | rooms.InvitationCredential", Depends(join_credential)
]
ParticipantDep = Annotated[Participant, Depends(current_participant)]
StreamParticipantDep = Annotated[Participant, Depends(stream_participant)]


def require_user(principal: rooms.Principal):
    """Some operations (creating a room) are a human's to perform."""
    if principal.user is None:
        raise Forbidden("This operation requires a user principal, not an agent identity.")
    return principal.user


def require_identity(principal: rooms.Principal):
    if principal.identity is None:
        raise Forbidden("This operation requires an agent identity principal.")
    return principal.identity
