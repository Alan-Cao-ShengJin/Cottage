#!/usr/bin/env python
"""Turn a room's push stream into wake-ups for an attended agent.

**What this is for.** A chat-shaped agent exists only during a turn. Between turns
nothing of it is running, so a task proposed to its seat at 3am sits unclaimed until
its human next types. `scripts/room_watcher.py` already relays such events, but it
*polls*: it asks the room every `--interval` seconds whether anything happened. This
does not ask. It holds the room's WebSocket open and is told.

**Why it is cheap.** The socket subscribes with `classes=judgement`, so the server
applies `app/domain/relevance.py` and sends only events that need somebody to decide
something — no keepalives, no presence churn, no activity narration. The filtering is
server-side and decided in code, never by a model: a channel that woke a model to ask
it whether the last frame was worth waking for would cost more than the polling loop
it replaces. An idle room therefore produces no lines, no notifications, and no
tokens, for as long as it stays idle.

**How it wakes anything.** It prints one line per event to stdout and nothing else, so
it can be run under a host that converts stdout lines into notifications:

    Monitor({command: "python scripts/wake_channel.py --room room_...",
             description: "cottage judgement events", persistent: true})

That is the whole mechanism, and it is worth being precise about where the push stops:
the *room* pushes to this process over a socket it holds open. This process pushes a
line to its host. Whether that line becomes a model turn is the host's decision and
not something Cottage can reach — MCP has no server-initiated wake channel (D-005),
and nothing here pretends otherwise.

**Presence.** Pass `--connection-id` to have the socket beat for an existing
connection, the way the browser does. Left out, this is a pure observer: it reads the
room without claiming to be a runtime of the seat, which is the honest default for a
process whose only job is to notice.

    set AGENT_ROOMS_TOKEN=...      (participant token; never passed as an argument)
    python scripts/wake_channel.py --room room_...
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import pathlib
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

DEFAULT_BASE = "https://agent-rooms.fly.dev"

#: Fields worth putting in the one line a reader gets, in the order worth trying. An
#: event with none of them is still announced by type: knowing a lease moved matters
#: even when there is no sentence to print about it.
DETAIL_FIELDS = (
    # First, because a presence wake carries nothing else: "Joiner" alone does not say
    # whether the peer arrived or vanished, and only one of those needs acting on.
    "liveness",
    "summary",
    "body",
    "headline",
    "title",
    "note",
    "reason",
    "result",
    "error",
)

#: Backoff ceiling for reconnects. A wake channel that reconnects hard in a tight loop
#: is a wake channel that gets itself rate-limited off the room it is watching.
MAX_BACKOFF_SECONDS = 30.0


def _force_utf8_stdio() -> None:
    """Room text is arbitrary; a legacy console codepage must not kill the channel.

    Without this, one non-ASCII character in a peer's message raises
    UnicodeEncodeError inside the print that was the whole point of the process.
    """
    for stream in (sys.stdout, sys.stderr):
        with contextlib.suppress(AttributeError, ValueError):
            stream.reconfigure(encoding="utf-8", errors="replace")


def log(message: str) -> None:
    """Diagnostics go to stderr.

    Kept off stdout deliberately: stdout is the event stream, and a host that turns
    each line into a notification would turn "reconnecting" into a wake-up about
    nothing.
    """
    print(f"wake_channel: {message}", file=sys.stderr, flush=True)


def mint_ticket(base: str, room: str, token: str) -> str:
    """Exchange the durable participant credential for a one-use realtime ticket.

    The socket takes its credential in the query string, where it would otherwise
    appear in access logs and proxy history for the life of the connection. The ticket
    is short-lived and single-use, so a leaked URL is worth nothing by the time anyone
    reads it — which is the reason the endpoint exists.
    """
    request = urllib.request.Request(
        f"{base.rstrip('/')}/api/rooms/{room}/stream-ticket",
        method="POST",
        data=b"",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        payload = json.loads(response.read().decode("utf-8"))
    ticket = payload.get("ticket")
    if not ticket:
        raise RuntimeError(f"the room issued no ticket: {payload}")
    return str(ticket)


def socket_url(
    base: str, room: str, *, ticket: str, since_seq: int, connection_id: str
) -> str:
    scheme = "wss" if base.startswith("https") else "ws"
    host = base.split("://", 1)[-1].rstrip("/")
    params: dict[str, str] = {
        "ticket": ticket,
        "since_seq": str(since_seq),
        # The point of the whole script. Without this the socket is the browser's
        # firehose and every keepalive becomes a notification.
        "classes": "judgement",
    }
    if connection_id:
        params["connection_id"] = connection_id
    return f"{scheme}://{host}/api/rooms/{room}/ws?{urllib.parse.urlencode(params)}"


def describe(event: dict[str, Any]) -> str:
    """One line, because one line is one notification.

    Deliberately terse. The reader of this line is about to go and read the room
    properly over its own transport; what it needs from here is enough to decide
    whether to bother, not a copy of the event.
    """
    kind = str(event.get("type") or "event")
    seq = event.get("seq")
    actor = (event.get("actor") or {}).get("display_name") or "room"
    payload = event.get("payload") or {}
    detail = ""
    for field in DETAIL_FIELDS:
        value = payload.get(field)
        if isinstance(value, str) and value.strip():
            detail = " ".join(value.split())
            break
    line = f"[{seq}] {kind} | {actor}"
    if detail:
        line = f"{line} | {detail[:280]}"
    return line


class UnfilteredServer(RuntimeError):
    """The peer accepted `classes=judgement` and then ignored it.

    Found by pointing this script at a deployed instance that predates the filter: the
    query parameter was dropped, every routine event arrived, and three narration notes
    became three wake-ups. The server-side guard only rejects an *unknown value*; it
    cannot help against a server that never learned the parameter exists, and a client
    that assumes otherwise degrades silently into the exact firehose it exists to avoid.

    So the filter must be *confirmed*, never assumed. Two frames prove it was not
    applied, and either is fatal rather than a warning:

    * `snapshot` where a filtered subscription is answered with `ready` — the tell on a
      fresh start (`since_seq=0`).
    * `keepalive`, which a filtered subscription never sends — the tell when resuming
      from a cursor, where no opening frame is sent at all.

    Failing closed is the only safe direction. An unfiltered wake channel does not
    announce itself; it just quietly bills its reader for every heartbeat in the room.
    """


def handle_frame(frame: dict[str, Any], *, cursor: int) -> tuple[int, str | None]:
    """Decide what one frame means: the new cursor, and the line to wake a reader with.

    Pure and separate from the socket so the fail-closed rules can be tested without a
    server. Returns `None` for the line when the frame deserves no wake-up. Raises
    `UnfilteredServer` when the frame proves the filter is not being applied.
    """
    name = frame.get("frame")
    if name == "event":
        event = frame.get("event") or {}
        seq = event.get("seq")
        if isinstance(seq, int):
            cursor = max(cursor, seq)
        return cursor, describe(event)
    if name == "ready":
        cursor = int((frame.get("data") or {}).get("cursor") or cursor)
        log(f"subscribed from seq {cursor}")
        return cursor, None
    if name == "resume_gap":
        # A wake-up, not a log line: losing history is something the reader must act on,
        # and it is the one case where it cannot trust its own cursor.
        return (
            0,
            "[gap] resume_gap | room history was truncated; re-read the room state",
        )
    if name == "snapshot":
        # A filtered subscription is answered with `ready`. A snapshot means the peer
        # does not know `classes` and is streaming the whole room.
        raise UnfilteredServer(
            "the room answered a filtered subscription with a full snapshot, so it is "
            "not applying classes=judgement — refusing to run as an unfiltered firehose"
        )
    if name == "keepalive":
        raise UnfilteredServer(
            "the room sent a keepalive on a filtered subscription, so it is not applying "
            "classes=judgement — refusing to run as an unfiltered firehose"
        )
    # Never silent: an unrecognised frame on a channel whose whole job is deciding what
    # deserves attention is itself worth one line of attention.
    log(f"ignored an unrecognised frame type: {name!r}")
    return cursor, None


async def stream_once(url: str, *, cursor: int) -> int:
    """Hold the socket open, printing judgement events. Returns the cursor reached.

    The cursor is returned rather than stored so a reconnect resumes from what was
    actually delivered. Sequence numbers are the room's, not ours, and the resume
    contract is what keeps a reconnect from either re-announcing an event or silently
    skipping one.

    Raises `UnfilteredServer` if the peer proves it is not honouring the filter.
    """
    import websockets

    async with websockets.connect(url, max_size=None) as socket:
        log("connected; the room will push from here")
        async for raw in socket:
            try:
                frame = json.loads(raw)
            except (TypeError, ValueError):
                log(f"ignored a frame that was not JSON: {raw!r:.120}")
                continue
            cursor, line = handle_frame(frame, cursor=cursor)
            if line is not None:
                print(line, flush=True)
    return cursor


async def run(args: argparse.Namespace, token: str) -> int:
    cursor = args.from_seq if args.from_seq is not None else 0
    backoff = 1.0
    while True:
        try:
            ticket = mint_ticket(args.base, args.room, token)
            url = socket_url(
                args.base,
                args.room,
                ticket=ticket,
                since_seq=cursor,
                connection_id=args.connection_id or "",
            )
            cursor = await stream_once(url, cursor=cursor)
            # A clean close with no error is the room ending the stream — a closed room,
            # or a revoked credential. Reconnecting into that forever is a busy loop
            # against a door that is shut.
            log("the room closed the socket")
            backoff = min(backoff * 2, MAX_BACKOFF_SECONDS)
        except UnfilteredServer as exc:
            log(str(exc))
            log(
                "this server predates classes=judgement. Upgrade it, or run "
                "scripts/room_watcher.py --emit, which filters client-side."
            )
            return 3
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403):
                # No amount of retrying fixes a credential the room has refused.
                log(
                    f"refused ({exc.code}); the participant token is invalid or revoked"
                )
                return 2
            log(f"ticket request failed ({exc.code}); retrying in {backoff:.0f}s")
            backoff = min(backoff * 2, MAX_BACKOFF_SECONDS)
        except (OSError, RuntimeError) as exc:
            log(f"{type(exc).__name__}: {exc}; retrying in {backoff:.0f}s")
            backoff = min(backoff * 2, MAX_BACKOFF_SECONDS)

        if args.once:
            return 0
        await asyncio.sleep(backoff)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--base", default=os.environ.get("AGENT_ROOMS_BASE", DEFAULT_BASE)
    )
    parser.add_argument("--room", default=os.environ.get("AGENT_ROOMS_ROOM"))
    parser.add_argument(
        "--from-seq",
        type=int,
        default=None,
        help=(
            "Resume from this sequence. Omitted, the subscription starts at the room's "
            "current position: a wake channel that replayed history on start would wake "
            "its reader about everything that already happened."
        ),
    )
    parser.add_argument(
        "--connection-id",
        default=os.environ.get("AGENT_ROOMS_CONNECTION_ID"),
        help=(
            "Beat for this existing connection while the socket is open. Omitted, this "
            "is a pure observer and touches no presence."
        ),
    )
    parser.add_argument(
        "--token-file",
        default=os.environ.get("AGENT_ROOMS_TOKEN_FILE"),
        help="Read the participant token from this file. A path is not a secret.",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Exit when the socket closes instead of reconnecting. For testing.",
    )
    args = parser.parse_args(argv)
    _force_utf8_stdio()

    # Never an argument: a token on a command line is readable from any process listing
    # for the life of the process (D-058). `--token-file` exists because an inline
    # `VAR=value cmd` prefix is *also* a command line.
    token = os.environ.get("AGENT_ROOMS_TOKEN")
    if args.token_file:
        token = pathlib.Path(args.token_file).read_text(encoding="ascii").strip()
    if not (args.room and token):
        parser.error("need --room and AGENT_ROOMS_TOKEN in the environment")

    try:
        import websockets  # noqa: F401
    except ImportError:
        parser.error(
            "this needs the `websockets` package: pip install websockets "
            "(it is already present in backend/.venv)"
        )

    with contextlib.suppress(KeyboardInterrupt):
        return asyncio.run(run(args, token))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
