#!/usr/bin/env python
"""Render the room's pulse as one status-bar line.

Reads only the file `scripts/room_watcher.py` writes — never the network. A status
line is rendered constantly, so anything slow here is felt on every keystroke.

The age of the reading is part of the display on purpose. A status line that keeps
showing the last good state after the poller dies is worse than one that shows
nothing, because it manufactures exactly the false confidence it was added to prevent.
"""

from __future__ import annotations

import contextlib
import json
import os
import pathlib
import sys
import time
from typing import Any

DEFAULT_POLL_INTERVAL_S = 10.0
STALE_AFTER_POLL_PERIODS = 2.0
MODES = frozenset({"WATCHING", "DRAINING", "STOPPED"})


def _number(state: dict[str, Any], key: str, default: float) -> float:
    """Read a finite positive number without trusting a status file to be well formed."""
    try:
        value = float(state.get(key, default))
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def _field(value: Any) -> str:
    """A bounded, deterministic rendering for optional relay telemetry."""
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, str | int | float):
        return str(value)
    if isinstance(value, dict):
        return ",".join(f"{key}={value[key]}" for key in sorted(value))[:80]
    if isinstance(value, list):
        return str(len(value))
    return str(value)[:80]


def render(state: dict[str, Any], *, now: float | None = None) -> str:
    """Render one reading without network access or model work.

    `at` is the poller's proof of life. Room sequence is deliberately not used for
    the pulse: a healthy watcher in a quiet room sees no new sequence numbers.
    """
    read_at = _number(state, "at", 0.0)
    current = time.time() if now is None else now
    age = max(0.0, current - read_at)
    interval = _number(state, "poll_interval_s", DEFAULT_POLL_INTERVAL_S)
    stale_after = interval * STALE_AFTER_POLL_PERIODS
    raw_mode = str(state.get("mode") or "WATCHING").upper()
    mode = raw_mode if raw_mode in MODES else "UNKNOWN"

    # STOPPED is a successful terminal state, not a watcher that later went stale.
    if mode == "STOPPED":
        bits = ["■ room STOPPED"]
    elif state.get("error"):
        return f"○ room {mode} unreachable ({state['error']}) · poll age {int(age)}s"
    elif age > stale_after:
        return f"○ room {mode} stale · poll age {int(age)}s (limit {int(stale_after)}s)"
    else:
        # Quantize by the declared poll period so a fresh reading changes the pulse
        # even when the room's event sequence stays fixed.
        pulse = "♥" if int(read_at / interval) % 2 == 0 else "♡"
        bits = [f"{pulse} room {mode}"]

    if "cursor" in state:
        bits.append(f"cursor {state['cursor']}")
    elif "seq" in state:
        bits.append(f"seq {state['seq']}")

    if "live" in state or "participants" in state:
        bits.append(f"{state.get('live', 0)}/{state.get('participants', 0)} live")
    for key in ("workers", "pending", "delivery"):
        if key in state:
            bits.append(f"{key} {_field(state[key])}")
    if state.get("in_progress"):
        bits.append(f"{state['in_progress']} working")
    if state.get("waiting_input"):
        bits.append(f"⚠ {state['waiting_input']} waiting on you")
    if state.get("open_questions"):
        bits.append(f"? {state['open_questions']}")
    if state.get("conflicts"):
        bits.append(f"✗ {state['conflicts']} conflicts")
    if state.get("headline"):
        bits.append(str(state["headline"]))
    bits.append(f"poll age {int(age)}s")
    return " · ".join(bits)


def main() -> int:
    # Windows consoles default to cp1252, which cannot encode the pulse glyph — the
    # same encoding trap that has already put mojibake into a room-visible checkpoint
    # and a BOM into a piped secret. Say utf-8 explicitly rather than inherit a guess.
    with contextlib.suppress(AttributeError, OSError):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]

    # Claude Code sends session JSON on stdin; nothing here needs it, but it must be
    # drained or the writer can block.
    with contextlib.suppress(Exception):
        sys.stdin.read()

    # Argument first: a status-line command is spawned by the host, and relying on it
    # to forward an environment variable is the kind of assumption that shows up as a
    # permanently blank line with nothing to debug.
    path = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("AGENT_ROOMS_STATUS_FILE")
    if not path or not pathlib.Path(path).exists():
        print("○ room: not being watched")
        return 0
    try:
        state = json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        print("○ room: unreadable status file")
        return 0
    print(render(state))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
