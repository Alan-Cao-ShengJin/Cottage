#!/usr/bin/env python
"""Long-poll a room and keep a one-line summary of it on disk.

Exists because a human supervising through a chat surface has no window into the room
except what their agent happens to mention. Silence then reads as "the room is dead"
when in fact a companion is working — which is the exact confusion this product exists
to remove, reproduced one level up.

Deliberately read-only: it authenticates with a participant token but never connects,
so it does not register presence, hold a lease, or make the seat look live when it is
not. What it reports is what a *reader* of the room can see, which is the honest thing
for a status indicator to show.

Pair with `scripts/room_statusline.py`, which renders the file this writes. Two
processes rather than one because a status line is rendered many times a second and
must never make a network call.

    set AGENT_ROOMS_TOKEN=...        (participant token; never passed as an argument)
    python scripts/room_watcher.py --room room_... --out %TEMP%\\room_status.json
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import time
import urllib.error
import urllib.request
from typing import Any

DEFAULT_BASE = "https://agent-rooms.fly.dev"


def read_room(base: str, room: str, token: str, *, timeout: int = 20) -> dict[str, Any]:
    req = urllib.request.Request(
        f"{base}/api/rooms/{room}/snapshot",
        headers={"Authorization": f"Bearer {token}"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return json.load(response)


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


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--base", default=os.environ.get("AGENT_ROOMS_BASE", DEFAULT_BASE)
    )
    parser.add_argument("--room", default=os.environ.get("AGENT_ROOMS_ROOM"))
    parser.add_argument("--out", default=os.environ.get("AGENT_ROOMS_STATUS_FILE"))
    parser.add_argument("--interval", type=int, default=10)
    args = parser.parse_args()

    # Never an argument: a token on a command line is readable from any process
    # listing for the life of the process (D-058).
    token = os.environ.get("AGENT_ROOMS_TOKEN")
    if not (args.room and args.out and token):
        parser.error("need --room, --out, and AGENT_ROOMS_TOKEN in the environment")

    out = pathlib.Path(args.out)
    while True:
        try:
            state = summarize(read_room(args.base, args.room, token))
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as exc:
            state = {"at": time.time(), "error": type(exc).__name__}
        except Exception as exc:  # noqa: BLE001 - a status line must not take the poller down
            state = {"at": time.time(), "error": type(exc).__name__}
        tmp = out.with_suffix(".tmp")
        tmp.write_text(json.dumps(state), encoding="utf-8")
        # Atomic on Windows too: the status line must never read a half-written file.
        tmp.replace(out)
        time.sleep(args.interval)


if __name__ == "__main__":
    raise SystemExit(main())
