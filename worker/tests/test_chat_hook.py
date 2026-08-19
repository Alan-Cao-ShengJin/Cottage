"""The `>` chat hook: post in the host, spend no model turn (D-091).

Typing `>what's up?` and waiting four or five seconds is not chat. The delay was never the
network — it was a model being invoked to decide something mechanical. This hook does that work
in the host, and these tests hold the two properties that make it safe to put in front of every
prompt a person types:

* **It fails open on everything.** Missing config, refused request, timeout, malformed payload,
  unexpected exception: the prompt reaches the model, which relays it the slower way. A chat
  message silently swallowed is far worse than one that took five seconds.
* **It intercepts only the unambiguous case.** Every non-empty line marked `>`. A mixed prompt
  goes to the model untouched, because the hook can block a prompt but cannot rewrite one — so
  relaying half and passing the whole thing through would post those lines twice.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import cottage_chat_hook as hook  # noqa: E402

HOOK = Path(__file__).resolve().parents[1] / "cottage_chat_hook.py"


def _run(user_input: str, *, env_extra: dict[str, str] | None = None):
    import os

    env = {
        "PATH": os.environ.get("PATH", ""),
        "SYSTEMROOT": os.environ.get("SYSTEMROOT", ""),
    }
    env.update(env_extra or {})
    return subprocess.run(
        [sys.executable, str(HOOK)],
        input=json.dumps({"hook_event_name": "UserPromptSubmit", "user_input": user_input}),
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )


# ---------------------------------------------------------------------------
# What counts as chat
# ---------------------------------------------------------------------------


def test_a_marked_line_is_chat_and_the_marker_is_not_part_of_it():
    assert hook._chat_body(">what's up?") == "what's up?"
    assert hook._chat_body("> what's up?") == "what's up?"
    assert hook._chat_body("   > indented") == "indented"


def test_every_line_must_be_marked_or_the_model_handles_it():
    """The hook can block a prompt but cannot rewrite one. Relaying the marked half and passing
    the whole prompt through would post those lines twice, so a mixed message is left alone —
    splitting somebody's words between two mechanisms is worse than being slow."""
    assert hook._chat_body("> tell them I am done\nalso fix the failing test") is None
    assert hook._chat_body("fix the failing test") is None
    assert hook._chat_body("look at > this file") is None


def test_several_marked_lines_relay_as_one_message():
    assert hook._chat_body("> first\n> second") == "first\nsecond"
    # Blank lines between them are formatting, not content.
    assert hook._chat_body("> first\n\n> second") == "first\nsecond"


def test_a_second_marker_is_the_persons_own_text():
    """`>>` is a quote. Exactly one marker is addressing; the rest is what they wrote, and the
    hook does not get to edit that."""
    assert hook._chat_body(">> quoted") == "> quoted"


def test_an_empty_or_marker_only_prompt_is_not_chat():
    assert hook._chat_body(">") is None
    assert hook._chat_body("> ") is None
    assert hook._chat_body("") is None
    assert hook._chat_body("   ") is None


# ---------------------------------------------------------------------------
# The receipt
# ---------------------------------------------------------------------------


def test_the_receipt_is_one_line_with_the_rooms_own_stamp():
    line = hook._receipt(name="Alan", body="what's up?", created_at="2026-08-19T16:09:28.442Z")
    assert line == "Sent 16:09:28Z · Alan · what's up?"


def test_a_multi_line_relay_says_how_much_it_sent():
    line = hook._receipt(name="Alan", body="first\nsecond\nthird", created_at="")
    assert line == "Sent · Alan · first (+2 more lines)"


def test_an_unreadable_stamp_is_omitted_rather_than_guessed():
    assert hook._clock("") == ""
    assert hook._clock("nonsense") == ""
    assert hook._clock("2026-08-19T16:09:28Z") == "16:09:28Z"


# ---------------------------------------------------------------------------
# Failing open, which is the whole safety margin
# ---------------------------------------------------------------------------

#: A port nothing listens on. Refused immediately rather than timing out, which is what makes
#: "no relay running" cost a millisecond instead of a wait.
CLOSED_PORT = "1"


def test_no_relay_running_lets_the_prompt_through():
    """The common case on a machine with no room open. The prompt reaches the model, which can
    relay it the slower way and say what happened — so a chat line is never swallowed."""
    result = _run(">what's up?", env_extra={"COTTAGE_RELAY_PORT": CLOSED_PORT})
    assert result.returncode == 0
    assert result.stdout == "", "a failed relay must not look like a sent message"


