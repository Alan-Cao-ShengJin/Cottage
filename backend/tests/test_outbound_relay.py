"""The held connection behind `>` chat: warm, serialized, and cheap to poke (D-092).

The relay exists because posting a chat line from a cold process spends most of its wall clock
establishing a connection it then throws away — 185ms of TCP and 210ms of TLS against the hosted
instance (D-091). Holding the connection took `>` to about half a second.

But a held connection is only warm while it is being used, and chat is bursty. Measured here:
1172ms for the first line after the relay started, against 448ms once warm. That is not an edge
case — it is most messages, and it lands on exactly the one somebody reads as broken. Hence a
keep-alive, and hence these tests, which run against a real HTTP server on loopback so that
connection *reuse* is actually observed rather than asserted about a mock.

The lock is the other half. `serve` hands every accepted socket to its own thread, so two lines
typed close together already raced on one HTTP/1.1 connection and could read each other's
responses. Rare while a person types; routine once a keep-alive can fire mid-request.
"""

from __future__ import annotations

import http.server
import importlib.util
import json
import sys
import threading
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]


def _load_channel():
    name = "_wake_channel_under_test"
    if name in sys.modules:
        return sys.modules[name]
    path = REPO / "scripts" / "wake_channel.py"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        del sys.modules[name]
        raise
    return module


channel = _load_channel()


class _Recorder:
    """What the server saw. Shared across handler instances."""

    def __init__(self):
        self.requests: list[tuple[str, str, dict[str, str]]] = []
        self.connections = 0
        self.overlaps = 0
        self.in_flight = 0
        self.hold_seconds = 0.0
        self.status_for_post = 200
        self.lock = threading.Lock()


@pytest.fixture
def server():
    recorder = _Recorder()

    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def setup(self):
            with recorder.lock:
                recorder.connections += 1
            super().setup()

        def log_message(self, *args):
            return

        def _record(self):
            with recorder.lock:
                recorder.requests.append(
                    (self.command, self.path, {k.lower(): v for k, v in self.headers.items()})
                )

        def do_GET(self):
            self._record()
            body = b'{"status": "ok"}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            length = int(self.headers.get("Content-Length") or 0)
            self.rfile.read(length)
            self._record()
            with recorder.lock:
                recorder.in_flight += 1
                if recorder.in_flight > 1:
                    recorder.overlaps += 1
            if recorder.hold_seconds:
                time.sleep(recorder.hold_seconds)
            with recorder.lock:
                recorder.in_flight -= 1
            body = json.dumps({"ok": True, "created_at": "2026-08-20T17:00:00.000Z"}).encode()
            self.send_response(recorder.status_for_post)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    port = httpd.server_address[1]
    try:
        yield f"http://127.0.0.1:{port}", recorder
    finally:
        httpd.shutdown()
        httpd.server_close()


def _relay(base: str) -> object:
    return channel.OutboundRelay(base, "room_01TEST", "tok_participant_secret")


# ---------------------------------------------------------------------------
# Warming
# ---------------------------------------------------------------------------


def test_warm_establishes_the_connection_before_anybody_is_waiting(server):
    base, recorder = server
    relay = _relay(base)
    assert relay.warm() is True
    assert recorder.connections == 1
    assert recorder.requests[0][0] == "GET"


def test_a_poke_carries_no_credential(server):
    """`/healthz` needs no authorization, and a keep-alive that sent the participant token every
    50 seconds would be spending a credential on a request that does not need one."""
    base, recorder = server
    _relay(base).warm()
    _method, path, headers = recorder.requests[0]
    assert path == "/healthz"
    assert "authorization" not in headers


def test_warming_then_posting_reuses_the_same_connection(server):
    """The whole point. If the poke opened its own connection, the first real message would
    still pay the handshake and the keep-alive would be pure cost."""
    base, recorder = server
    relay = _relay(base)
    relay.warm()
    assert relay.post_message("hello", "Alan")["ok"] is True
    assert recorder.connections == 1, "a second connection means nothing was kept warm"


