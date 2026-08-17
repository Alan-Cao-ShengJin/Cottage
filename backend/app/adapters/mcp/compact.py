"""Compact projections for MCP tool responses.

Every tool response is text the calling model reads, so it is spent context — and for a
`human_turn_only` client that context belongs to a human's subscription. Measured on a
modest room (4 participants, 3 work declarations, 12 tasks), the full projections cost
~3,400 tokens for `get_room_state` and ~5,100 for a `since_seq=0` replay, with a single
join-read-claim turn at ~7,300. That is not a rounding error; it is most of a small
context window spent on fields no agent reads.

So the adapter presents a *coordination view*: what a participant needs to decide what to
do next, and nothing else. Full fidelity stays one parameter away (`detail="full"`), and
the ARP HTTP surface is unchanged — a browser rendering a board legitimately wants
everything.

This is presentation, not policy: no field is withheld for authorization reasons here.
Privacy filtering already happened in `core.projections`, which is the only place allowed
to make that decision.
"""

from __future__ import annotations

from typing import Any

#: Cap on events returned by one poll. A client that has been away can page with the
#: cursor rather than receive an unbounded replay it will mostly ignore.
DEFAULT_MAX_EVENTS = 40

#: Messages are an annotation channel, so a room read defaults to the last few rather than
#: the full backlog.
DEFAULT_MAX_MESSAGES = 5


def _runtimes(presence: dict[str, Any]) -> list[dict[str, Any]]:
    """Which runtimes of this seat are live, and what each says it is.

    Included in the coordination view because "this participant is live" is the wrong
    answer once a seat is a chat window plus a background worker: whether to expect a
    prompt reply depends on which of them is live, not on whether one of them is
    (D-054).

    `declared` is kept as a nested object rather than flattened, so a reader cannot
    mistake a self-report for something the room observed. `live` beside it is
    derived and is the only fact here the room stands behind.
    """
    out: list[dict[str, Any]] = []
    for runtime in presence.get("runtimes") or []:
        declared = runtime.get("declared") or {}
        entry: dict[str, Any] = {
            "ref": runtime.get("ref"),
            "liveness": runtime.get("liveness"),
        }
        if runtime.get("label"):
            entry["label"] = runtime["label"]
        said = {
            key: declared.get(key)
            for key in ("role", "executor_kind", "model")
            if declared.get(key) and declared.get(key) != "unspecified"
        }
        if said:
            entry["declared"] = said
        out.append(entry)
    return out


def participant(row: dict[str, Any]) -> dict[str, Any]:
    """Who is here, how reachable they are, and whether they can take work.

    Omits scopes, trust internals, connection counts, and delivery-mode lists: an agent
    deciding whether to hand over a task needs `liveness` and `may_claim`, not the
    negotiation detail behind them.
    """
    presence = row.get("presence") or {}
    runtime = presence.get("runtime") or {}
    out = {
        "participant_id": row["id"],
        "name": (row.get("identity") or {}).get("display_name"),
        "org": (row.get("identity") or {}).get("org_name"),
        "liveness": presence.get("liveness", "disconnected"),
    }
    if runtime:
        out["may_claim"] = runtime.get("may_claim")
        if not runtime.get("may_claim") and runtime.get("claim_denied_reason"):
            out["cannot_claim_because"] = runtime["claim_denied_reason"]
    if row.get("role") == "owner":
        out["role"] = "owner"
    if (row.get("trust") or "member") != "member":
        out["trust"] = row["trust"]
    # Only when it changes how the name should be read. A coordinating agent deciding
    # whether to trust "Alice's Deploy Bot" needs to know nobody vouched for that name;
    # saying so for every participant would be noise, and noise gets skimmed past.
    if (row.get("identity") or {}).get("name_is_self_asserted"):
        out["name_is_self_asserted"] = True
    # Only when there is more than one, for the same reason: a seat with a single
    # runtime is fully described by `liveness` above, and repeating it per runtime
    # would spend the reader's context to say nothing (D-054).
    runtimes = _runtimes(presence)
    if len(runtimes) > 1:
        out["runtimes"] = runtimes
    return out