def test_the_hook_needs_no_configuration_at_all():
    """Deliberate, and it is what lets this sit in front of every prompt on a machine. The
    resident relay holds the room, the token and the default name; this file holds none of
    them, so it carries no credential and cannot post to the wrong room.

    An earlier version demanded COTTAGE_ROOM and COTTAGE_PARTICIPANT_TOKEN and stood down
    without them — after the rework moved that job to the relay. The docstring was updated and
    the guard was not, so every message silently stood down. Hence this test.
    """
    source = HOOK.read_text(encoding="utf-8")
    assert "COTTAGE_ROOM" not in source
    assert "COTTAGE_PARTICIPANT_TOKEN" not in source
    assert "add_argument" not in source
    # And it opens no connection of its own: that is the 750ms path this exists to avoid, and
    # doing it as a fallback would hide a dead relay behind a merely slow one.
    assert "urllib" not in source
    assert "http.client" not in source


def test_an_ordinary_prompt_is_untouched_even_with_a_relay_available():
    result = _run("fix the failing reconnect test", env_extra={"COTTAGE_RELAY_PORT": CLOSED_PORT})
    assert result.returncode == 0
    assert result.stdout == ""


def test_a_malformed_payload_lets_the_prompt_through():
    for payload in ("", "not json", "[]", "null"):
        result = subprocess.run(
            [sys.executable, str(HOOK)],
            input=payload,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, payload
        assert result.stdout == "", payload


def test_a_relay_that_refuses_the_message_lets_the_prompt_through():
    """`ok: false` from the relay means the room did not take it. The prompt must still reach
    the model, because the words are not where the sender thinks they are."""
    with _fake_relay({"ok": False, "error": "the room refused it (403)"}) as port:
        result = _run(">what's up?", env_extra={"COTTAGE_RELAY_PORT": str(port)})
    assert result.returncode == 0
    assert result.stdout == ""


# ---------------------------------------------------------------------------
# The one path that stops the model
# ---------------------------------------------------------------------------


import contextlib  # noqa: E402
import socket as socket_module  # noqa: E402
import threading  # noqa: E402


@contextlib.contextmanager
def _fake_relay(answer: dict, *, capture: list | None = None):
    """A localhost stand-in for the wake channel's relay: one line in, one line out."""
    listener = socket_module.socket(socket_module.AF_INET, socket_module.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]

    def serve():
        try:
            conn, _ = listener.accept()
        except OSError:
            return
        with conn:
            buf = b""
            while b"\n" not in buf:
                piece = conn.recv(4096)
                if not piece:
                    break
                buf += piece
            if capture is not None:
                with contextlib.suppress(ValueError):
                    capture.append(json.loads(buf.split(b"\n", 1)[0] or b"{}"))
            conn.sendall(json.dumps(answer).encode() + b"\n")

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    try:
        yield port
    finally:
        listener.close()


def test_it_blocks_the_prompt_and_shows_a_receipt_when_the_relay_accepts():
    """`continue: false` stops the prompt before the model sees it; `stopReason` is what the
    person reads. Exit 2 would also block, but it *erases* the prompt and shows nothing —
    words that look sent and are not."""
    sent: list = []
    answer = {
        "ok": True,
        "message_id": "msg_1",
        "created_at": "2026-08-19T16:09:28.442Z",
        "speaking_as": "Alan",
    }
    with _fake_relay(answer, capture=sent) as port:
        result = _run(
            ">what's up?",
            env_extra={"COTTAGE_RELAY_PORT": str(port), "COTTAGE_HUMAN_NAME": "Alan"},
        )

    assert result.returncode == 0
    decision = json.loads(result.stdout)
    assert decision["continue"] is False
    assert decision["stopReason"] == "Sent 16:09:28Z · Alan · what's up?"
    # The marker is stripped before it leaves, and the relay is told this is a person speaking.
    assert sent == [{"body": "what's up?", "speaking_as": "Alan"}]


def test_the_receipt_names_the_attribution_the_relay_actually_used():
    """The hook may supply no name and take the relay's default. A receipt naming somebody
    other than the message's real attribution would be a lie in the one line the sender reads.
    """
    answer = {"ok": True, "created_at": "2026-08-19T16:09:28.442Z", "speaking_as": "Bea"}
    with _fake_relay(answer) as port:
        result = _run(">hello", env_extra={"COTTAGE_RELAY_PORT": str(port)})
    decision = json.loads(result.stdout)
    assert "Bea" in decision["stopReason"]
