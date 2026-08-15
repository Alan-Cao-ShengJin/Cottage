"""Worker → human, which the control plane deliberately cannot express (D-051).

Directives run one way: a human with `room.admin` steers a participant. That
asymmetry is not an oversight, it is the security property — issuing a directive
requires admin precisely so that a worker cannot manufacture instructions for
itself or anyone else. So "the worker needs to ask something" cannot be a directive
with the target and issuer swapped, however tempting the symmetry looks. It would
hand every worker the power the scope check exists to withhold.

A **question** is therefore its own primitive with its own authority model: any
participant that may speak in the room may ask one, addressed to a participant or
to the room at large. It carries no authority whatsoever, which is exactly why it
needs no admin grant. An **answer** is likewise its own record, linked to the
question — not a reverse directive either, for the same reason.

**Blocking is the interesting case, and it is opt-in.** By default a question
changes nothing: the asker keeps its lease and carries on, because a worker that
halts on every uncertainty cannot work unattended, which is the entire point of
having one. When an asker genuinely cannot proceed it may say so — and then the
room checkpoints the task, moves it to `waiting_input`, and **releases the lease**.
Holding a lease while waiting for a human would let one unanswered question park a
piece of work for as long as nobody happened to read it.
"""

from __future__ import annotations

from pydantic import BaseModel

MAX_QUESTION_CHARS = 2000
MAX_ANSWER_CHARS = 4000


class Question(BaseModel):
    id: str
    room_id: str
    #: Optional in general, **required when blocking**: blocking means "this task
    #: cannot proceed", and a task is the only thing the room knows how to halt.
    task_id: str | None = None
    asked_by_participant_id: str
    #: `None` means the room at large. Addressing narrows who is expected to reply;
    #: it never restricts who *may*, because a question nobody answers is worse than
    #: one answered by the wrong person.
    to_participant_id: str | None = None
    body: str
    #: Whether the asker released its work to wait. Recorded on the question because
    #: an unblocked task with an outstanding blocking question is a state the room
    #: must be able to notice.
    blocking: bool = False
    created_seq: int
    created_at: str
    answered_at: str | None = None
    answered_by_participant_id: str | None = None
    answer_id: str | None = None

    @property
    def is_open(self) -> bool:
        return self.answered_at is None


class Answer(BaseModel):
    """A reply, kept as its own row rather than a column on the question.

    Separate because an answer has its own author, its own timestamp and its own
    place in the log — and because collapsing it into the question would make the
    first reply the only possible one.
    """

    id: str
    room_id: str
    question_id: str
    answered_by_participant_id: str
    body: str
    seq: int
    created_at: str
