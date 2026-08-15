#!/usr/bin/env python
"""Render the room's pulse as one status-bar line.

Reads only the file `scripts/room_watcher.py` writes — never the network. A status
line is rendered constantly, so anything slow here is felt on every keystroke.

The age of the reading is part of the display on purpose. A status line that keeps
showing the last good state after the poller dies is worse than one that shows
nothing, because it manufactures exactly the false confidence it was added to prevent.
"""

from __future__ import annotations

import json
import os
import pathlib
import sys
import time

STALE_AFTER = 45.0


def render(state: dict) -> str:
    age = time.time() - float(state.get("at") or 0)

    if state.get("error"):
        return f"○ room unreachable ({state['error']})"
    if age > STALE_AFTER:
        return f"○ room: no reading for {int(age)}s — poller stopped?"

    # A beat that alternates on each reading, so a *frozen* line is visibly frozen.
    pulse = "♥" if int(state.get("seq") or 0) % 2 == 0 else "♡"
    bits = [
        f"{pulse} room seq {state.get('seq')}",
        f"{state.get('live', 0)}/{state.get('participants', 0)} live",
    ]
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
    return " · ".join(bits) + f"  ({int(age)}s ago)"


def main() -> int:
    # Windows consoles default to cp1252, which cannot encode the pulse glyph — the
    # same encoding trap that has already put mojibake into a room-visible checkpoint
    # and a BOM into a piped secret. Say utf-8 explicitly rather than inherit a guess.
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    except (AttributeError, OSError):
        pass

    # Claude Code sends session JSON on stdin; nothing here needs it, but it must be
    # drained or the writer can block.
    try:
        sys.stdin.read()
    except Exception:  # noqa: BLE001
        pass

    # Argument first: a status-line command is spawned by the host, and relying on it
    # to forward an environment variable is the kind of assumption that shows up as a
    # permanently blank line with nothing to debug.
    path = (
        sys.argv[1] if len(sys.argv) > 1 else os.environ.get("AGENT_ROOMS_STATUS_FILE")
    )
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
