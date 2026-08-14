"""The explicit disclosure boundary.

Every piece of content a participant contributes to a room crosses a boundary out
of that participant's private context. Domain shape alone cannot police this: a
message body, a task description, a state value, or an artifact summary is
free-form, and any of them can carry a credential, a chunk of a private file, or
another client's context (`docs/SECURITY.md` §2).

So a disclosure is *modeled*, not assumed. A content-bearing command carries a
`Disclosure`; `core.privacy.check_disclosure` turns it into a `DisclosureDecision`
by running, in order:

1. **Authorization** — may this participant assert this class in this room?
2. **Policy** — does the room's visibility and policy permit this class and audience?
3. **Inspection** — does the content trip a hard rule (secret shapes, size caps)?

The decision is stamped onto the resulting event, so what was disclosed, by whom,
to whom, and under what class is permanently auditable.
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field

from .room import PrivacyClass


class Audience(str, Enum):
    """Who the author intends to receive this content."""

    #: Everyone in the room whose class filter admits it.
    ROOM = "room"
    #: One named participant (plus room admins of the owning org, per §6).
    PARTICIPANT = "participant"
    #: Participants belonging to the room's owning org. Internal rooms only.
    ORG = "org"


class Disclosure(BaseModel):
    """The author's explicit statement about content it is contributing."""

    privacy_class: PrivacyClass = PrivacyClass.ROOM_PUBLIC
    audience: Audience = Audience.ROOM
    #: Required when `audience` is PARTICIPANT.
    to_participant_id: str | None = None
    #: Free-text label for where this content came from, e.g. "repo:api/routes.py",
    #: "customer ticket 4192", "my own analysis". Recorded in provenance; it is a
    #: claim by the author, not a verified fact.
    source: str | None = None


class DisclosureDecision(BaseModel):
    """The server's ruling, stamped onto the event. Not client-supplied."""

    privacy_class: PrivacyClass
    audience: Audience
    to_participant_id: str | None = None
    #: Participants the payload may reach, resolved at decision time when the
    #: audience is narrower than the room. `None` means "apply the class filter".
    restricted_to_participant_ids: list[str] | None = None
    #: Which checks ran and passed, for audit readability.
    checks_passed: list[str] = Field(default_factory=list)


class Provenance(BaseModel):
    """Attribution for a shared assertion. `docs/PROTOCOL.md` §6.

    `asserted_by_participant_id` and `asserted_at` are stamped server-side and
    cannot be forged. Everything else is the author's claim, and is labeled as
    such — attribution is the control here, not verification (`docs/SECURITY.md`
    §1).
    """

    asserted_by_participant_id: str
    asserted_at: str
    source: str | None = None
    confidence: float | None = None
    #: State keys or artifact versions this was derived from.
    derived_from: list[str] = Field(default_factory=list)
    #: True when the asserting identity is untrusted; renders as "unverified".
    unverified: bool = False