def work(row: dict[str, Any]) -> dict[str, Any]:
    """A current-work declaration. `targets` is kept in full: it is the collision key."""
    out = {
        "work_id": row["id"],
        "by": row["participant_id"],
        "headline": row["headline"],
        "status": row["status"],
        "targets": row.get("targets") or [],
    }
    if row.get("stale"):
        out["stale"] = True
    if row.get("note"):
        out["note"] = row["note"]
    return out


def task(row: dict[str, Any]) -> dict[str, Any]:
    """A task board entry, with the lease facts that decide whether it is available."""
    out = {
        "task_id": row["id"],
        "title": row["title"],
        "status": row["status"],
        "targets": row.get("targets") or [],
    }
    if row.get("priority"):
        out["priority"] = row["priority"]
    # A stopped task has no claim, so `status` alone reads as `open` — available.
    # It is not: claiming it is refused. Omitting this made the compact board say
    # the opposite of the truth about the single most consequential state a human
    # can put a task into (D-045).
    steering = row.get("steering")
    if steering and steering != "running":
        out["steering"] = steering
        out["steering_reason"] = row.get("steering_reason") or ""
        out["claimable"] = False
    claim = row.get("claim")
    if claim:
        out["held_by"] = claim["participant_id"]
        # The fence and expiry are the operative facts: one is required for every later
        # mutation, the other says when the work becomes reclaimable.
        out["fence"] = claim["fence"]
        out["lease_expires_at"] = claim["expires_at"]
    if row["status"] == "waiting_input":
        # Same class of defect as the steering omission above: `waiting_input` with no
        # claim would otherwise read like an ordinary unheld task to anything that
        # only looks at holders, when in fact claiming it is refused (D-051).
        out["claimable"] = False
    if row.get("result"):
        out["result"] = row["result"]
    return out


def question(row: dict[str, Any]) -> dict[str, Any]:
    """An unanswered question, with the one fact that decides urgency.

    `blocking` is what separates "somebody wondered something" from "a piece of work
    is stopped until you reply", and it is the field a reader most needs first.
    """
    out = {
        "question_id": row["id"],
        "from": row["asked_by_participant_id"],
        "body": row["body"],
        "blocking": bool(row.get("blocking")),
    }
    if row.get("to_participant_id"):
        out["asked_of"] = row["to_participant_id"]
    if row.get("task_id"):
        out["task_id"] = row["task_id"]
    return out


def conflict(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "conflict_id": row["id"],
        "kind": row["kind"],
        "detail": row["detail"],
        "involves": row.get("participant_ids") or [],
    }


def message(row: dict[str, Any]) -> dict[str, Any]:
    out = {"from": row.get("participant_id"), "body": row["body"], "seq": row.get("seq")}
    if row.get("to_participant_id"):
        out["direct_to"] = row["to_participant_id"]
    if row.get("about_ref"):
        out["about"] = row["about_ref"]
    return out


def room_state(
    snapshot: dict[str, Any],
    *,
    max_messages: int = DEFAULT_MAX_MESSAGES,
) -> dict[str, Any]:
    """The coordination view of a room.

    Deliberately drops the room's policy/retention blocks, per-participant scope lists,
    and event-log bookkeeping. `cursor` is kept because it is what the polling loop needs.
    """
    room = snapshot.get("room") or {}
    you = snapshot.get("you") or {}
    messages = snapshot.get("messages") or []

    state: dict[str, Any] = {
        # Kept whole and kept first. Everything else in this view is compacted or
        # dropped to save the caller's context; an instruction addressed to the caller
        # is the one thing that must survive the trim, and it must be the first thing
        # read (D-045).
        "directives_for_you": snapshot.get("directives_for_you") or [],
        "room": {
            "room_id": room.get("id"),
            "name": room.get("name"),
            "purpose": room.get("purpose"),
            "charter": room.get("charter"),
            "status": room.get("status"),
            "visibility": room.get("visibility"),
        },
        "you": you.get("participant_id"),
        "cursor": snapshot.get("snapshot_seq"),
        "participants": [
            participant(p) for p in snapshot.get("participants") or [] if p.get("state") == "joined"
        ],
        "current_work": [work(w) for w in snapshot.get("work") or []],
        "tasks": [task(t) for t in snapshot.get("tasks") or []],
    }

    open_questions = snapshot.get("open_questions") or []
    if open_questions:
        # Second only to directives. A worker that stood down on a blocking question
        # is stopped until somebody answers, and a coordination view that shows the
        # parked task but not the reason makes the room look broken rather than
        # waiting (D-051).
        state["open_questions"] = [question(q) for q in open_questions]

    open_conflicts = [c for c in snapshot.get("conflicts") or [] if c.get("status") == "open"]
    if open_conflicts:
        state["open_conflicts"] = [conflict(c) for c in open_conflicts]

    if messages:
        recent = messages[-max_messages:]
        state["recent_messages"] = [message(m) for m in recent]
        if len(messages) > len(recent):
            state["older_messages_omitted"] = len(messages) - len(recent)

    return state


