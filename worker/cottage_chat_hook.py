#!/usr/bin/env python3
"""Claude Code UserPromptSubmit hook: relay a `>` line to a Cottage room, with no model turn.

**Why this exists.** Typing `>what's up?` and waiting four or five seconds is not chat. The
delay was not the network — it was a model being invoked to decide something that needs no
deciding: strip the marker, post the words, show the receipt. Every one of those steps is
mechanical, so spending a turn on them buys nothing and costs both latency and tokens
(D-091). This does the mechanical part in the host, which is where the `>` convention always
belonged.

Same shape as `cottage_goal_hook.py`: a local adapter doing a deterministic thing so the model
does not have to be woken for it. The room is still the authority; this only carries words to
it.

**The mechanism.** `UserPromptSubmit` receives the typed text as `user_input`. On a pure chat
prompt this posts the message and returns `{"continue": false, "stopReason": "<receipt>"}` —
`continue: false` stops the prompt before it reaches the model, and `stopReason` is shown to
the person. So: one HTTP round trip, no thinking, and the sender still sees confirmation.

Exit 2 would also block, but it *erases* the prompt and shows nothing, which is the one
outcome worse than being slow: words that look sent and are not.

## What it will and will not intercept

Only a prompt whose every non-empty line begins with `>`. That is the unambiguous case.

A **mixed** prompt — some lines marked, some not — is deliberately left alone and goes to the
model as normal. The hook cannot rewrite a prompt, only block it, so relaying the marked half
and passing the whole thing through would post those lines twice. Splitting a person's message
between two mechanisms without being able to edit it is worse than being slow, so the model
handles that case with its own judgement.

## It fails open, always

Missing configuration, an unset name, a refused request, a timeout, any unexpected error: exit
0 and let the prompt through to the model, which will relay it the slower way. A chat message
silently swallowed is far worse than one that took five seconds, so every failure path here
degrades to the behaviour that already worked.

## Installing it

    {
      "hooks": {
        "UserPromptSubmit": [
          {"hooks": [{"type": "command",
                      "command": "python /path/to/cottage_chat_hook.py"}]}
        ]
      }
    }

Required environment (never arguments — a participant token on a command line is a credential
in every process list):

* `COTTAGE_HUMAN_NAME` — the name your words appear under. Without it the hook stands down
  rather than posting unattributed: the seat is the *agent*, so an unnamed relay reads as the
  agent talking, which is the confusion the convention exists to remove.
* `COTTAGE_RELAY_PORT` — optional, defaults to 8787. Must match the wake channel's.

## It needs the wake channel running

This hook does **not** open its own connection to the room, and that is the point. A fresh
process spends about 400ms on TCP and TLS before the first byte of the message leaves, which
was roughly half the wall clock of typing a line. So the work is handed to
`scripts/wake_channel.py`, which is already resident for this room and holds the connection
warm.

If nothing is listening, the hook stands down and the prompt reaches the model, which relays it
the slower way. That fallback is deliberately the *model* rather than a direct request here:
opening a connection would hide a dead relay behind a merely sluggish one, and this session has
already been bitten once by a relay that failed silently.

Notice what this file therefore does not hold: no room id, no participant token. The credential
stays in the resident process, and the door onto it is localhost-only.
"""

from __future__ import annotations

import json
import os
import socket
import sys

DEFAULT_BASE = "https://app.cottageai.dev"

#: Where the resident wake channel listens for outbound chat. Reaching it saves the ~400ms a
#: fresh process spends on TCP and TLS before a single byte of the message leaves — measured,
#: and roughly half the wall clock of typing a line (D-091).
DEFAULT_RELAY_PORT = 8787

#: How long to wait for the local relay before giving up on it. Generous enough for a warm
#: round trip to the room, short enough that a wedged relay is not what a person waits on.
RELAY_TIMEOUT_SECONDS = 6.0

#: One HTTP round trip is the entire budget. A person who typed a line is watching the cursor,
#: so a hook that waits longer than this has already failed at the thing it exists to do —
#: better to hand the prompt to the model, which is slower but visibly working.
TIMEOUT_SECONDS = 4.0

#: Cap on what is relayed in one go. The room enforces its own limit; this is here so a pasted
#: file cannot become a chat message by accident.
MAX_BODY_CHARS = 4000


def _allow() -> int:
    """Let the prompt reach the model. The answer to every failure in this file."""
    return 0


