"""Current-work declarations — the primary surface of the product.

"What am I doing right now" is what makes concurrent work by separately-owned
agents visible enough to divide and de-conflict. A declaration is small and public
on purpose: a headline, a status, and the *targets* it touches. Targets are the
overlap-detection key, so they matter more than the prose.
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field

from .room import PrivacyClass


class WorkStatus(str, Enum):
    ACTIVE = "active"
    PAUSED = "paused"
    BLOCKED = "blocked"
    DONE = "done"


class WorkEndReason(str, Enum):
    COMPLETED = "completed"
    ABANDONED = "abandoned"
    SUPERSEDED = "superseded"
    #: Owner's presence went to `disconnected`; the room ended it for them.
    PRESENCE_LOST = "presence_lost"


class WorkDeclaration(BaseModel):
    id: str
    room_id: str
    participant_id: str
    #: One line, present tense. "Refactoring the auth middleware."
    headline: str
    status: WorkStatus = WorkStatus.ACTIVE
    #: Opaque scoped identifiers this work touches — file paths, artifact ids,
    #: service names, ticket ids. Intersection between two active declarations is
    #: what raises `overlapping_work`.
    targets: list[str] = Field(default_factory=list)
    #: Set when this work is executing a room task.
    task_id: str | None = None
    note: str = ""
    privacy_class: PrivacyClass = PrivacyClass.ROOM_PUBLIC
    started_at: str
    updated_at: str
    #: Last time the owner affirmed this is still happening.
    heartbeat_at: str
    expected_done_by: str | None = None
    ended_at: str | None = None
    end_reason: WorkEndReason | None = None
    #: Owner's presence lapsed; the declaration is shown but not to be trusted as
    #: current. Derived on read, not a durable truth.
    stale: bool = False

    @property
    def is_open(self) -> bool:
        return self.ended_at is None and self.status != WorkStatus.DONE
