#!/usr/bin/env python
"""Turn a room's push stream into wake-ups for an attended agent.

**What this is for.** A chat-shaped agent exists only during a turn. Between turns
nothing of it is running, so a task proposed to its seat at 3am sits unclaimed until
its human next types. `scripts/room_watcher.py` already relays such events, but it
*polls*: it asks the room every `--interval` seconds whether anything happened. This
does not ask. It holds the room's WebSocket open and is told.

**Why it is cheap.** The socket subscribes with `classes=judgement,human_visible`, so the server
applies `app/domain/relevance.py` and sends only events that need somebody to decide
something — no keepalives, no presence churn, no activity narration. The filtering is
server-side and decided in code, never by a model: a channel that woke a model to ask
it whether the last frame was worth waking for would cost more than the polling loop
it replaces. An idle room therefore produces no lines, no notifications, and no
tokens, for as long as it stays idle.

**Two axes, not one threshold.** `judgement` is what a model should think about;
`human_visible` is what a *person* should see even when no model needs to. Only one thing
falls in the second and not the first — another human's words, relayed by their agent — and
it is the reason this script asks for both. Suppressing the wake for chat was correct and
made chat undeliverable: this socket is the only push a resident process holds, so "not
worth a turn" had become "nobody receives it" (D-091). Such lines are marked `[chat]`, so a
host can put them in front of its person without treating them as work.

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

#: How long the relay waits on the room, and on the hook. Short on purpose: a person is
#: watching a cursor, and a relay that hangs is worse for them than one that says it failed.
REQUEST_TIMEOUT_SECONDS = 6.0

#: Cap on one relayed line. The room enforces its own limit; this stops a pasted file from
#: being read as a chat message before it ever leaves the machine.
MAX_RELAY_BYTES = 64 * 1024

#: Default localhost port for the outbound relay. Localhost only — it holds the participant
#: token, so anything that can reach it can post as this seat.
DEFAULT_RELAY_PORT = 8787


def _connection_closed_type() -> type[BaseException]:
    """`websockets.exceptions.ConnectionClosed`, or a type that can never be raised.

    Imported lazily and defensively so this module still loads for `--help` and for tests
    on a machine without `websockets` — the same reason the library is imported inside
    `stream_once` rather than at the top.
    """
    try:
        from websockets.exceptions import ConnectionClosed
    except ImportError:

        class _Never(BaseException):
            pass

        return _Never
    return ConnectionClosed


_ConnectionClosed = _connection_closed_type()

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


class OutboundRelay:
    """A warm connection to the room, and a localhost door onto it.

    Posting a chat line from a fresh process costs ~750ms, measured against the hosted
    instance: 185ms to open TCP, another 210ms for TLS, and only then a request. Roughly half
    the wall clock of typing `>what's up?` was spent establishing a connection that was then
    thrown away (D-091).

    So the connection is held. This runs inside the wake channel rather than as a second
    daemon, deliberately: the channel is already resident for this room, already supervised,
    and already reports its own failures as visible lines. A separate relay would be one more
    process that can die quietly — which is the exact bug this file just had.

    The socket carries events *in*; this carries words *out*. One resident process per room,
    doing both, is the honest shape of "a local presence for this seat".

    **Localhost only, and that is a security boundary rather than a convenience.** The
    listener binds 127.0.0.1 and holds the participant token, so anything that can reach it
    can post as this seat. Binding any other interface would hand that to the network.
    """

    def __init__(self, base: str, room: str, token: str) -> None:
        self._base = base.rstrip("/")
        self._room = room
        self._token = token
        self._connection: Any = None
        self._host = self._base.split("://", 1)[-1].rstrip("/")
        self._https = self._base.startswith("https")

    def _client(self) -> Any:
        import http.client

        if self._connection is None:
            factory = http.client.HTTPSConnection if self._https else http.client.HTTPConnection
            # `http.client` rather than `urllib.request`: it keeps the connection, which is the
            # entire point, and it imports in a fraction of the time.
            self._connection = factory(self._host, timeout=REQUEST_TIMEOUT_SECONDS)
        return self._connection

    def _drop(self) -> None:
        with contextlib.suppress(Exception):
            if self._connection is not None:
                self._connection.close()
        self._connection = None

    def post_message(self, body: str, speaking_as: str) -> dict[str, Any]:
        """Relay one line. Retries once on a dropped keep-alive, then gives up.

        A kept-alive connection is closed by the peer eventually — a deploy, an idle timeout —
        and the first request after that fails on a connection that looked fine. Exactly one
        retry: the second failure is the room, not the socket, and a retry loop here would
        make a person wait while it happened.
        """
        payload = json.dumps(
            {"body": body, "speaking_for": "human", "speaking_as": speaking_as}
        ).encode()
        headers = {
            "Authorization": f"Bearer {self._token}",
            "Content-Type": "application/json",
        }
        path = f"/api/rooms/{urllib.parse.quote(self._room)}/messages"
        for attempt in (1, 2):
            try:
                client = self._client()
                client.request("POST", path, body=payload, headers=headers)
                response = client.getresponse()
                raw = response.read()
                if response.status >= 400:
                    self._drop()
                    return {"ok": False, "error": f"the room refused it ({response.status})"}
                return dict(json.loads(raw))
            except Exception as exc:
                self._drop()
                if attempt == 2:
                    return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        return {"ok": False, "error": "unreachable"}

    def serve(self, port: int, speaking_as: str) -> None:
        """Accept one line of JSON per connection, relay it, answer with the result.

        Blocking, on its own thread. One line in, one line out, connection closed — a protocol
        small enough that the hook calling it needs nothing but `socket` and `json`, which is
        what keeps *its* startup cost near the floor.
        """
        import socket as socket_module
        import threading

        def handle(conn: Any) -> None:
            with conn:
                try:
                    conn.settimeout(REQUEST_TIMEOUT_SECONDS)
                    chunks: list[bytes] = []
                    while b"\n" not in b"".join(chunks):
                        piece = conn.recv(4096)
                        if not piece:
                            break
                        chunks.append(piece)
                        if sum(len(c) for c in chunks) > MAX_RELAY_BYTES:
                            break
                    request = json.loads(b"".join(chunks).split(b"\n", 1)[0] or b"{}")
                    body = str(request.get("body") or "").strip()
                    if not body:
                        conn.sendall(b'{"ok": false, "error": "empty body"}\n')
                        return
                    attribution = str(request.get("speaking_as") or speaking_as)
                    result = self.post_message(body, attribution)
                    # Echoed back so the caller's receipt names what was actually posted. The
                    # hook may have supplied no name and taken this process's default; a
                    # receipt naming somebody else would be a lie in the one line the sender
                    # reads.
                    result.setdefault("speaking_as", attribution)
                    if not result.get("ok"):
                        # Visible, because a chat line that never reached the room is the one
                        # thing a person must not have to infer from silence.
                        log(f"relay failed: {result.get('error')}")
                    conn.sendall(json.dumps(result).encode() + b"\n")
                except Exception as exc:
                    log(f"relay error: {type(exc).__name__}: {exc}")
                    with contextlib.suppress(Exception):
                        conn.sendall(b'{"ok": false, "error": "relay error"}\n')

        listener = socket_module.socket(socket_module.AF_INET, socket_module.SOCK_STREAM)
        listener.setsockopt(socket_module.SOL_SOCKET, socket_module.SO_REUSEADDR, 1)
        try:
            listener.bind(("127.0.0.1", port))
        except OSError as exc:
            # Never fatal: the channel's job is receiving, and the hook falls back to its own
            # request when nothing answers here.
            log(f"outbound relay not listening on {port}: {exc}")
            return
        listener.listen(8)
        log(f"outbound relay ready on 127.0.0.1:{port}")
        while True:
            try:
                conn, _ = listener.accept()
            except OSError:
                return
            threading.Thread(target=handle, args=(conn,), daemon=True).start()


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


def socket_url(base: str, room: str, *, ticket: str, since_seq: int, connection_id: str) -> str:
    scheme = "wss" if base.startswith("https") else "ws"
    host = base.split("://", 1)[-1].rstrip("/")
    params: dict[str, str] = {
        "ticket": ticket,
        "since_seq": str(since_seq),
        # The point of the whole script. Without this the socket is the browser's
        # firehose and every keepalive becomes a notification.
        #
        # `human_visible` is the second half, and it is not an optimisation (D-091):
        # relayed human speech is deliberately not worth a model turn, and this socket is
        # the only push a resident process holds — so without asking for it, "not worth a
        # turn" silently meant "the person it was for never receives it". Requested
        # unconditionally because a wake channel that drops human chat is the bug, not a
        # cheaper mode; a server that does not know the value refuses the subscription,
        # which is the fail-closed behaviour this script already relies on.
        "classes": "judgement,human_visible",
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
    # Chat is marked, and marked as the *person* rather than the seat that carried it. A
    # host reading this puts it in front of its human; treating it as work would be the
    # mistake, and the person's own name is what makes it read as somebody talking rather
    # than as an agent reporting (D-090, D-091).
    if kind == "message.posted" and payload.get("speaking_for") == "human":
        who = str(payload.get("speaking_as") or "").strip() or actor
        said = detail or str(payload.get("body") or "").strip()
        return f"[{seq}] [chat] {who} | {' '.join(said.split())[:280]}"
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


def _close_code(socket: Any) -> int | None:
    """The socket's close code, defensively.

    `websockets` has moved this between attributes across versions, and the reconnect
    decision must not depend on which one this install exposes — guessing wrong here is how
    a relay stops reconnecting.
    """
    for attribute in ("close_code",):
        code = getattr(socket, attribute, None)
        if isinstance(code, int):
            return code
    return None


#: Close codes that mean "come back", not "go away". 1012 is a service restart, which is what
#: a deploy sends; 1001 is going away; 1006 is an abnormal close with no code at all. None of
#: them says the room is finished with this subscriber.
TRANSIENT_CLOSE_CODES = frozenset({1001, 1006, 1012, 1013})


async def stream_once(url: str, *, cursor: int) -> tuple[int, int | None]:
    """Hold the socket open, printing judgement events. Returns the cursor reached.

    The cursor is returned rather than stored so a reconnect resumes from what was
    actually delivered. Sequence numbers are the room's, not ours, and the resume
    contract is what keeps a reconnect from either re-announcing an event or silently
    skipping one.

    Raises `UnfilteredServer` if the peer proves it is not honouring the filter.

    Returns `(cursor, close_code)`. An abrupt close is **not** an error here: a server
    restart sends 1012 and `websockets` raises `ConnectionClosed`, which derives from
    `WebSocketException` and therefore matched none of `run`'s handlers — so the loop
    written specifically to survive a restart never ran, and every deploy permanently killed
    every relay watching (D-091, diagnosed by the Laptop 1 session).

    That failure is worse than a crash on startup, because it is silent in the one direction
    that matters: the relay has already proved it works, so its silence afterwards reads as a
    quiet room rather than as a dead relay. Nobody goes looking.
    """
    import websockets

    async with websockets.connect(url, max_size=None) as socket:
        log("connected; the room will push from here")
        try:
            async for raw in socket:
                try:
                    frame = json.loads(raw)
                except (TypeError, ValueError):
                    log(f"ignored a frame that was not JSON: {raw!r:.120}")
                    continue
                cursor, line = handle_frame(frame, cursor=cursor)
                if line is not None:
                    print(line, flush=True)
        except _ConnectionClosed:
            # **The cursor must leave this function by return, never by exception.**
            #
            # `websockets` splits a close in two: `ConnectionClosedOK` ends the iterator and
            # falls through below, while `ConnectionClosedError` is *raised* out of it. A
            # server restart sends 1012, which is the second kind — so the return statement
            # was unreachable in exactly the case it was written for. The exception reached
            # `run`, `cursor = await stream_once(...)` never assigned, and `cursor` is an int
            # passed by value, so every position this socket had reached was discarded.
            #
            # Two different symptoms follow, and both are the same defect (D-091, diagnosed by
            # the Laptop 1 session after a redeploy):
            #
            # * With an explicit `--from-seq`, the next connect asks for it again and the room
            #   replays everything since — a wall of old chat presented as new.
            # * With the default, `since_seq=0` means "start from now" on a filtered
            #   subscription, so the reconnect **skips whatever arrived while it was gone.**
            #   That is the quieter and worse half: the relay says nothing happened.
            #
            # A relay that replays on reconnect and one that dies silently are the same
            # failure from opposite ends — neither lets a reader trust that what arrived is
            # what is new. One says nothing ever happens, the other says everything just did.
            pass
    return cursor, _close_code(socket)


def start_outbound_relay(args: argparse.Namespace, token: str) -> None:
    """Hold a warm connection for outbound chat, on a daemon thread.

    Started before the socket, so a person typing in the first second is not waiting on a
    WebSocket handshake to finish. Never fatal: if the port is taken or the thread dies, the
    chat hook falls back to opening its own connection, which is the behaviour that already
    worked — just slower.
    """
    if args.relay_port <= 0:
        return
    import threading

    relay = OutboundRelay(args.base, args.room, token)
    threading.Thread(
        target=relay.serve,
        args=(args.relay_port, args.human_name),
        name="cottage-outbound-relay",
        daemon=True,
    ).start()


async def run(args: argparse.Namespace, token: str) -> int:
    start_outbound_relay(args, token)
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
            cursor, close_code = await stream_once(url, cursor=cursor)
            if close_code in TRANSIENT_CLOSE_CODES:
                # A restart, not a refusal. Backoff **resets**: connecting successfully is
                # not evidence the server is unwell, and escalating here would leave a
                # healthy relay sitting out half a minute after a few releases.
                log(f"the room restarted the socket ({close_code}); reconnecting")
                backoff = 1.0
            else:
                # A deliberate close is the room ending the stream — a closed room, or a
                # revoked credential. Reconnecting into that forever is a busy loop against
                # a door that is shut, so this one does escalate.
                log(f"the room closed the socket ({close_code})")
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
                log(f"refused ({exc.code}); the participant token is invalid or revoked")
                return 2
            log(f"ticket request failed ({exc.code}); retrying in {backoff:.0f}s")
            backoff = min(backoff * 2, MAX_BACKOFF_SECONDS)
        except _ConnectionClosed as exc:
            # A backstop, and now only reachable for a close during the handshake itself —
            # `stream_once` catches everything after that and returns its cursor. Kept because
            # a close before the socket is open genuinely advanced nothing, so there is no
            # position to preserve and starting over is correct.
            code = getattr(getattr(exc, "rcvd", None), "code", None)
            if code in TRANSIENT_CLOSE_CODES or code is None:
                log(f"the socket dropped ({code}); reconnecting")
                backoff = 1.0
            else:
                log(f"the socket closed ({code}); retrying in {backoff:.0f}s")
                backoff = min(backoff * 2, MAX_BACKOFF_SECONDS)
        except (OSError, RuntimeError) as exc:
            log(f"{type(exc).__name__}: {exc}; retrying in {backoff:.0f}s")
            backoff = min(backoff * 2, MAX_BACKOFF_SECONDS)

        if args.once:
            return 0
        await asyncio.sleep(backoff)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default=os.environ.get("AGENT_ROOMS_BASE", DEFAULT_BASE))
    parser.add_argument("--room", default=os.environ.get("AGENT_ROOMS_ROOM"))
    parser.add_argument(
        "--relay-port",
        type=int,
        default=int(os.environ.get("COTTAGE_RELAY_PORT", DEFAULT_RELAY_PORT)),
        help=(
            "localhost port for the outbound chat relay, so a `>` line does not pay for a "
            "fresh TLS handshake. 0 disables it. Localhost only: it holds the participant "
            "token."
        ),
    )
    parser.add_argument(
        "--human-name",
        default=os.environ.get("COTTAGE_HUMAN_NAME", ""),
        help=(
            "default name relayed chat is attributed to, when the caller does not supply one. "
            "The seat is the agent, so an unnamed relay reads as the agent talking."
        ),
    )
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
