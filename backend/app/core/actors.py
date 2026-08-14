"""Event actor construction.

Its own module so that every service can attribute an event without importing
`core.rooms`, which would create an import cycle (rooms → work → rooms).
"""

from __future__ import annotations

from ..domain.events import EventActor
from ..domain.room import Participant

#: The room acting on its own behalf: a reaper expiring a lease, a janitor closing
#: an expired room. A `None` participant is meaningful — it distinguishes "the
#: system did this" from "somebody did this", which matters when reading an audit
#: trail to work out why a claim vanished.
SYSTEM_ACTOR = EventActor(participant_id=None, display_name="room")


def actor_for(participant: Participant) -> EventActor:
    return EventActor(
        participant_id=participant.id,
        display_name=participant.identity.display_name,
        kind=participant.identity.kind,
        org_id=participant.org_id,
    )
