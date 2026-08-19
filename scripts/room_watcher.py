#!/usr/bin/env python
"""Keep a control surface live in a room, and keep a summary of the room on disk.

Two jobs, and the first one is the point.

**Staying live.** A chat-shaped client exists only during a tool call, so between calls
its seat drops to `disconnected` — a supervisor that is very much still supervising reads
as gone. That is only unavoidable for hosts with no runtime at all (a browser tab). Any
host that runs on a machine can hold a small process open, and then its liveness says what
is true: *this participant is being watched by something that will notice*. The division
of labour is that the companion's liveness means "it is working" and this one's means "it
is tracking" — two runtimes of one seat (D-044), which is exactly the shape the room was
built for.

Capabilities are declared for what this process actually does: it receives events and
polls. It does **not** claim `can_execute_background`, because it executes nothing — the
companion attached to the same seat does that, and the room grades a seat from its best
live attachment rather than from a promise made here (principle 5).

**Reporting.** A human supervising through a chat surface has no window into the room
except what their agent happens to mention, and silence reads as a dead room while a
companion works. The JSON feeds a terminal status line; the markdown copy is for keeping
open in an editor split, since an editor reloads a changed file on disk.

**And doing both cheaply.** We do not pay for inference — our users do, from their own
subscriptions (`docs/PRODUCT.md` §9). This process's stdout is wired to a host that turns
each line into a model wake-up, so every line printed here spends someone else's money.
Events are therefore split in *code* into two classes: `routine` ones render into the files
below, which cost nothing to write and nothing to read; only `judgement` ones reach stdout,
batched so that a poll carrying five of them is one wake rather than five. The counters
that prove it are written into the status file — a relay that cannot report its own wake
rate is not known to be cheap, only not known to be expensive.

Pass `--read-only` to go back to observing without attaching — useful for watching a room
you do not want to appear in.

Pair with `scripts/room_statusline.py`, which renders the file this writes. Two
processes rather than one because a status line is rendered many times a second and
must never make a network call.

    set AGENT_ROOMS_TOKEN=...        (participant token; never passed as an argument)
    python scripts/room_watcher.py --room room_... --out %TEMP%\\room_status.json
"""

from __future__ import annotations

import argparse
import collections
import contextlib
import json
import os
import pathlib
import re
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

DEFAULT_BASE = "https://agent-rooms.fly.dev"


#: What this process can honestly do. No `can_execute_background`: it tracks, it does
#: not work. Overstating here would have the room route work to a seat on the strength
#: of a runtime that cannot take it.
WATCH_CAPABILITIES = [
    "can_receive_events",
    "supports_poll",
    "supports_resume",
]


def call(
    base: str,
    room: str,
    token: str,
    method: str,
    path: str,
    payload: dict[str, Any] | None = None,
    *,
    timeout: int = 30,
) -> dict[str, Any]:
    req = urllib.request.Request(
        f"{base}/api/rooms/{room}{path}",
        data=json.dumps(payload).encode() if payload is not None else None,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        method=method,
    )
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return json.load(response)


def read_room(base: str, room: str, token: str, *, timeout: int = 20) -> dict[str, Any]:
    return call(base, room, token, "GET", "/snapshot", timeout=timeout)


def attach(base: str, room: str, token: str, label: str) -> str:
    """Register a durable runtime for this seat and return its connection id.

    `runtime_role: control_surface` because that is what this is — the supervising
    side of the seat, kept reachable — and saying so stops a reader mistaking a
    tracking process for the one doing the work (D-054).
    """
    result = call(
        base,
        room,
        token,
        "POST",
        "/connect",
        {
            "host_class": "persistent_local",
            "capabilities": WATCH_CAPABILITIES,
            "transport": "long_poll",
            "attachment_label": label,
            "attachment_resumable": True,
            "runtime_role": "control_surface",
            "executor_kind": "none",
        },
    )
    return str(result["connection_id"])


