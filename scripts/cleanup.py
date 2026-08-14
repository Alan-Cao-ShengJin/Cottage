"""Cleanup for temporary rooms.

V0 keeps expired rooms in SQLite so you can inspect a demo afterwards. This is
the operation that actually deletes them, and the seam where a scheduled TTL job
would plug in later.

    python scripts/cleanup.py --list                 # what is in the database
    python scripts/cleanup.py --expire               # mark overdue rooms expired
    python scripts/cleanup.py --purge-expired        # delete expired rooms
    python scripts/cleanup.py --purge-all            # delete everything
    python scripts/cleanup.py --purge F7K29A         # delete one room
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.db import database as db  # noqa: E402
from app.services import rooms  # noqa: E402
from app.util import normalize_join_code  # noqa: E402


async def main(args: argparse.Namespace) -> int:
    await db.init_db()
    print(f"database: {db.get_database_path()}\n")

    if args.expire:
        expired = await rooms.expire_due_rooms()
        print(f"expired {len(expired)} overdue room(s)")

    if args.purge:
        code = normalize_join_code(args.purge)
        await rooms.purge_room(code)
        print(f"purged room {code}")

    if args.purge_expired or args.purge_all:
        await rooms.expire_due_rooms()
        targets = [
            room
            for room in await rooms.list_rooms(limit=1000)
            if args.purge_all or room.status != "active"
        ]
        for room in targets:
            await rooms.purge_room(room.id)
        print(f"purged {len(targets)} room(s)")

    listing = await rooms.list_rooms(limit=100)
    if not listing:
        print("no rooms")
        return 0

    print(f"{'CODE':<8} {'STATUS':<9} {'TURNS':<7} {'EXPIRES IN':<12} TITLE")
    for room in listing:
        remaining = f"{room.seconds_remaining // 60}m" if room.seconds_remaining else "-"
        turns = f"{room.agent_turns_used}/{room.max_agent_turns}"
        print(f"{room.join_code:<8} {room.status:<9} {turns:<7} {remaining:<12} {room.title}")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--list", action="store_true", help="List rooms (default)")
    parser.add_argument("--expire", action="store_true", help="Mark overdue rooms expired")
    parser.add_argument("--purge", metavar="CODE", help="Delete one room by join code")
    parser.add_argument("--purge-expired", action="store_true", help="Delete expired rooms")
    parser.add_argument("--purge-all", action="store_true", help="Delete every room")
    sys.exit(asyncio.run(main(parser.parse_args())))