def test_repeated_pokes_do_not_pile_up_connections(server):
    base, recorder = server
    relay = _relay(base)
    for _ in range(5):
        relay.warm()
    assert recorder.connections == 1


def test_a_poke_reads_the_response_body(server):
    """An unread body leaves an HTTP/1.1 connection unusable for the next request. Skipping the
    read would make the keep-alive cause the stall it exists to prevent — and it would look like
    a warm connection right up to the moment somebody typed."""
    base, recorder = server
    relay = _relay(base)
    relay.warm()
    assert relay.post_message("hello", "Alan")["ok"] is True
    assert [r[0] for r in recorder.requests] == ["GET", "POST"]


def test_a_poke_that_cannot_reach_the_room_is_not_fatal():
    """It costs a person nothing, so it reports and moves on. `post_message` reconnects on its
    own, which is the path that actually matters.

    Port 1 rather than a mangled copy of the live one: refused immediately, and unambiguously
    the reason the poke failed."""
    relay = _relay("http://127.0.0.1:1")
    assert relay.warm() is False


def test_the_keepalive_loop_pokes_on_its_own_thread_without_being_asked_twice(server):
    base, recorder = server
    relay = _relay(base)
    thread = threading.Thread(target=relay.keep_warm, args=(0.05,), daemon=True)
    thread.start()
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline:
        with recorder.lock:
            if len(recorder.requests) >= 3:
                break
        time.sleep(0.02)
    with recorder.lock:
        assert len(recorder.requests) >= 3, "the loop should keep poking, not poke once"
        assert recorder.connections == 1


# ---------------------------------------------------------------------------
# Serialization
# ---------------------------------------------------------------------------


def test_two_lines_at_once_do_not_interleave_on_one_connection(server):
    """`serve` spawns a thread per accepted socket, and one HTTP/1.1 connection cannot carry two
    requests at a time — the second reads the first's response. This is the pre-existing race
    that the keep-alive would have made routine."""
    base, recorder = server
    recorder.hold_seconds = 0.15
    relay = _relay(base)
    relay.warm()

    results: list[dict] = []
    threads = [
        threading.Thread(target=lambda i=i: results.append(relay.post_message(f"line {i}", "Alan")))
        for i in range(4)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=20)

    assert len(results) == 4
    assert all(r.get("ok") for r in results), results
    assert recorder.overlaps == 0, "two requests were in flight on one held connection"
    assert recorder.connections == 1


def test_a_failed_request_does_not_close_a_connection_another_thread_is_using(server):
    """`_drop` takes the same lock for this reason. It was called outside it, so a failing
    request could close the object a concurrent request was mid-flight on — and the lock has to
    be reentrant because `warm` drops while already holding it."""
    base, _recorder = server
    relay = _relay(base)
    relay.warm()
    relay._drop()  # from a caller that holds nothing
    assert relay.post_message("still fine", "Alan")["ok"] is True

    # And from a caller that already holds it, which a plain Lock would deadlock on.
    with relay._lock:
        relay._drop()


# ---------------------------------------------------------------------------
# Posting
# ---------------------------------------------------------------------------


def test_a_refusal_from_the_room_is_reported_rather_than_retried(server):
    """A 4xx is the room's answer, not a broken socket. Retrying would delay the receipt that
    tells somebody their words did not land."""
    base, recorder = server
    recorder.status_for_post = 403
    relay = _relay(base)
    result = relay.post_message("hello", "Alan")
    assert result["ok"] is False
    assert "403" in result["error"]
    assert len([r for r in recorder.requests if r[0] == "POST"]) == 1


def test_the_post_carries_the_token_and_says_a_person_is_speaking(server):
    base, recorder = server
    _relay(base).post_message("anyone want lunch?", "Alan")
    _method, path, headers = next(r for r in recorder.requests if r[0] == "POST")
    assert path == "/api/rooms/room_01TEST/messages"
    assert headers["authorization"] == "Bearer tok_participant_secret"