def summarize(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Reduce a snapshot to what fits on one line of a status bar."""
    participants = snapshot.get("participants") or []
    live = [
        p
        for p in participants
        if (p.get("presence") or {}).get("liveness")
        in {"live_poll", "live_push", "attended"}
    ]
    tasks = snapshot.get("tasks") or []
    active = [t for t in tasks if t.get("status") == "in_progress"]
    waiting = [t for t in tasks if t.get("status") == "waiting_input"]

    headline = ""
    for work in snapshot.get("work") or []:
        if work.get("status") == "active":
            headline = work.get("headline", "")
            break

    return {
        "at": time.time(),
        "seq": snapshot.get("snapshot_seq"),
        "participants": len(participants),
        "live": len(live),
        "in_progress": len(active),
        "waiting_input": len(waiting),
        "open_questions": len(snapshot.get("open_questions") or []),
        "conflicts": len(snapshot.get("conflicts") or []),
        "headline": headline[:60],
    }


#: Three classes, decided here in code and never by a model — deciding what matters by
#: asking a model to read every line is the exact cost `docs/PRODUCT.md` §9 forbids.
JUDGEMENT = "judgement"  # worth waking a supervisor: it needs a decision
ROUTINE = "routine"  # worth showing: it renders into the files and stops there
NOISE = "noise"  # not worth a line: it would crowd out the feed it sits in

#: Events that need a person or an agent to *decide* something: an instruction aimed at
#: us, an unanswered question, a clash, work offered to us, a lease we just lost, a peer
#: that vanished mid-task. Everything not listed is routine, so a new event type shows up
#: in the file rather than silently disappearing — the failure mode to avoid is a relay
#: that stops mentioning the thing that mattered, not one that says too much.
JUDGEMENT_TYPES = frozenset(
    {
        # Someone is telling us to do something, or asking.
        "directive.issued",
        "task.steered",
        "question.asked",
        "question.answered",
        # Work is being offered to us, or taken away.
        "task.proposed",
        "task.cancelled",
        # Execution moved between two runtimes of one seat. The holder did not change,
        # so nothing else tells an executor it is no longer the executor.
        "task.executor_changed",
        # This runtime was told to stop. The decision is the only notice it gets —
        # the room never learns whether the process ended (D-062).
        "presence.runtime_drained",
        # We lost a lease we thought we held. Nothing else in the log says so.
        "task.claim_expired",
        # Two participants disagree about the same thing.
        "conflict.detected",
        "artifact.divergence_detected",
        # Progress has stopped and nobody has said why.
        "task.awaiting_input",
        "task.blocked",
        "work.stale",
        # A peer is gone. What it was holding is now nobody's.
        "participant.left",
        "room.closed",
    }
)

#: Presence is noise when a participant is merely re-confirming that it is here, and is
#: coordination news when it *stops* being here. Suppressing the whole event type — which
#: this did until the Codex participant reviewed it at seq 203 — throws away every peer
#: disconnect, which is precisely the event a supervisor needs to act on.
PRESENCE_WORTH_WAKING = frozenset({"disconnected", "stale", "idle"})

#: Terminal states meaning the work did not land, read structurally before the prose: the
#: lexical path catches `failed` and `rejected` only by accident and misses `cancelled`
#: entirely. Mirrors `domain/relevance.FAILED_STATES`.
FAILED_STATES = frozenset({"failed", "rejected", "cancelled", "abandoned"})

#: Events whose relevance depends on whom they name, mapped to the field naming them.
#: Mirrors `domain/relevance.ADDRESSED_JUDGEMENT_FIELDS`. Not in `JUDGEMENT_TYPES`, because
#: that set is unconditional and a room-wide allocation must not wake every reader.
ADDRESSED_JUDGEMENT_FIELDS = {
    "supervisor.goal_replaced": "target_supervisor_participant_id",
    "supervisor.goal_closed": "participant_id",
    "job.assigned": "assigned_to_participant_id",
}

#: Hierarchy events that churn like presence: a line per capacity report and a line per
#: declared worker state change would make this file unreadable. Mirrors
#: `domain/relevance.HIERARCHY_NOISE_TYPES`.
HIERARCHY_NOISE_TYPES = frozenset(
    {"supervisor.capacity_changed", "worker.state_changed"}
)

#: Free-text fields that may contain a report of trouble. A checkpoint's `summary` and a
#: completion's `result` are prose: the room stores no structured "did it work" flag, so
#: whether an event reports failure can only be read out of the words (see below).
OUTCOME_FIELDS = ("result", "summary", "outcome", "status", "error", "reason", "note")

#: Deliberately over-broad. A false wake costs one model call; a missed "the gate is red"
#: costs a supervisor who thinks work is progressing while it is not. When cost and
#: coverage conflict here, coverage wins — that is the whole point of the relay.
TROUBLE = re.compile(
    r"\b("
    r"fail\w*|error\w*|block\w*|broke\w*|break\w*|"
    # The contractions and their spelled-out forms both, because a worker writing up a
    # failure picks either and the two are the same report. `couldn't` was here without
    # `could not`, so "gave up, could not reach the room" classified as routine.
    r"cannot|can't|couldn't|could not|gave up|giving up|unable|stuck|abort\w*|crash\w*|"
    r"denied|reject\w*|refus\w*|invalid|missing|timeout|timed out|"
    r"traceback|exception|regress\w*|conflict\w*|revert\w*|"
    r"red|failing|unresolved|no-go"
    r")\b",
    re.IGNORECASE,
)


def reports_trouble(payload: dict[str, Any]) -> bool:
    """Does this payload say something went wrong?

    Structural first — an explicit `ok: false` or `success: false` is unambiguous and
    needs no guessing. Then the words, because the room's own schema has no field for
    "it worked": a checkpoint carries a free-text `summary` and a completion a free-text
    `result`, so for those two the prose is the only evidence there is.
    """
    for flag in ("ok", "success", "succeeded", "passed"):
        if payload.get(flag) is False:
            return True
    state = payload.get("state")
    if isinstance(state, str) and state in FAILED_STATES:
        return True
    for field_name in OUTCOME_FIELDS:
        value = payload.get(field_name)
        if isinstance(value, str) and TROUBLE.search(value):
            return True
    return False


def classify(event: dict[str, Any], *, me: str = "") -> str:
    """Decide what an event costs: a wake, a line in a file, or nothing.

    The one genuinely contested case is `task.checkpointed`, and the ruling is: **a
    checkpoint is routine unless it reports trouble.** A checkpoint is progress, and
    progress arriving every few minutes is exactly the drip that makes a relay expensive
    — but "the gate failed" and "I am blocked" arrive as checkpoints too, and those are
    the single most important thing a supervisor can be told. So it is split by content
    rather than by type. `task.completed` is split the same way, for the same reason.

    What a reader loses under this rule, stated plainly because it is a real loss: a
    checkpoint that reports failure in words `TROUBLE` does not contain ("the numbers
    came back lower than we hoped") renders into `ROOM.md` and the status file, and does
    not wake anyone until the supervisor next looks. It is never dropped — the file is
    complete — but its *delivery* is pull rather than push. The mitigations are that the
    vocabulary is over-broad on purpose, and that the counters make the ratio of routine
    to judgement visible, so a relay that has gone quiet can be seen to have gone quiet.
    """
    kind = str(event.get("type") or "")
    payload = event.get("payload") or {}

    if kind == "presence.attachment_registered":
        # Fires every time any participant reattaches; several a minute in a busy room.
        return NOISE
    if kind == "presence.changed":
        liveness = str(payload.get("liveness") or "")
        return JUDGEMENT if liveness in PRESENCE_WORTH_WAKING else NOISE
    if (
        kind == "message.posted"
        and me
        and (event.get("actor") or {}).get("participant_id") == me
    ):
        # Something I said, read back to me. Only messages: a checkpoint or a task
        # change from this same seat comes from the *companion* runtime, which is news
        # to the supervisor even though the room attributes it to one participant.
        return NOISE

    if kind in HIERARCHY_NOISE_TYPES:
        return NOISE
    addressed_field = ADDRESSED_JUDGEMENT_FIELDS.get(kind)
    if addressed_field is not None:
        # Judgement only when it names this reader. `me` is empty when the caller did not
        # say who it is, and then this renders rather than waking - the safe direction for a
        # reader that cannot identify itself.
        return JUDGEMENT if me and payload.get(addressed_field) == me else ROUTINE
    if kind in JUDGEMENT_TYPES:
        return JUDGEMENT
    if kind in ("task.checkpointed", "task.completed", "worker.finished"):
        return JUDGEMENT if reports_trouble(payload) else ROUTINE
    if kind == "message.posted":
        # Free-form text from another participant. There is no schema to reason from,
        # which is precisely why a model rather than a template has to read it.
        return JUDGEMENT
    return ROUTINE


#: Fields worth showing per event, in the order they are worth trying. An event whose
#: payload has none of these is still listed by type — knowing that a lease moved matters
#: even when there is no sentence to print about it.
DETAIL_FIELDS = (
    "summary",
    "body",
    "headline",
    "title",
    "note",
    "reason",
    "result",
    "error",
)


def local_time(iso: str) -> str:
    """UTC on the wire, the reader's clock on the page.

    The room stamps `ts` in UTC, which is right for a log and wrong for a person
    glancing at a feed — an event eight hours in the "past" reads as stale history
    rather than as something that just happened.
    """
    if not iso:
        return "--:--:--"
    try:
        when = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return "--:--:--"
    return when.astimezone().strftime("%H:%M:%S")


#: Whether the files this writes may contain what people actually said.
#:
#: Off, and the default matters more than the feature. This process writes to a status
#: file and a markdown file that live outside the repository, unencrypted, for as long as
#: nobody deletes them. An ACL audit found the markdown copy readable by every local user
#: and writable by any authenticated one — so a room's prose, from a room with more than
#: one organisation in it, sat on disk with weaker protection than the room itself
#: enforces. `docs/SECURITY.md` says free-text bodies can carry anything; nothing about
#: being useful to glance at makes that untrue.
#:
#: With this off a reader still learns everything coordination needs — who acted, when,
#: on what, and how much they said — and none of what was said. Turning it on is an
#: explicit statement that this machine is a fine place for other people's words.
INCLUDE_CONTENT = os.environ.get("AGENT_ROOMS_INCLUDE_CONTENT", "").lower() in {
    "1",
    "true",
    "yes",
}


def describe(event: dict[str, Any]) -> str:
    """One line for a human reading the room over someone's shoulder."""
    payload = event.get("payload") or {}
    detail = ""
    for field_name in DETAIL_FIELDS:
        value = payload.get(field_name)
        if isinstance(value, str) and value.strip():
            if INCLUDE_CONTENT:
                detail = " ".join(value.split())
            else:
                # The shape of what was said, never the words. Length is deliberate:
                # "someone posted 4000 characters" is a coordination signal on its own,
                # and it is not a disclosure of any of them.
                detail = f"<{field_name}, {len(value)} chars>"
            break
    actor = (event.get("actor") or {}).get("display_name") or "room"
    stamp = local_time(str(event.get("ts") or event.get("created_at") or ""))
    line = f"`{event.get('seq'):>4}` **{stamp}** {actor} · `{event.get('type')}`"
    return f"{line} — {detail[:150]}" if detail else line


#: The typographic characters this module actually emits, and their ASCII readings. Named
#: rather than folded blindly so a dash stays a dash instead of becoming `?`. Written as
#: escapes because the literal glyphs are what this table exists to eliminate — a source
#: file that has to survive a cp1252 editor should not depend on its own bytes.
ASCII_READINGS = str.maketrans(
    {
        "·": "|",  # middle dot, from describe()'s actor separator
        "—": "-",  # em dash, from describe()'s detail separator
        "–": "-",  # en dash
        "‘": "'",  # curly quotes, which arrive in copied-in prose
        "’": "'",
        "“": '"',
        "”": '"',
    }
)


def plain(event: dict[str, Any]) -> str:
    """The same line without markdown, and ASCII-only, for a stdout relay.

    A relay line crosses a pipe into a host that may decode it as anything; the middle
    dot arrived as a replacement character the first time this ran. Encoding has cost
    this project a mangled checkpoint, a rejected secret and a dead status line already
    - a status line is not the place to keep testing it.

    The promise is kept at this boundary rather than at each call site. An earlier version
    of this function claimed "ASCII-only" while stripping three markdown characters and
    one middle dot, so the em dash `describe` puts before every detail went straight out
    to the pipe. Substituting the characters we know, then folding whatever is left, means
    a *new* non-ASCII character in someone's display name degrades instead of escaping.
    """
    line = describe(event).replace("`", "").replace("**", "").translate(ASCII_READINGS)
    return line.encode("ascii", "replace").decode("ascii")


def write_private(path: pathlib.Path, text: str) -> None:
    """Write a file only its owner can read, where the OS lets us say that.

    An ACL audit of the files this process writes found ROOM.md readable by every local
    user and writable by any authenticated one, inherited from the directory it happened
    to be created in. Redacting the content (see `INCLUDE_CONTENT`) removes most of what
    was worth reading; this narrows who can read the rest.

    POSIX gets 0600 at creation rather than a chmod afterwards, because a chmod leaves a
    window in which the file exists with the default mode and this file is rewritten
    every few seconds - a small window multiplied by a lot of rewrites.

    Windows is the honest gap. `os.chmod` there toggles a read-only bit and says nothing
    about the ACL, which is inherited from the containing directory, so on Windows the
    protection is *where you point `--out`*, not what this function does. Said plainly
    rather than papered over with a chmod that would look like a control and be none.
    """
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    descriptor = os.open(path, flags, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)
    except BaseException:
        with contextlib.suppress(OSError):
            os.close(descriptor)
        raise


#: Separator inside a coalesced wake. It has to be one *line*, not one block: the host
#: reading this stdout makes a wake-up per line, so two lines are two wakes no matter how
#: related they are.
BATCH_SEPARATOR = " ;; "


def render_batch(events: list[dict[str, Any]]) -> str:
    """Every judgement event from one poll, as a single line."""
    lines = [plain(event) for event in events]
    if len(lines) == 1:
        return lines[0]
    return f"[{len(lines)} events] " + BATCH_SEPARATOR.join(lines)


@dataclass
class RelayCounters:
    """The numbers `docs/PRODUCT.md` §9 says a supervisor must be able to report.

    Three of the four live here. The fourth — duplicate claims prevented — is a property
    of the room's lease table, not of this relay; counting it from a client's view of the
    event stream would be a guess presented as a measurement, so it is deliberately
    absent rather than approximated.
    """

    started_at: float = field(default_factory=time.time)
    #: Lines actually written to stdout. This is the number that costs money.
    wakes: int = 0
    #: Judgement events seen, whether or not `--emit` was on to deliver them.
    judgement_events: int = 0
    #: Events that rendered into the files and woke nobody. The savings.
    routine_events: int = 0
    noise_events: int = 0
    #: Judgement events actually delivered; `emitted - wakes` is what batching saved.
    emitted_events: int = 0
    wake_bytes_total: int = 0
    wake_bytes_max: int = 0
    wake_bytes_last: int = 0

    def record_wake(self, line: str, count: int) -> None:
        size = len(line.encode("utf-8", "replace"))
        self.wakes += 1
        self.emitted_events += count
        self.wake_bytes_total += size
        self.wake_bytes_last = size
        self.wake_bytes_max = max(self.wake_bytes_max, size)

    def report(self, *, now: float | None = None) -> dict[str, Any]:
        elapsed = max((now if now is not None else time.time()) - self.started_at, 1.0)
        return {
            "uptime_s": round(elapsed),
            "wakes": self.wakes,
            "wakes_per_hour": round(self.wakes * 3600.0 / elapsed, 2),
            "judgement": self.judgement_events,
            "routine": self.routine_events,
            "noise": self.noise_events,
            # Events that arrived alongside another judgement event and so cost no
            # extra wake. Zero wakes for five routine events does not show up here —
            # that is `routine`, which is the larger saving of the two.
            "coalesced": self.emitted_events - self.wakes,
            "bytes_last": self.wake_bytes_last,
            "bytes_max": self.wake_bytes_max,
            "bytes_mean": round(self.wake_bytes_total / self.wakes) if self.wakes else 0,
        }


def relay(
    events: list[dict[str, Any]],
    *,
    me: str = "",
    counters: RelayCounters,
    emit: bool = True,
) -> tuple[list[str], str | None]:
    """One poll's events, split into what renders and what wakes.

    Returns the lines to append to the readable feed, and at most one line for stdout —
    at most, because three routine events are three lines in a file and zero wakes, and
    two judgement events in the same poll are one wake carrying both.
    """
    shown: list[str] = []
    waking: list[dict[str, Any]] = []
    for event in events:
        klass = classify(event, me=me)
        if klass == NOISE:
            counters.noise_events += 1
            continue
        shown.append(describe(event))
        if klass == JUDGEMENT:
            counters.judgement_events += 1
            waking.append(event)
        else:
            counters.routine_events += 1

    if not (waking and emit):
        return shown, None
    line = render_batch(waking)
    counters.record_wake(line, len(waking))
    return shown, line


def as_markdown(state: dict[str, Any]) -> str:
    """The same reading, for a human keeping a file open in a split pane."""
    stamp = time.strftime("%H:%M:%S", time.localtime(state.get("at") or time.time()))
    if state.get("error"):
        return f"# ROOM — unreachable\n\n`{state['error']}` at {stamp}\n"

    lines = [
        f"# ROOM — seq {state.get('seq')}",
        "",
        f"read at **{stamp}**",
        "",
        f"- participants: **{state.get('live', 0)} live** of {state.get('participants', 0)}",
        f"- in progress: **{state.get('in_progress', 0)}**",
    ]
    if state.get("waiting_input"):
        lines.append(f"- ⚠ **{state['waiting_input']} waiting on you**")
    if state.get("open_questions"):
        lines.append(f"- ❓ open questions: **{state['open_questions']}**")
    if state.get("conflicts"):
        lines.append(f"- ✗ conflicts: **{state['conflicts']}**")
    if state.get("headline"):
        lines += ["", f"> {state['headline']}"]

    # Newest first: a feed a human glances at is read from the top, and the thing they
    # need is what just happened, not what happened when they last looked.
    feed = state.get("feed") or []
    lines += ["", "## Live", ""]
    lines += [f"- {line}" for line in reversed(feed)] or ["_nothing yet_"]

    # What this relay cost, on the page rather than only in the JSON. A supervisor
    # reading the room should be able to see the bill without being asked to.
    relay_stats = state.get("relay") or {}
    if relay_stats:
        lines += [
            "",
            "## Cost",
            "",
            f"- model wakes: **{relay_stats.get('wakes', 0)}** "
            f"({relay_stats.get('wakes_per_hour', 0)}/hour)",
            f"- rendered without waking: **{relay_stats.get('routine', 0)}** routine, "
            f"{relay_stats.get('noise', 0)} suppressed",
            f"- coalesced into an existing wake: **{relay_stats.get('coalesced', 0)}**",
            f"- payload per wake: {relay_stats.get('bytes_last', 0)}B last, "
            f"{relay_stats.get('bytes_mean', 0)}B mean, "
            f"{relay_stats.get('bytes_max', 0)}B max",
        ]
    return "\n".join(lines) + "\n"


def main() -> int:
    # Declared up front because the flag's default reads the module value below, and a
    # `global` after that first read is a syntax error rather than a subtle bug - which
    # is the better failure of the two.
    global INCLUDE_CONTENT

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--base", default=os.environ.get("AGENT_ROOMS_BASE", DEFAULT_BASE)
    )
    parser.add_argument("--room", default=os.environ.get("AGENT_ROOMS_ROOM"))
    parser.add_argument("--out", default=os.environ.get("AGENT_ROOMS_STATUS_FILE"))
    # Below the room's 120s work-stale cutoff and its presence grading window with room
    # to miss one and recover, rather than tuned to sit just inside either.
    parser.add_argument("--interval", type=int, default=10)
    parser.add_argument(
        "--markdown",
        default=os.environ.get("AGENT_ROOMS_STATUS_MD"),
        help="Also write a human-readable copy here, to keep open in an editor split.",
    )
    parser.add_argument(
        "--label",
        default=os.environ.get("AGENT_ROOMS_LABEL", "supervisor-watch"),
        help="Attachment label. Stable across restarts, so a restart is the same runtime.",
    )
    parser.add_argument(
        "--read-only",
        action="store_true",
        help="Observe without attaching, leaving this seat's presence untouched.",
    )
    parser.add_argument(
        "--token-file",
        default=os.environ.get("AGENT_ROOMS_TOKEN_FILE"),
        help="Read the participant token from this file. A path is not a secret.",
    )
    parser.add_argument(
        "--from-seq",
        type=int,
        default=None,
        help="Start from this sequence instead of the room's current position. 0 replays.",
    )
    parser.add_argument(
        "--emit",
        action="store_true",
        help=(
            "Print each meaningful event to stdout as it arrives. Intended for a host "
            "that turns stdout lines into notifications, so a supervisor whose turn has "
            "ended can still be woken."
        ),
    )
    parser.add_argument(
        "--include-content",
        action="store_true",
        default=INCLUDE_CONTENT,
        help=(
            "Write what people actually said into the status and markdown files. Off by "
            "default: those files sit outside the repository, unencrypted, for as long "
            "as nobody deletes them, and an audit found the markdown copy readable by "
            "every local user. Without this you still get who, when, what type, and how "
            "many characters -- everything coordination needs and none of the words."
        ),
    )
    parser.add_argument(
        "--feed-length",
        type=int,
        default=25,
        help="How many recent events to keep in the readable feed.",
    )
    args = parser.parse_args()

    # Module-level because `describe` is called from several places and threading a
    # disclosure setting through every one of them is how a redaction gets forgotten in
    # exactly one path. Set once, before anything can render.
    INCLUDE_CONTENT = bool(args.include_content)
    if INCLUDE_CONTENT:
        log_line = (
            "writing ROOM CONTENT to disk by explicit request: these files are "
            "unencrypted and outlive this process. Delete them when done."
        )
        print(f"room_watcher: {log_line}", file=sys.stderr, flush=True)

    # Never an argument: a token on a command line is readable from any process
    # listing for the life of the process (D-058). `--token-file` exists because an
    # inline `VAR=value cmd` prefix is *also* a command line — that is how this very
    # script leaked its own credential, caught by another participant reading the
    # process table rather than by any check of ours.
    token = os.environ.get("AGENT_ROOMS_TOKEN")
    if args.token_file:
        token = pathlib.Path(args.token_file).read_text(encoding="ascii").strip()
    if not (args.room and args.out and token):
        parser.error("need --room, --out, and AGENT_ROOMS_TOKEN in the environment")

    out = pathlib.Path(args.out)
    connection_id = ""
    # Bounded, because this is a window on the room and not a second copy of the log —
    # the event log is the source of truth and lives on the server.
    feed: collections.deque[str] = collections.deque(maxlen=args.feed_length)
    # Start at the room's current position, not at the beginning. A relay that replays
    # the whole log on restart wakes its human with two hundred events they have already
    # seen — which is how a feed teaches someone to ignore it. `--from-seq 0` asks for
    # the replay deliberately; the default is "from now".
    cursor = args.from_seq if args.from_seq is not None else -1
    me = ""
    counters = RelayCounters()
    while True:
        try:
            if not args.read_only and not connection_id:
                connection_id = attach(args.base, args.room, token, args.label)
            if connection_id:
                # Presence is graded on heartbeat age. This is the whole reason the
                # process exists, so it happens before the read rather than after: a
                # slow snapshot must not be what makes the seat look absent.
                call(
                    args.base,
                    args.room,
                    token,
                    "POST",
                    "/heartbeat",
                    {"connection_id": connection_id},
                )
            if cursor < 0:
                # First pass: adopt the room's current position without reporting it.
                snap = read_room(args.base, args.room, token)
                cursor = int(snap.get("snapshot_seq") or 0)
                # `you` is a participant id over MCP and a participant object over
                # REST. Reading it as a string worked, silently produced a value that
                # could never match a participant id, and the self-filter did nothing.
                you = snap.get("you")
                if isinstance(you, dict):
                    me = str(you.get("participant_id") or you.get("id") or "")
                else:
                    me = str(you or "")
            fresh = call(
                args.base,
                args.room,
                token,
                "GET",
                f"/events?since_seq={cursor}&limit=60",
            )
            batch = list(fresh.get("events") or [])
            for event in batch:
                cursor = max(cursor, int(event.get("seq") or cursor))
            shown, wake = relay(batch, me=me, counters=counters, emit=args.emit)
            feed.extend(shown)
            if wake:
                # One line per *poll*, not per event, flushed immediately: this stdout IS
                # the relay. A host that turns lines into notifications can wake a
                # supervisor whose turn has already ended, which is the difference
                # between a durable log and a continuous feed — and each line it reads
                # spends a model call, so a poll that carries three things to decide
                # spends one call and not three.
                print(wake, flush=True)
            cursor = max(cursor, int(fresh.get("current_seq") or cursor))

            state = summarize(read_room(args.base, args.room, token))
            state["feed"] = list(feed)
        except urllib.error.HTTPError as exc:
            # A connection the room has forgotten (restart, reaper, revocation) must be
            # re-established rather than heartbeated forever into nothing.
            if exc.code in {401, 404, 409}:
                connection_id = ""
            state = {"at": time.time(), "error": f"HTTP {exc.code}"}
        except (urllib.error.URLError, TimeoutError) as exc:
            state = {"at": time.time(), "error": type(exc).__name__}
        except Exception as exc:  # noqa: BLE001 - a status line must not take the poller down
            state = {"at": time.time(), "error": type(exc).__name__}
        # On every path, including the error ones: the cost of the relay is a fact about
        # the relay, not about whether the room happened to answer this time.
        state["relay"] = counters.report()
        tmp = out.with_suffix(".tmp")
        write_private(tmp, json.dumps(state))
        # Atomic on Windows too: the status line must never read a half-written file.
        tmp.replace(out)

        # A second, human-readable copy. The status line only renders in the terminal
        # UI, so a supervisor working in an editor panel needs something they can keep
        # open in a split — an editor reloads a changed file on disk, which makes an
        # ordinary markdown file a live dashboard with no extension to install.
        if args.markdown:
            md = pathlib.Path(args.markdown)
            md_tmp = md.with_suffix(".tmp")
            write_private(md_tmp, as_markdown(state))
            md_tmp.replace(md)

        time.sleep(args.interval)


if __name__ == "__main__":
    raise SystemExit(main())
