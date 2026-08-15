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
import json
import os
import pathlib
import time
import urllib.error
import urllib.request
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


def describe(event: dict[str, Any]) -> str:
    """One line for a human reading the room over someone's shoulder."""
    payload = event.get("payload") or {}
    detail = ""
    for field_name in DETAIL_FIELDS:
        value = payload.get(field_name)
        if isinstance(value, str) and value.strip():
            detail = " ".join(value.split())
            break
    actor = (event.get("actor") or {}).get("display_name") or "room"
    stamp = local_time(str(event.get("ts") or event.get("created_at") or ""))
    line = f"`{event.get('seq'):>4}` **{stamp}** {actor} · `{event.get('type')}`"
    return f"{line} — {detail[:150]}" if detail else line


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
    return "\n".join(lines) + "\n"


def main() -> int:
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
        "--feed-length",
        type=int,
        default=25,
        help="How many recent events to keep in the readable feed.",
    )
    args = parser.parse_args()

    # Never an argument: a token on a command line is readable from any process
    # listing for the life of the process (D-058).
    token = os.environ.get("AGENT_ROOMS_TOKEN")
    if not (args.room and args.out and token):
        parser.error("need --room, --out, and AGENT_ROOMS_TOKEN in the environment")

    out = pathlib.Path(args.out)
    connection_id = ""
    # Bounded, because this is a window on the room and not a second copy of the log —
    # the event log is the source of truth and lives on the server.
    feed: collections.deque[str] = collections.deque(maxlen=args.feed_length)
    cursor = 0
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
            fresh = call(
                args.base,
                args.room,
                token,
                "GET",
                f"/events?since_seq={cursor}&limit=60",
            )
            for event in fresh.get("events") or []:
                feed.append(describe(event))
                cursor = max(cursor, int(event.get("seq") or cursor))
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
        tmp = out.with_suffix(".tmp")
        tmp.write_text(json.dumps(state), encoding="utf-8")
        # Atomic on Windows too: the status line must never read a half-written file.
        tmp.replace(out)

        # A second, human-readable copy. The status line only renders in the terminal
        # UI, so a supervisor working in an editor panel needs something they can keep
        # open in a split — an editor reloads a changed file on disk, which makes an
        # ordinary markdown file a live dashboard with no extension to install.
        if args.markdown:
            md = pathlib.Path(args.markdown)
            md_tmp = md.with_suffix(".tmp")
            md_tmp.write_text(as_markdown(state), encoding="utf-8")
            md_tmp.replace(md)

        time.sleep(args.interval)


if __name__ == "__main__":
    raise SystemExit(main())
