#!/usr/bin/env python
"""Run the wake channel as a service that outlives any one editor session.

**The flaw this fixes.** The outbound chat relay lives inside `wake_channel.py`, which was the
right home — one resident process per room, already reporting its own failures. But it was
being started as a child of a Claude Code session, and that is the wrong *lifetime* for
something a `>` keystroke depends on. Observed twice in one evening: the session restarted, the
relay went with it, and `>` silently fell back to the slow path. Once it survived a restart by
luck of being detached, and died fifteen minutes later anyway.

Silent is the operative word. `cottage_chat_hook.py` correctly stands down when nothing answers
on the port, so a dead relay looks exactly like a slightly slow one — which is the same failure
shape as the reconnect bug that started this (D-091): a relay that is not running is
indistinguishable from a quiet room.

So the relay gets a supervisor that is not a session: a detached process, a pidfile, and a
`status` that says plainly whether it is up.

    python scripts/cottage_relay_service.py start   --room room_...
    python scripts/cottage_relay_service.py status
    python scripts/cottage_relay_service.py stop

**The credential has the same problem as the process.** A token in a session's environment
dies with that session, so "start it detached" alone just moves the failure: the relay outlives
the session and cannot be restarted without it. That is not hypothetical — it is how this file
came to be written, with a live room, a free port, and no way to reconnect to it.

So a token file is the supported way to run this (`--token-file`, or `AGENT_ROOMS_TOKEN_FILE`),
and `AGENT_ROOMS_TOKEN` from the environment is the convenience path. Either way the token is
**never** an argument — a command line is readable from any process listing (D-058) — while the
*path* is, because a path is not a secret. Keep the file readable only by its owner; it is a
participant credential at rest, which is a real trade for a service that must survive a restart.
`COTTAGE_HUMAN_NAME` is inherited the same way and is what relayed chat is attributed to.

**What this does not do.** It does not survive a reboot, and it is not a Windows service or a
systemd unit. Writing one of those is a real decision about where a credential lives at rest,
and it is not something to sneak in behind a convenience script. `status` exists so the gap is
visible rather than assumed away.
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import socket
import subprocess
import sys
import tempfile

#: Where the pidfile lives. Beside the runtime state the companion already keeps, so one
#: directory holds everything a machine knows about its Cottage runtimes.
DEFAULT_STATE_DIR = pathlib.Path(
    os.environ.get("COTTAGE_RUNTIME_DIR")
    or pathlib.Path(tempfile.gettempdir()) / "cottage-worker"
)

DEFAULT_PORT = int(os.environ.get("COTTAGE_RELAY_PORT", "8787"))
CHANNEL = pathlib.Path(__file__).resolve().parent / "wake_channel.py"


def _pidfile(state_dir: pathlib.Path) -> pathlib.Path:
    return state_dir / "relay-service.json"


def _listening(port: int) -> bool:
    """Is something answering on the relay port?

    The authoritative check, and deliberately preferred over the pidfile: a pid can exist
    while the thread behind the port has died, and what a `>` keystroke needs is the port.
    """
    try:
        socket.create_connection(("127.0.0.1", port), timeout=1).close()
        return True
    except OSError:
        return False


def _alive(pid: int) -> bool:
    if os.name == "nt":
        out = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
            capture_output=True,
            text=True,
        ).stdout
        return str(pid) in out
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _restrict_to_owner(path: pathlib.Path) -> None:
    """Make a token file readable by its owner only.

    Best effort, and said plainly when it is not: `chmod` is honored on POSIX, while on Windows
    the mode bits are close to meaningless and the real control is the ACL. `icacls` is the
    thing that actually narrows it, so a failure there is reported rather than swallowed -- a
    credential that is world-readable while the tool implied otherwise is worse than one whose
    exposure was stated.
    """
    try:
        path.chmod(0o600)
    except OSError:
        pass
    if os.name != "nt":
        return
    user = os.environ.get("USERNAME") or ""
    if not user:
        print(
            "warning: could not determine the current user; file ACL left as inherited."
        )
        return
    done = subprocess.run(
        ["icacls", str(path), "/inheritance:r", "/grant:r", f"{user}:F"],
        capture_output=True,
        text=True,
    )
    if done.returncode != 0:
        print(
            f"warning: could not restrict {path} to {user}; it inherits directory permissions."
        )


def _read_state(state_dir: pathlib.Path) -> dict:
    try:
        return dict(json.loads(_pidfile(state_dir).read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return {}


def start(args: argparse.Namespace) -> int:
    # The port is checked first, and deliberately before the credential. A second `start` from
    # a shell that happens not to carry the token would otherwise answer "no credential" while
    # a perfectly healthy relay is serving: a refusal that reads as "nothing is running" in the
    # one case where everything is fine.
    if _listening(args.port):
        print(f"already listening on 127.0.0.1:{args.port}; nothing to do.")
        return 0

    # Printed output stays ASCII throughout this file. It runs on a cp1252 console, where an
    # em dash is mojibake at best and a UnicodeEncodeError at worst, and a traceback out of
    # `status` would be the tool failing at the one question it exists to answer.
    token_file = args.token_file
    has_file = bool(token_file) and pathlib.Path(token_file).is_file()
    env_token = os.environ.get("AGENT_ROOMS_TOKEN", "").strip()

    if args.save_token and env_token and token_file and not has_file:
        # Turning an ephemeral credential into a durable one, at the only moment it exists: a
        # participant token is minted inside a session, and the service that needs it has to
        # outlive that session. Writing a credential to disk is never a side effect, which is
        # why this needs the flag -- but without some such path the durable mode is unreachable
        # except by pasting a token somewhere, and every "somewhere" is worse than a 0600 file.
        target = pathlib.Path(token_file)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(env_token, encoding="ascii")
        _restrict_to_owner(target)
        has_file = True
        print(f"saved the participant token to {target} (owner-only).")

    if not (env_token or has_file):
        print("No participant credential; refusing to start.")
        print(
            "  --token-file PATH        (or AGENT_ROOMS_TOKEN_FILE): survives a restart"
        )
        print("  AGENT_ROOMS_TOKEN=...    in this environment: dies with this shell")
        if token_file:
            print(f"(--token-file was given but {token_file} is not a readable file.)")
        print(
            "The token itself is never an argument: a command line is world-readable."
        )
        return 2
    if not args.room:
        print("--room is required (or set AGENT_ROOMS_ROOM).")
        return 2

    args.state_dir.mkdir(parents=True, exist_ok=True)
    log_path = args.state_dir / "relay-service.log"
    # Appended, never truncated: the reason a relay stopped is the thing worth keeping, and a
    # restart that erased it would destroy the evidence at exactly the wrong moment.
    log = open(log_path, "a", encoding="utf-8", buffering=1)  # noqa: SIM115

    command = [
        sys.executable,
        str(CHANNEL),
        "--room",
        args.room,
        "--base",
        args.base,
        "--relay-port",
        str(args.port),
    ]
    if has_file:
        # The path, never the token. The child re-reads it at startup, so a rotated token is
        # picked up by a restart rather than needing this script edited.
        command += ["--token-file", str(token_file)]
    # Detached, so closing the terminal that started it does not take it down — which is the
    # entire point of this file.
    creation = 0
    if os.name == "nt":
        creation = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
    child = subprocess.Popen(
        command,
        stdout=log,
        stderr=log,
        stdin=subprocess.DEVNULL,
        creationflags=creation,
        start_new_session=(os.name != "nt"),
        env={**os.environ, "COTTAGE_RELAY_PORT": str(args.port)},
    )
    _pidfile(args.state_dir).write_text(
        json.dumps(
            {
                "pid": child.pid,
                "room": args.room,
                "port": args.port,
                "log": str(log_path),
            }
        ),
        encoding="utf-8",
    )
    print(f"started pid {child.pid} for {args.room}; log: {log_path}")
    print(f"check it with: {pathlib.Path(__file__).name} status")
    return 0


def status(args: argparse.Namespace) -> int:
    state = _read_state(args.state_dir)
    up = _listening(args.port)
    pid = int(state.get("pid") or 0)
    print(f"port 127.0.0.1:{args.port}: {'LISTENING' if up else 'not listening'}")
    if pid:
        print(
            f"pidfile pid {pid}: {'alive' if _alive(pid) else 'gone'}  room={state.get('room')}"
        )
        print(f"log: {state.get('log')}")
    else:
        print("no pidfile; nothing was started through this script.")
    if pid and _alive(pid) and not up:
        # Worth calling out rather than leaving to be inferred: the process being alive is not
        # the thing a `>` keystroke depends on.
        print(
            "the process is alive but the port is not answering: the relay thread is down."
        )
    # Non-zero when chat would fall back to the slow path, so this is usable in a check.
    return 0 if up else 1


def stop(args: argparse.Namespace) -> int:
    state = _read_state(args.state_dir)
    pid = int(state.get("pid") or 0)
    if not pid:
        print("no pidfile; nothing to stop.")
        return 0
    if not _alive(pid):
        print(f"pid {pid} is already gone.")
        _pidfile(args.state_dir).unlink(missing_ok=True)
        return 0
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True)
    else:
        import signal

        os.kill(pid, signal.SIGTERM)
    print(f"stopped pid {pid}.")
    _pidfile(args.state_dir).unlink(missing_ok=True)
    return 0


def main(argv: list[str] | None = None) -> int:
    # A short ASCII description rather than `__doc__`: the module docstring is written for
    # somebody reading the file, and piping its em dashes at a cp1252 console would make
    # `--help` the first thing that crashes.
    parser = argparse.ArgumentParser(
        description="Run the room wake channel and its chat relay as a detached service."
    )
    parser.add_argument("action", choices=("start", "status", "stop"))
    parser.add_argument("--room", default=os.environ.get("AGENT_ROOMS_ROOM"))
    parser.add_argument(
        "--base", default=os.environ.get("COTTAGE_BASE", "https://app.cottageai.dev")
    )
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument(
        "--token-file", default=os.environ.get("AGENT_ROOMS_TOKEN_FILE")
    )
    parser.add_argument(
        "--save-token",
        action="store_true",
        help=(
            "Copy AGENT_ROOMS_TOKEN into --token-file (owner-only) so later restarts need no "
            "session. Never implicit: this writes a credential to disk."
        ),
    )
    parser.add_argument("--state-dir", type=pathlib.Path, default=DEFAULT_STATE_DIR)
    args = parser.parse_args(argv)
    return {"start": start, "status": status, "stop": stop}[args.action](args)


if __name__ == "__main__":
    sys.exit(main())
