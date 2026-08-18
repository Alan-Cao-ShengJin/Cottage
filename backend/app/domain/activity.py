"""Live activity: what a participant is doing *right now* (D-082).

The gap this fills, stated precisely, because most of what looks like it is missing
already exists under another name:

| what a watcher wants to see | already an event |
|---|---|
| an agent arrived | `participant.joined`, `presence.changed` |
| it took a task | `task.claimed` |
| what it is working on | `work.declared` / `work.updated` |
| durable progress on that task | `task.checkpointed` (D-050) |
| it is stuck | `task.blocked`, `task.awaiting_input` |
| it finished | `task.completed` |
| it went quiet | `presence.changed` |

What none of those carry is the *narration between them*. A work card is an upsert —
one per participant, changed when the participant decides its headline is wrong. A
checkpoint is durable progress against a held lease, and deliberately expensive: it
carries resume state and is fenced. Between claiming a task and checkpointing it, a
worker can spend ten minutes doing real things and the room shows a single unchanged
line. To a human watching, an agent that is working looks identical to one that has
died, which is the product failure this exists to close.

So: a note is a **breadcrumb, not a state change**. Three properties follow, and each
is a deliberate restriction rather than an omission.

**It writes no mutable projection.** There is no `activity` table. The event log is
the feed, and a fresh human snapshot derives the latest visible note per runtime
directly from that log. A dropped realtime delivery is recovered by cursor replay;
every genuine coordination state change remains the event that already models it.

**Phase is a closed enum, and the prose is separate.** A UI must be able to render
"Working" without parsing a sentence, and a sentence must never be able to invent a
state. Free text decides nothing here.

**There is no field for reasoning, and that is the point.** `summary` is what a
person would say out loud across a desk — "running the backend tests", "found a
reconnect bug in the companion loop". `AppendCheckpointCommand` makes the same
argument about `resume_state`: the field an executor most wants to add is "everything
I was thinking", and that field must not exist. A high-frequency narration channel is
the single most inviting place in this product to paste a chain of thought, so every
note goes through `check_disclosure` like any other free text, and the schema offers
nowhere to put reasoning even when the disclosure check would have allowed it.
"""

from __future__ import annotations

from enum import Enum


class ActivityPhase(str, Enum):
    """What kind of moment this note marks.

    Closed on purpose. A phase the room does not know is a phase the UI cannot
    render and a watcher cannot filter, so a new one is a protocol change rather
    than a string an agent may invent.
    """

    #: Actively doing the work. The ordinary case, and the one that makes a room
    #: look alive between the events that change state.
    WORKING = "working"
    #: About to run something external — a test suite, a build, a search. Named
    #: separately from `working` because "started X" and "finished X" bracket a
    #: duration a watcher can show as still running.
    TOOL_STARTED = "tool_started"
    TOOL_FINISHED = "tool_finished"
    #: Stopped on something that needs another party. `task.blocked` and
    #: `task.awaiting_input` remain the durable statements; this is the narration
    #: for a worker not holding a task, or for the moment before it files one.
    BLOCKED = "blocked"
    #: Alive, holding nothing, waiting for work. Distinct from presence `idle`,
    #: which is a *transport* grade meaning "has not beaten within one interval".
    #: A worker can be perfectly live and have nothing to do; conflating the two is
    #: what makes a healthy idle companion read as a dying one.
    MONITORING = "monitoring"
    #: Finished a unit of work. `task.completed` remains the durable statement when
    #: a task was involved.
    COMPLETED = "completed"
    #: Tried and could not. Room-visible because a silent failure is the worst thing
    #: a watcher can be shown, and because the next participant needs to know before
    #: it retries the same thing.
    FAILED = "failed"


#: Phases that mean "this participant is currently doing something". A UI renders
#: these as an in-flight row; everything else is a moment that has passed.
IN_FLIGHT_PHASES: frozenset[ActivityPhase] = frozenset(
    {ActivityPhase.WORKING, ActivityPhase.TOOL_STARTED}
)

#: Bounded low, unlike a message. A note is one line of narration, and a field that
#: comfortably fits an essay is a field that will eventually receive one.
MAX_ACTIVITY_SUMMARY_CHARS = 280
#: Enough to name a tool and its target ("pytest backend/tests"), not to carry a
#: command line with an embedded credential.
MAX_ACTIVITY_TOOL_CHARS = 80
