"""Durable progress on a task, so a restart is not an amnesia (D-050).

Before this, a worker counted its steps in local memory. A process restart lost
them and no other participant could see them at all — so "what has it actually
done?" was answerable only by the worker itself, and only while it lived. That is
the opposite of live shared work awareness.

**Two audiences, deliberately separated.** A checkpoint carries a `summary` that
the room may read and an optional `resume_state` that only the seat that wrote it
needs. They are not the same thing wearing one field: the summary is coordination —
what was done, what it means, what is next — while the resume state is a machine's
bookmark, useful to the runtime and noise to everyone else.

**What a checkpoint is never.** Not a scratchpad, not chain-of-thought, not a
transcript, not private agent memory. `docs/SECURITY.md` forbids all four from
crossing the boundary, and a progress record is the most natural-looking place to
start leaking them. The schema below is closed and narrow *because* the pressure to
widen it will be constant: the field an executor most wants is "everything I was
thinking", and that field must not exist.

The narrowness is not the control — free text can carry anything, and
`check_disclosure` is the boundary (`docs/SECURITY.md` §2). But a closed schema
means widening is a deliberate act with a diff, rather than a dict quietly growing
a key.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

#: A room-visible progress note. Long enough to say what was done and what is next;
#: far too short to paste a transcript into without it being obvious.
MAX_SUMMARY_CHARS = 1200
MAX_RESUME_ITEMS = 40
MAX_RESUME_ITEM_CHARS = 200


class ResumeState(BaseModel):
    """A runtime's bookmark: enough to pick the work back up, and nothing else.

    Every field answers "where was I?". None of them answers "what was I thinking?",
    and that distinction is the whole design. `extra="forbid"` so a client cannot
    smuggle a fifth key past a schema that looks restrictive.
    """

    model_config = ConfigDict(extra="forbid")

    #: A short label for the stage of work, e.g. `analysing` or `applying-edits`.
    phase: str = Field(default="", max_length=120)
    #: Ids of steps this runtime considers finished. Ids, not narratives.
    completed_step_ids: list[str] = Field(default_factory=list, max_length=MAX_RESUME_ITEMS)
    #: References to artifacts already produced — ids and paths, never contents.
    artifact_refs: list[str] = Field(default_factory=list, max_length=MAX_RESUME_ITEMS)
    #: Named calls the runtime believes are in flight, so a restart can decide
    #: whether to re-issue them. Names and ids: the fence, not the payload.
    pending_tool_calls: list[str] = Field(default_factory=list, max_length=MAX_RESUME_ITEMS)
    #: The single next thing this runtime intends to do.
    next_action: str = Field(default="", max_length=MAX_RESUME_ITEM_CHARS)


class Checkpoint(BaseModel):
    """One append-only progress record on one task.

    Append-only in the strong sense: there is no update path and no delete path.
    A checkpoint that could be edited would be a claim about the past that the past
    does not support, and the whole value here is that the sequence is evidence.
    """

    id: str
    room_id: str
    task_id: str
    #: The seat that recorded it. Attribution is per participant, never per runtime,
    #: because a companion worker and its chat surface are one accountable party.
    participant_id: str
    #: Which runtime of that seat wrote it, when there is a durable one. This is the
    #: honest answer to "was this the worker or the human's session?" (D-044).
    attachment_id: str | None = None
    #: The lease generation this was written under. A checkpoint from a superseded
    #: generation is still true history; the fence is what says which run it belongs to.
    fence: int
    #: Room-visible outcome. What was done, what it means, what is next.
    summary: str
    #: Same-seat bookmark, or `None`. Absent from every projection this participant
    #: does not own.
    resume_state: ResumeState | None = None
    seq: int
    created_at: str