def _chat_body(user_input: str) -> str | None:
    """The words to relay, or None if this prompt is not pure chat.

    A line "begins with `>`" after leading whitespace, and exactly one marker is removed —
    `>> quoted` relays as `> quoted`, because the second marker is the person's text and this
    hook does not get to edit what they wrote.
    """
    lines = user_input.splitlines() or [user_input]
    meaningful = [line for line in lines if line.strip()]
    if not meaningful:
        return None
    if not all(line.lstrip().startswith(">") for line in meaningful):
        # Mixed, or not chat at all. Left to the model on purpose - see the module docstring.
        return None
    body = "\n".join(line.lstrip().removeprefix(">").lstrip() for line in meaningful).strip()
    return body or None


def _clock(timestamp: str) -> str:
    """`HH:MM:SSZ` from the room's own RFC 3339 stamp, or "" if it is unreadable.

    Sliced rather than parsed: the value is already UTC, so reformatting cannot move the
    instant, and a receipt is the wrong place to discover a timezone conversion was wrong.
    """
    if len(timestamp) >= 19 and timestamp[10] in "T ":
        return f"{timestamp[11:19]}Z"
    return ""


def _receipt(*, name: str, body: str, created_at: str) -> str:
    """One line, because the person is watching a cursor.

    Deliberately *not* `compact.sent`. That sheet is rendered by the MCP adapter for a model
    that has to be told to print it verbatim, and it names who is watching live — which costs
    a second request this hook cannot afford. Here the whole budget is one POST, so the
    receipt states only what that POST already returned: the room's own timestamp, the
    attribution, and the words as they went out.
    """
    at = _clock(created_at)
    stamp = f"Sent {at}" if at else "Sent"
    first = body.splitlines()[0] if body else ""
    more = "" if len(body.splitlines()) <= 1 else f" (+{len(body.splitlines()) - 1} more lines)"
    return f"{stamp} · {name} · {first}{more}"


def _via_relay(body: str, name: str) -> dict[str, object] | None:
    """Hand the line to the resident wake channel. `None` if it could not be done.

    One line of JSON in, one line out, connection closed. The protocol is this small on
    purpose: it means this file imports `socket` and `json` and nothing else, which keeps
    interpreter startup near its floor — the other half of the latency, after the handshake
    the relay is saving.
    """
    port = int(os.environ.get("COTTAGE_RELAY_PORT", DEFAULT_RELAY_PORT) or DEFAULT_RELAY_PORT)
    if port <= 0:
        return None
    payload = json.dumps({"body": body, "speaking_as": name}).encode() + b"\n"
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=RELAY_TIMEOUT_SECONDS) as conn:
            conn.sendall(payload)
            chunks: list[bytes] = []
            while b"\n" not in b"".join(chunks):
                piece = conn.recv(4096)
                if not piece:
                    break
                chunks.append(piece)
            answer = json.loads(b"".join(chunks).split(b"\n", 1)[0] or b"{}")
    except (OSError, ValueError):
        return None
    if not isinstance(answer, dict) or not answer.get("ok"):
        return None
    return answer


def main() -> int:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except (OSError, ValueError):
        return _allow()
    if not isinstance(payload, dict):
        return _allow()

    body = _chat_body(str(payload.get("user_input") or ""))
    if body is None:
        return _allow()

    # No room, no token, no required name. The resident relay holds all three, which is why
    # this file can be dropped in front of every prompt on a machine without carrying a
    # credential — and why it needs no configuration to be safe when nothing is listening.
    name = os.environ.get("COTTAGE_HUMAN_NAME", "").strip()
    result = _via_relay(body[:MAX_BODY_CHARS], name)
    if result is None:
        # Nothing listening, or it failed. The words are not in the room, so the prompt must
        # reach the model — which will relay them the slower way and can say what happened.
        # Deliberately *not* a fallback to opening a connection here: that is the 750ms path
        # this hook exists to avoid, and doing it silently would hide a dead relay behind a
        # sluggish one. The model is the fallback, and it is a visible one.
        return _allow()

    # Posted. Stop the prompt before it reaches the model, and show the person the receipt.
    #
    # The name comes back from the relay rather than from this process: when the hook supplies
    # none, the relay's own default is used, and a receipt naming somebody other than the
    # message's actual attribution would be a small lie in the one line the sender reads.
    json.dump(
        {
            "continue": False,
            "stopReason": _receipt(
                name=str(result.get("speaking_as") or name or "you"),
                body=body,
                created_at=str(result.get("created_at") or ""),
            ),
        },
        sys.stdout,
    )
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        # A hook that breaks must not eat the message. Allowing costs five seconds; blocking
        # costs the words.
        sys.exit(0)