#: Payload fields worth surfacing per event type. Anything not listed is dropped, because
#: the full envelope repeats identity and policy blocks the client already has.
_EVENT_FIELDS: dict[str, tuple[str, ...]] = {
    "participant.joined": ("participant_id", "display_name", "role"),
    "participant.left": ("participant_id", "reason"),
    "presence.changed": ("participant_id", "liveness"),
    "message.posted": ("participant_id", "body", "to_participant_id", "about_ref"),
    "work.declared": ("work_id", "participant_id", "headline", "targets", "status"),
    "work.updated": ("work_id", "headline", "status", "targets"),
    "work.ended": ("work_id", "reason"),
    "work.stale": ("work_id", "participant_id", "reason"),
    "task.created": ("task_id", "title", "status", "targets", "priority"),
    "task.updated": ("task_id", "title", "status"),
    "task.claimed": ("task_id", "participant_id", "fence", "expires_at"),
    "task.claim_renewed": ("task_id", "fence", "expires_at"),
    "task.claim_released": ("task_id", "participant_id", "reason"),
    "task.claim_expired": ("task_id", "participant_id", "reason"),
    "task.completed": ("task_id", "participant_id", "result"),
    "task.cancelled": ("task_id", "reason"),
    "task.proposed": ("proposal_id", "task_id", "to_participant_id", "note"),
    "task.checkpointed": ("task_id", "participant_id", "summary", "has_resume_state"),
    "question.asked": ("question_id", "task_id", "to_participant_id", "body", "blocking"),
    "question.answered": ("question_id", "task_id", "body", "asked_by_participant_id"),
    "task.awaiting_input": ("task_id", "question_id", "participant_id"),
    "conflict.detected": ("conflict_id", "kind", "detail"),
    "conflict.resolved": ("conflict_id", "resolution"),
    "room.closed": ("reason",),
}


def event(envelope: dict[str, Any]) -> dict[str, Any]:
    """One event, reduced to what a coordinating agent acts on.

    Unknown types keep their payload rather than losing it: forward compatibility matters
    more than a few tokens, and a silently emptied event would be worse than a verbose one.
    """
    type_ = envelope.get("type", "")
    payload = envelope.get("payload") or {}
    fields = _EVENT_FIELDS.get(type_)

    if fields is None:
        kept = payload
    else:
        kept = {k: payload[k] for k in fields if k in payload and payload[k] not in (None, "", [])}

    out: dict[str, Any] = {
        "seq": envelope.get("seq"),
        "type": type_,
        "actor": (envelope.get("actor") or {}).get("display_name"),
    }
    if kept:
        out.update(kept)
    return out


def events(
    envelopes: list[dict[str, Any]], *, max_events: int = DEFAULT_MAX_EVENTS
) -> tuple[list[dict[str, Any]], int]:
    """Compact and cap a batch. Returns `(events, dropped_count)`.

    Keeps the *newest* when over the cap, since a client that has been away cares about
    the current state of play more than the beginning of what it missed — and the cursor
    still lets it page back if it wants.
    """
    if len(envelopes) <= max_events:
        return [event(e) for e in envelopes], 0
    kept = envelopes[-max_events:]
    return [event(e) for e in kept], len(envelopes) - len(kept)
