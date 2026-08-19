"""What an event costs a reader: a wake, a line in a file, or nothing.

This exists because a push transport and a *wake* channel are not the same thing.
Delivering every visible frame to a socket is correct for a browser building a live
view of the board — it renders them and costs nothing to render. Delivering every
visible frame to a model-backed agent bills someone for each one, so a host that
turns frames into model turns needs the room to say which frames were worth a turn.

Three classes, decided **here in code and never by a model**: asking a model to read
every line in order to decide whether that line was worth reading is precisely the
cost this module exists to avoid (`docs/PRODUCT.md` §9).

The rule that shapes the rest: everything unlisted is `ROUTINE`, so a newly added
event type shows up in a feed rather than silently disappearing from one. The failure
mode worth engineering against is a relay that quietly stops mentioning the thing that
mattered — not one that occasionally says too much.

Kept transport-neutral and stdlib-only on purpose. The same judgement has to be
callable from the WebSocket fanout in `api.routes`, from `core.projections`, and from
`scripts/room_watcher.py`, which runs standalone with no access to this package. The
duplication with that script is pinned by a test rather than trusted.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from enum import Enum
from typing import Any

from .events import EventType


class RelevanceClass(str, Enum):
    """Ordered by what delivery costs the reader."""

    #: Someone must decide something. Worth waking a supervisor, and worth the tokens.
    JUDGEMENT = "judgement"
    #: Worth showing when asked for. It renders into a view and stops there.
    ROUTINE = "routine"
    #: Not worth a line. It would crowd out the feed it sits in.
    NOISE = "noise"


#: Events that need a person or an agent to *decide* something: an instruction aimed at
#: us, an unanswered question, a clash, work offered to us, a lease we just lost, a peer
#: that vanished mid-task.
JUDGEMENT_TYPES: frozenset[EventType] = frozenset(
    {
        # Someone is telling us to do something, or asking.
        EventType.DIRECTIVE_ISSUED,
        EventType.TASK_STEERED,
        EventType.QUESTION_ASKED,
        EventType.QUESTION_ANSWERED,
        # Work is being offered to us, or taken away.
        EventType.TASK_PROPOSED,
        EventType.TASK_CANCELLED,
        # Execution moved between runtimes of one seat. The holder did not change, so
        # nothing else in the log tells an executor it is no longer the executor.
        EventType.TASK_EXECUTOR_CHANGED,
        # We lost a lease we thought we held. Nothing else in the log says so.
        EventType.TASK_CLAIM_EXPIRED,
        # Two participants disagree about the same thing.
        EventType.CONFLICT_DETECTED,
        EventType.ARTIFACT_DIVERGENCE_DETECTED,
        # Progress has stopped and nobody has said why.
        EventType.TASK_AWAITING_INPUT,
        EventType.TASK_BLOCKED,
        EventType.WORK_STALE,
        # A peer is gone. What it was holding is now nobody's.
        EventType.PARTICIPANT_LEFT,
        EventType.ROOM_CLOSED,
        # This runtime was told to stop. It must not keep working, and the decision is
        # the only notice it gets — the room never learns whether the process ended.
        EventType.RUNTIME_DRAINED,
    }
)

#: Presence is noise while a participant re-confirms it is here, and coordination news
#: when it *stops* being here. Suppressing the whole event type throws away every peer
#: disconnect, which is exactly the transition a supervisor needs to act on.
PRESENCE_WORTH_WAKING: frozenset[str] = frozenset({"disconnected", "stale", "idle"})

#: Types split by content rather than by name, because the room stores no structured
#: "did it work" flag for either: a checkpoint carries free-text `summary` and a
#: completion a free-text `result`, so the prose is the only evidence there is.
CONTENT_JUDGED_TYPES: frozenset[EventType] = frozenset(
    {EventType.TASK_CHECKPOINTED, EventType.TASK_COMPLETED}
)

#: Free-text fields that may carry a report of trouble.
OUTCOME_FIELDS = ("result", "summary", "outcome", "status", "error", "reason", "note")

#: Deliberately over-broad. A false wake costs one model call; a missed "the gate is
#: red" costs a supervisor who believes work is progressing while it is not. Where cost
#: and coverage conflict here, coverage wins — that is the whole point of the relay.
TROUBLE = re.compile(
    r"\b("
    r"fail\w*|error\w*|block\w*|broke\w*|break\w*|"
    # Contractions and their spelled-out forms both: a worker writing up a failure picks
    # either, and the two are the same report.
    r"cannot|can't|couldn't|could not|gave up|giving up|unable|stuck|abort\w*|crash\w*|"
    r"denied|reject\w*|refus\w*|invalid|missing|timeout|timed out|"
    r"traceback|exception|regress\w*|conflict\w*|revert\w*|"
    r"red|failing|unresolved|no-go"
    r")\b",
    re.IGNORECASE,
)


def reports_trouble(payload: Mapping[str, Any]) -> bool:
    """Does this payload say something went wrong?

    Structural first — an explicit ``ok: false`` needs no guessing. Then the words,
    because the room's own schema has no field for "it worked".
    """
    for flag in ("ok", "success", "succeeded", "passed"):
        if payload.get(flag) is False:
            return True
    for field_name in OUTCOME_FIELDS:
        value = payload.get(field_name)
        if isinstance(value, str) and TROUBLE.search(value):
            return True
    return False


def classify(
    *,
    event_type: EventType | str,
    payload: Mapping[str, Any] | None = None,
    actor_participant_id: str | None = None,
    viewer_participant_id: str | None = None,
) -> RelevanceClass:
    """Decide what one event costs its reader.

    Takes primitives rather than an envelope so the same judgement is callable from a
    transport that holds parsed models and from one holding decoded JSON.

    The genuinely contested case is ``task.checkpointed``, and the ruling is: **a
    checkpoint is routine unless it reports trouble.** A checkpoint is progress, and
    progress arriving every few minutes is exactly the drip that makes a relay
    expensive — but "the gate failed" and "I am blocked" arrive as checkpoints too, and
    those are the most important thing a supervisor can be told. ``task.completed`` is
    split the same way for the same reason.

    What a reader loses under that rule, stated plainly because it is a real loss: a
    checkpoint reporting failure in words `TROUBLE` does not contain ("the numbers came
    back lower than we hoped") is classed routine and does not wake anyone until the
    reader next looks. It is never dropped — a pull of the log is still complete — but
    its *delivery* becomes pull rather than push.
    """
    kind = event_type.value if isinstance(event_type, EventType) else str(event_type)
    body: Mapping[str, Any] = payload or {}

    if kind == EventType.ATTACHMENT_REGISTERED.value:
        # Fires whenever any participant reattaches; several a minute in a busy room.
        return RelevanceClass.NOISE
    if kind == EventType.PRESENCE_CHANGED.value:
        liveness = str(body.get("liveness") or "")
        return (
            RelevanceClass.JUDGEMENT if liveness in PRESENCE_WORTH_WAKING else RelevanceClass.NOISE
        )
    if kind == EventType.ACTIVITY_NOTED.value:
        # Live narration (D-082). It is the feed, not news; the coordination view
        # already suppresses it, and waking a model per breadcrumb is the whole
        # anti-pattern.
        return RelevanceClass.NOISE
    if (
        kind == EventType.MESSAGE_POSTED.value
        and viewer_participant_id
        and actor_participant_id == viewer_participant_id
    ):
        # Something this seat said, read back to it. Messages only: a checkpoint or a
        # task change from the same seat comes from its *companion* runtime, which is
        # news to the supervisor even though the room attributes both to one
        # participant.
        return RelevanceClass.NOISE

    if kind in {t.value for t in JUDGEMENT_TYPES}:
        return RelevanceClass.JUDGEMENT
    if kind in {t.value for t in CONTENT_JUDGED_TYPES}:
        return RelevanceClass.JUDGEMENT if reports_trouble(body) else RelevanceClass.ROUTINE
    if kind == EventType.MESSAGE_POSTED.value:
        # Free-form text from another participant. There is no schema to reason from,
        # which is precisely why a model rather than a template has to read it.
        return RelevanceClass.JUDGEMENT
    return RelevanceClass.ROUTINE


def wakes(
    *,
    event_type: EventType | str,
    payload: Mapping[str, Any] | None = None,
    actor_participant_id: str | None = None,
    viewer_participant_id: str | None = None,
) -> bool:
    """Is this event worth spending a reader's turn on?"""
    return (
        classify(
            event_type=event_type,
            payload=payload,
            actor_participant_id=actor_participant_id,
            viewer_participant_id=viewer_participant_id,
        )
        is RelevanceClass.JUDGEMENT
    )
