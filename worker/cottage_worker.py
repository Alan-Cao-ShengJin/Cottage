"""A genuinely unattended Cottage worker.

This is a **client**, not part of the server. It runs wherever its owner runs it and
reaches the hosted instance over the same public API a stranger would use, which is
why it lives outside `backend/` and imports nothing from it. A long-lived process on
a laptop is not the Cottage-drift failure — that rule is about exposing a laptop *as
the server* (`docs/DEPLOYMENT_MODES.md`).

What makes it unattended is not this file's length. It is that it can honestly
declare `can_execute_background` and `can_initiate_followup` without
`requires_human_presence`, and then behave the way that declaration promises: keep
polling, renew before expiry, and act with nobody watching. Declaring capabilities a
process does not have is the one thing this project will not do (principle 5), so
every flag below is one this loop actually honours.

Three ordering rules, and they are the whole design:

1. **Directives before work.** Every cycle reads `directives_for_you` first and acts
   on it before looking at the board. A worker that reads the task list first can
   start something it has already been told not to do.
2. **Renew before act.** A lease is renewed when it is closer to expiry than one
   work step is long, never after the step that would have outlived it.
3. **Complete or release, never neither.** Any exit path that leaves a lease held
   makes the room wait out the TTL for information the worker already had.

Run it with:

    python worker/cottage_worker.py --base https://agent-rooms.fly.dev \\
        --room room_... --token $COTTAGE_PARTICIPANT_TOKEN --label worker-main
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger("cottage-worker")

#: What this loop can actually do, and nothing more.
#:
#: `can_execute_background` and `can_initiate_followup` are true because the loop
#: below really does take the next action on its own. `requires_human_presence` is
#: absent for the same reason — and if this file ever grows a prompt for a human,
#: that flag has to come back or the declaration becomes a lie the room will act on.
CAPABILITIES = [
    "can_receive_events",
    "supports_poll",
    "supports_resume",
    "can_initiate_followup",
    "can_execute_background",
    "supports_tools",
]

#: Renew when less than this fraction of the lease remains. Chosen so a renewal is
#: attempted with time left to retry it, rather than at the moment it becomes urgent.
RENEW_AT_FRACTION = 0.4


class CottageError(RuntimeError):
    def __init__(self, code: str, message: str, status: int) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.status = status


@dataclass
class Lease:
    """What the worker believes it holds. `fence` is the part that matters."""

    task_id: str
    fence: int
    expires_at: float
    heartbeat_interval_s: int
    title: str = ""

    def needs_renewal(self, *, now: float, lease_seconds: float) -> bool:
        return (self.expires_at - now) < max(lease_seconds * RENEW_AT_FRACTION, 15.0)


@dataclass
class Worker:
    base: str
    room_id: str
    token: str
    label: str
    poll_seconds: int = 20
    max_cycles: int | None = None
    handler_name: str = "notes"

    cursor: int = 0
    connection_id: str = ""
    attachment_id: str | None = None
    participant_id: str = ""
    lease: Lease | None = None
    stopping: bool = False
    #: Task ids this worker was told to stop. Remembered so it does not immediately
    #: try to reclaim them on the next cycle and spend the room's time being refused.
    forbidden: set[str] = field(default_factory=set)

    # -- transport ---------------------------------------------------------

    def call(
        self, method: str, path: str, payload: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        url = f"{self.base}/api/rooms/{self.room_id}{path}"
        request = urllib.request.Request(
            url,
            data=json.dumps(payload).encode() if payload is not None else None,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
            },
            method=method,
        )
        try:
            with urllib.request.urlopen(
                request, timeout=self.poll_seconds + 40
            ) as response:
                return json.loads(response.read())
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", "replace")
            try:
                body = json.loads(raw)
            except ValueError:
                raise CottageError("http_error", raw[:200], exc.code) from exc
            error = body.get("error")
            code = (
                error if isinstance(error, str) else (error or {}).get("code", "error")
            )
            raise CottageError(code, body.get("message", raw[:200]), exc.code) from exc

    # -- lifecycle ---------------------------------------------------------

    def connect(self) -> None:
        """Attach as a durable runtime, so a restart is recognised as *this* worker.

        The label is what makes executor affinity survive a reconnect (D-032), and
        `attachment_resumable` is true because this process really does reuse the
        same label across restarts — the room is entitled to treat that as a promise.
        """
        result = self.call(
            "POST",
            "/connect",
            {
                "host_class": "persistent_local",
                "capabilities": CAPABILITIES,
                "attachment_label": self.label,
                "attachment_resumable": True,
                "since_seq": self.cursor,
            },
        )
        self.connection_id = result["connection_id"]
        self.attachment_id = result.get("attachment_id")
        self.cursor = max(self.cursor, int(result.get("current_seq") or 0))
        log.info(
            "connected: connection=%s attachment=%s may_claim=%s max_lease=%ss",
            self.connection_id,
            self.attachment_id,
            result.get("may_claim"),
            result.get("max_lease_seconds"),
        )
        if not result.get("may_claim"):
            # Refusing to loop is the honest response: a worker that cannot hold work
            # would otherwise poll forever looking busy while doing nothing.
            raise SystemExit(
                f"this room will not let this participant claim work: "
                f"{result.get('claim_denied_reason')}"
            )

    def hydrate(self) -> dict[str, Any]:
        return self.call(
            "GET", f"/hydrate?since_seq={self.cursor}" if self.cursor else "/hydrate"
        )

    # -- the loop ----------------------------------------------------------

    def run(self) -> None:
        self.connect()
        state = self.hydrate()
        self.participant_id = state.get("you", {}).get("participant_id", "")
        self.adopt_existing_leases(state)

        cycles = 0
        while not self.stopping and (
            self.max_cycles is None or cycles < self.max_cycles
        ):
            cycles += 1
            try:
                self.cycle()
            except CottageError as exc:
                # A refusal is information, not a crash. The loop is the thing that
                # must survive: an unattended worker that exits on the first 409 is
                # attended by whoever restarts it.
                log.warning("cycle %s refused: %s", cycles, exc)
                if exc.code in {"unauthenticated", "forbidden"}:
                    raise
                time.sleep(2)
            except urllib.error.URLError as exc:
                log.warning("network trouble, retrying: %s", exc)
                time.sleep(5)

        self.shutdown()

    def cycle(self) -> None:
        state = self.hydrate()
        self.cursor = max(self.cursor, int(state.get("cursor") or 0))

        # 1. Directives first, always. Reading the board first would let this worker
        #    start something it has already been told not to do.
        for directive in state.get("directives_for_you", []):
            self.obey(directive)
        if self.stopping:
            return

        # 2. Then keep what we hold alive, before spending time on anything else.
        if self.lease is not None:
            self.renew_if_needed()

        # 3. Then work: finish what we hold, or pick something up.
        if self.lease is not None:
            self.advance()
        else:
            self.take_work(state)

        if self.lease is None:
            # Nothing to do. Idling on the long poll is what makes this cheap, and it
            # doubles as the heartbeat that keeps presence honest.
            self.wait()

    def obey(self, directive: dict[str, Any]) -> None:
        """Act on a human's instruction, then record that it was seen.

        Acknowledging is deliberately *after* acting. It is evidence the worker
        noticed, so sending it before complying would make it evidence of nothing.
        """
        action = directive["action"]
        task_id = directive.get("task_id")
        log.info(
            "directive %s: %s (%s)",
            directive["id"],
            action,
            directive.get("reason", ""),
        )

        if action in {"stop", "pause"} and task_id:
            self.forbidden.add(task_id)
            if self.lease is not None and self.lease.task_id == task_id:
                # The room has already halted it; dropping the local lease keeps this
                # worker's belief and the room's state from diverging.
                self.lease = None
        elif action == "resume" and task_id:
            self.forbidden.discard(task_id)

        self.call(
            "POST",
            "/directives/acknowledge",
            {
                "directive_id": directive["id"],
                "note": f"worker {self.label} complied",
                "connection_id": self.connection_id,
            },
        )

    def adopt_existing_leases(self, state: dict[str, Any]) -> None:
        """Pick up leases this worker held before a restart.

        The attachment makes it the same executor, so the room will let it continue.
        Without this the process would restart, find its own work held by itself, and
        wait out a TTL for no reason.
        """
        for held in state.get("your_leases", []):
            self.lease = Lease(
                task_id=held["task_id"],
                fence=int(held["fence"]),
                expires_at=time.time() + float(held.get("seconds_remaining") or 0),
                heartbeat_interval_s=int(held.get("heartbeat_interval_s") or 20),
                title=held.get("title", ""),
            )
            log.info(
                "resumed lease on %s (fence %s)", self.lease.task_id, self.lease.fence
            )
            return

    def take_work(self, state: dict[str, Any]) -> None:
        candidates = [
            task
            for task in sorted(
                state.get("claimable", []) or [],
                key=lambda t: (-int(t.get("priority") or 0), t.get("created_at", "")),
            )
            if task["task_id"] not in self.forbidden
        ]
        if not candidates:
            return

        task = candidates[0]
        try:
            result = self.call(
                "POST",
                "/tasks/claim",
                {"task_id": task["task_id"], "connection_id": self.connection_id},
            )
        except CottageError as exc:
            if exc.code == "steering_halted":
                # Told not to, before we ever read the directive. Remember it rather
                # than rediscovering it every cycle.
                self.forbidden.add(task["task_id"])
                return
            if exc.code == "not_found":
                return
            if exc.code in {"lease_conflict", "executor_conflict"}:
                log.info("someone else has %s", task["task_id"])
                return
            raise

        claim = result["task"]["claim"]
        self.lease = Lease(
            task_id=task["task_id"],
            fence=int(claim["fence"]),
            expires_at=time.time() + self.seconds_until(claim["expires_at"]),
            heartbeat_interval_s=int(claim.get("heartbeat_interval_s") or 20),
            title=result["task"].get("title", ""),
        )
        log.info("claimed %s (fence %s)", self.lease.task_id, self.lease.fence)
        self.call(
            "POST",
            "/work",
            {
                "headline": f"Working: {self.lease.title}"[:200],
                "task_id": self.lease.task_id,
                "targets": result["task"].get("targets") or [],
                "note": f"Unattended worker {self.label}, no human attending.",
                "connection_id": self.connection_id,
            },
        )

    def renew_if_needed(self) -> None:
        assert self.lease is not None
        now = time.time()
        if not self.lease.needs_renewal(now=now, lease_seconds=300):
            return
        try:
            result = self.call(
                "POST",
                "/tasks/renew",
                {
                    "task_id": self.lease.task_id,
                    "fence": self.lease.fence,
                    "connection_id": self.connection_id,
                },
            )
        except CottageError as exc:
            # Losing a lease is normal and recoverable; pretending otherwise is not.
            log.warning("lost the lease on %s: %s", self.lease.task_id, exc)
            self.lease = None
            return
        claim = result["task"]["claim"]
        self.lease.expires_at = now + self.seconds_until(claim["expires_at"])
        log.debug("renewed %s", self.lease.task_id)

    def advance(self) -> None:
        """Do one step of actual work, then finish or report.

        One step per cycle on purpose: between steps the loop re-reads directives, so
        the longest a stop can take to be obeyed is one step rather than one task.
        """
        assert self.lease is not None
        outcome = HANDLERS[self.handler_name](self.lease)
        try:
            self.call(
                "POST",
                "/tasks/complete",
                {
                    "task_id": self.lease.task_id,
                    "fence": self.lease.fence,
                    "result": outcome,
                    "connection_id": self.connection_id,
                },
            )
            log.info("completed %s", self.lease.task_id)
        except CottageError as exc:
            if exc.code == "steering_halted":
                log.info("told to stop %s before finishing it", self.lease.task_id)
                self.forbidden.add(self.lease.task_id)
            elif exc.code in {"stale_fence", "lease_required", "executor_conflict"}:
                log.info("no longer ours: %s", exc)
            else:
                raise
        finally:
            self.lease = None

    def wait(self) -> None:
        """Stay reachable, then wait an interval.

        The heartbeat is not optional and not decorative: presence is derived from
        heartbeat age, so a worker that skipped it would be graded stale within three
        intervals and have its leases reaped while it was still working — the room
        would be correct and the worker would be gone.

        The HTTP surface has a pull endpoint and an SSE stream but no long poll; the
        MCP adapter has one (`await_room_events`). That asymmetry is a real parity
        gap and it is recorded rather than papered over: this loop polls on an
        interval, which is what `supports_poll` claims and all it claims.
        """
        try:
            self.call(
                "POST",
                "/heartbeat",
                {"connection_id": self.connection_id},
            )
            result = self.call("GET", f"/events?since_seq={self.cursor}&limit=50")
            self.cursor = max(self.cursor, int(result.get("cursor") or self.cursor))
        except CottageError as exc:
            if exc.code == "invalid_cursor":
                # Ahead of the room: only reachable if the room was rebuilt under us.
                log.warning("cursor %s is ahead of the room; resetting", self.cursor)
                self.cursor = 0
            else:
                raise
        time.sleep(self.poll_seconds)

    def shutdown(self) -> None:
        """Leave nothing held.

        A worker that exits holding a lease costs the room a full TTL of waiting for
        something this process already knew — and "it will expire eventually" is the
        answer leases exist so that nobody has to accept.
        """
        if self.lease is not None:
            try:
                self.call(
                    "POST",
                    "/tasks/release",
                    {
                        "task_id": self.lease.task_id,
                        "fence": self.lease.fence,
                        "note": "worker shutting down",
                        "connection_id": self.connection_id,
                    },
                )
                log.info("released %s on the way out", self.lease.task_id)
            except CottageError as exc:
                log.warning("could not release cleanly: %s", exc)
            self.lease = None
        if self.connection_id:
            try:
                self.call(
                    "POST", f"/disconnect?connection_id={self.connection_id}", None
                )
            except CottageError:
                pass
        log.info("stopped")

    @staticmethod
    def seconds_until(iso: str) -> float:
        from datetime import datetime, timezone

        when = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        return max((when - datetime.now(timezone.utc)).total_seconds(), 0.0)


# ---------------------------------------------------------------------------
# Handlers: what "doing the work" means. Deliberately small and side-effect free.
# ---------------------------------------------------------------------------


def handle_notes(lease: Lease) -> str:
    """The default. Produces a real, checkable result and touches nothing outside.

    A demo handler that shelled out would make the proof about the handler rather
    than about the loop, and would put arbitrary execution in a file whose job is to
    show that coordination works.
    """
    return (
        f"Completed by an unattended worker with no human attending. "
        f"Task '{lease.title}' was claimed at fence {lease.fence} and finished by the "
        f"same runtime that claimed it."
    )


HANDLERS = {"notes": handle_notes}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="An unattended Cottage worker.")
    parser.add_argument(
        "--base", default=os.environ.get("COTTAGE_BASE", "https://agent-rooms.fly.dev")
    )
    parser.add_argument("--room", default=os.environ.get("COTTAGE_ROOM"))
    parser.add_argument("--token", default=os.environ.get("COTTAGE_PARTICIPANT_TOKEN"))
    parser.add_argument(
        "--label", default=os.environ.get("COTTAGE_LABEL", "worker-main")
    )
    parser.add_argument("--poll-seconds", type=int, default=20)
    parser.add_argument("--max-cycles", type=int, default=None)
    parser.add_argument("--handler", default="notes", choices=sorted(HANDLERS))
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
    )
    if not args.room or not args.token:
        parser.error(
            "--room and --token are required (or COTTAGE_ROOM / COTTAGE_PARTICIPANT_TOKEN)"
        )

    worker = Worker(
        base=args.base.rstrip("/"),
        room_id=args.room,
        token=args.token,
        label=args.label,
        poll_seconds=args.poll_seconds,
        max_cycles=args.max_cycles,
        handler_name=args.handler,
    )

    def stop(*_: Any) -> None:
        # Sets a flag rather than exiting, so the loop reaches `shutdown` and gives
        # its leases back instead of leaving the room to time them out.
        log.info("shutdown requested; finishing this cycle")
        worker.stopping = True

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)

    try:
        worker.run()
    except SystemExit as exc:
        log.error("%s", exc)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
