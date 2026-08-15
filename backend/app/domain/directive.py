"""Control-plane directives: how a human steers a running agent (D-045).

Deliberately not a message. The argument that settled it came from the ChatGPT
participant and is about falsifiability rather than taste: prose in the room can be
missed among ordinary messages, processed late, or claimed never to have been seen,
and none of those can be distinguished afterwards. A directive has a target, an
action, an issuing authority and an observation record, so "the worker never saw it"
becomes a fact the room can state instead of an assertion nobody can check.

**Effect and observation are orthogonal**, which is why there is no single lifecycle
enum here. A control action applies the instant it is issued — waiting for the target
to acknowledge would make stopping a runaway worker depend on the cooperation of the
runaway worker. Acknowledgement is then evidence that it noticed, recorded separately,
and `applied but never acknowledged` is a real and important state rather than an
awkward one.

`INPUT` is the exception, and the only one: there is no room state to halt, so nothing
can be applied until the target consumes it. Waiting is intrinsic there rather than a
control failure.
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel


class DirectiveAction(str, Enum):
    #: Keep your place; do not progress. The lease survives.
    PAUSE = "pause"
    #: Stop and let go. Halts the task *and* releases the lease, so the work is not
    #: merely paused behind a holder who has been told to stop.
    STOP = "stop"
    #: Undo a pause or a stop. Work becomes claimable and progressable again.
    RESUME = "resume"
    #: Change what matters most, without taking the work away from whoever has it.
    REPRIORITIZE = "reprioritize"
    #: Give a worker something it asked for, or tell it something it needs. The one
    #: action with no immediate state change, so the one that legitimately waits.
    INPUT = "input"


#: Actions that change room state the moment they are issued.
CONTROL_ACTIONS: frozenset[DirectiveAction] = frozenset(
    {
        DirectiveAction.PAUSE,
        DirectiveAction.STOP,
        DirectiveAction.RESUME,
        DirectiveAction.REPRIORITIZE,
    }
)


class EffectStatus(str, Enum):
    """What happened to the *instruction*, never whether anyone noticed."""

    #: Issued, nothing applied yet. Only reachable for `INPUT`.
    PENDING = "pending"
    APPLIED = "applied"
    #: The target explicitly declined. Recorded rather than argued with: an agent
    #: may refuse, and the room's job is to make the refusal visible.
    REJECTED = "rejected"
    #: A later directive on the same target replaced this one before it was consumed.
    SUPERSEDED = "superseded"


class Directive(BaseModel):
    id: str
    room_id: str
    #: Who is being directed. Always a participant: directives address a seat, not a
    #: runtime, because the human steering does not and should not know which of the
    #: target's runtimes is currently executing.
    target_participant_id: str
    task_id: str | None = None
    action: DirectiveAction
    reason: str = ""
    issued_by_participant_id: str
    #: Derived server-side from the issuer's identity, never accepted from a caller.
    #: **Attribution, not verification**: it records that the issuing identity is a
    #: human principal, which is not the same as a human being present at the time.
    #: Authorization comes from `room.admin`, never from this field.
    human_origin: bool = False
    #: Room seq at issue, so "the worker acted at seq X having been told at seq Y" is
    #: answerable without correlating timestamps across machines.
    created_seq: int
    effect_status: EffectStatus
    created_at: str
    applied_at: str | None = None
    acknowledged_at: str | None = None
    acknowledged_by_participant_id: str | None = None

    @property
    def is_open(self) -> bool:
        """Still wants the target's attention: unacknowledged, or not yet applied."""
        return self.acknowledged_at is None or self.effect_status is EffectStatus.PENDING
