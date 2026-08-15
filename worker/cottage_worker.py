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

    export COTTAGE_PARTICIPANT_TOKEN="<runtime credential>"
    python worker/cottage_worker.py --base https://agent-rooms.fly.dev \\
        --room room_... --label worker-main
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sys
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any

from executors import EchoExecutor, Executor, StepContext, StepResult
from executors import build as build_executor

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
    description: str = ""
    targets: list[str] = field(default_factory=list)

    def needs_renewal(self, *, now: float, lease_seconds: float) -> bool:
        return (self.expires_at - now) < max(lease_seconds * RENEW_AT_FRACTION, 15.0)


def _resume_state(raw: dict[str, object], *, step: int) -> dict[str, Any]:
    """Coerce an executor's bookmark into the room's closed schema.

    Unknown keys are **dropped here rather than sent**, because the server rejects
    them and a rejected checkpoint loses the progress it was recording. This is also
    the one place an executor's own dictionary meets the room, so it is the right
    place to stop anything transcript-shaped from travelling: only these five fields
    exist, and none of them is free-form context.
    """
    allowed = {
        "phase",
        "completed_step_ids",
        "artifact_refs",
        "pending_tool_calls",
        "next_action",
    }
    state: dict[str, Any] = {k: v for k, v in raw.items() if k in allowed}
    state.setdefault("phase", f"step-{step}")
    for key in ("completed_step_ids", "artifact_refs", "pending_tool_calls"):
        value = state.get(key)
        if value is not None and not isinstance(value, list):
            state[key] = [str(value)]
    return state


def join_with_invitation(
    base: str,
    invitation_token: str,
    *,
    display_name: str,
    description: str = "",
) -> tuple[str, str]:
    """Walk in with nothing but the key, and come back with a seat.

    This is the product's own sentence completed for an unattended agent: someone
    starts a room, hands over a key, and a process that has never seen this
    organization joins on the strength of that key alone. Before this the worker
    needed a participant token, which meant a human first joined by hand and then
    passed a credential to a process — the same token-hunting the front door was
    opened to remove.

    Returns `(room_id, participant_token)`.
    """
    payload = {
        "invitation_token": invitation_token,
        "display_name": display_name,
        "host_class": "persistent_local",
        "capabilities": CAPABILITIES,
        "description": description,
    }
    request = urllib.request.Request(
        f"{base}/api/rooms/join",
        data=json.dumps(payload).encode(),
        # The invitation authenticates the request *and* is the thing being redeemed.
        # A stranger holding it needs no account, which is the whole point of D-025.
        headers={
            "Authorization": f"Bearer {invitation_token}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            body = json.loads(response.read())
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        raise CottageError("join_failed", raw[:300], exc.code) from exc
    return body["room"]["id"], body["participant_token"]


@dataclass
class Worker:
    base: str
    room_id: str
    token: str
    label: str
    poll_seconds: int = 20
    max_cycles: int | None = None
    #: What "doing the work" means. The loop never inspects it beyond the protocol
    #: in `worker/executors.py`, which is what lets a model-backed one be swapped in
    #: without the coordination guarantees being re-argued.
    executor: Executor = field(default_factory=EchoExecutor)
    #: How many cycles a claimed task takes. The default handler used to do
    #: everything in one call and finish in two seconds, which made the loop's own
    #: comment — "one step per cycle, so a stop is obeyed within one step" — vacuous:
    #: there was only ever one step, and a human could not have preempted it if they
    #: had been watching for it. Real work takes time, and a worker that cannot be
    #: interrupted mid-task is not demonstrating anything about interruption.
    steps_per_task: int = 12
    #: Lease seconds to request. Deliberately short relative to `steps_per_task` so a
    #: task outlives its lease several times over and renewal is actually exercised
    #: rather than merely implemented.
    lease_seconds: int = 60
    #: Whether to take work nobody addressed to it.
    #:
    #: Defaults to **false**, and that default was earned the hard way: on its first
    #: live run this worker took the highest-priority open task on the board — a
    #: long-running architecture task another participant was steering — and closed
    #: it with a canned result. Nothing malfunctioned. "Claim the best open task" is
    #: simply the wrong policy for an unattended process, because `open` means
    #: nobody holds it, never that it is anybody's to take.
    #:
    #: With this false the worker takes only work *proposed to it*, which is the
    #: room's existing assignment mechanism and an explicit act by another
    #: participant. Turning it on is a deliberate choice for a room that is a queue.
    take_unassigned: bool = False

    cursor: int = 0
    connection_id: str = ""
    attachment_id: str | None = None
    participant_id: str = ""
    lease: Lease | None = None
    stopping: bool = False
    #: Steps completed per task. Seeded from the room's checkpoints on start, so a
    #: restarted process resumes where it stopped instead of redoing work — which is
    #: the difference between durable progress and a progress-shaped log line.
    progress: dict[str, int] = field(default_factory=dict)
    #: Public summaries this worker has recorded, per task, oldest first. Passed to
    #: the executor so a fresh process has the same account of the work the room does.
    checkpoints: dict[str, list[str]] = field(default_factory=dict)
    #: Things humans have told this worker about a task: `input` directives, and
    #: answers to its own questions. Kept per task because that is the grain an
    #: executor can act on, and never merged into anything resembling a transcript.
    instructions: dict[str, list[str]] = field(default_factory=dict)
    #: Answers already folded in, so re-reading hydration does not duplicate them.
    seen_answers: set[str] = field(default_factory=set)
    #: How often to check for a stop *while a step is running*, and to renew. Zero
    #: disables the watcher, which is right for an executor that returns instantly:
    #: the loop's ordering rule already bounds a stop by one step. It is wrong for
    #: one that shells out, where a step can outlive the lease that authorises it.
    watch_interval_seconds: float = 5.0
    #: Provider or model to declare, if its operator chooses to say. Empty by default
    #: and empty is the honest answer for the subprocess executor: it delegates to a
    #: CLI and genuinely does not know which model answered. Naming one it cannot
    #: observe would be a claim about someone else's system.
    declared_model: str = ""
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
                # This loop pulls; it does not hold an SSE stream open. Saying so is
                # what makes the room grade it `live_poll` and unattended instead of
                # `attended_pull`, which would have been a lie about a process with
                # no human anywhere near it.
                "transport": "long_poll",
                "attachment_label": self.label,
                "attachment_resumable": True,
                # Say what this runtime is and how it works, so the room does not have
                # to guess from a host label — and so nobody reads a background
                # process as somebody's chat window (D-054). Self-reported, and the
                # room records it as such.
                "runtime_role": "companion",
                "executor_kind": getattr(self.executor, "name", "unknown"),
                "executor_model": self.declared_model,
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
        self.adopt_recorded_progress(state)
        self.absorb_answers(state)

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
        self.absorb_answers(state)
        # Every cycle, not only at startup. Hydration carries checkpoints for the
        # tasks this seat *currently holds*, so a worker that restarted and had to
        # re-claim only learns its own history after the claim lands — and a startup-
        # only read would have it begin again at step one, which is the exact failure
        # checkpoints exist to remove.
        self.adopt_recorded_progress(state)

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

        # Every cycle ends here, holding a lease or not. When a task took one cycle
        # this only ran while idle; multi-step work would have spun without it,
        # hammering the server and — worse — never heartbeating, so the room would
        # have reaped the lease of a worker that was busy the whole time.
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
            self.progress.pop(task_id, None)
            if self.lease is not None and self.lease.task_id == task_id:
                # The room has already halted it; dropping the local lease keeps this
                # worker's belief and the room's state from diverging.
                self.lease = None
        elif action == "resume" and task_id:
            self.forbidden.discard(task_id)
        elif action == "input" and task_id:
            # The one directive that carries content rather than control. It is kept
            # per task and handed to the executor as an instruction — data the work
            # takes into account, never something this loop executes. Room content is
            # untrusted text (`docs/SECURITY.md`), and a directive is room content.
            reason = (directive.get("reason") or "").strip()
            if reason:
                self.instructions.setdefault(task_id, []).append(reason)

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
                targets=list(held.get("targets") or []),
            )
            log.info(
                "resumed lease on %s (fence %s)", self.lease.task_id, self.lease.fence
            )
            return

    def adopt_recorded_progress(self, state: dict[str, Any]) -> None:
        """Take the room's account of what this worker has already done.

        Before checkpoints existed the step counter lived in this process, so a
        restart silently began again at step one — redoing work and reporting it as
        new. The room now holds the record, which means the *durable* answer and the
        worker's answer are the same one rather than two that can disagree.

        The count comes from `completed_step_ids` where a bookmark recorded them and
        from the number of checkpoints otherwise, because a checkpoint is written per
        step; an executor that stops writing them will simply resume earlier than it
        might have, which is the safe direction to be wrong in.
        """
        for task_id, records in (state.get("checkpoints") or {}).items():
            if not records:
                continue
            self.checkpoints[task_id] = [r.get("summary", "") for r in records]
            resume = (records[-1] or {}).get("resume_state") or {}
            completed = resume.get("completed_step_ids") or []
            recorded = len(completed) if completed else len(records)
            self.progress[task_id] = max(self.progress.get(task_id, 0), int(recorded))
            log.info(
                "resuming %s from step %s (%s checkpoints, phase %r)",
                task_id,
                self.progress[task_id],
                len(records),
                resume.get("phase", ""),
            )

    def absorb_answers(self, state: dict[str, Any]) -> None:
        """Turn replies to this worker's own questions into instructions it can act on.

        From hydration rather than the event stream, because a restarted process
        starts at the current cursor — so the one event it most needs is the one
        already behind it. The room carries the answer in the resume payload instead.

        An answer is *information this worker asked for*. It reaches the executor as
        data, in the same channel as an `input` directive, and nothing in this loop
        interprets it: room content is untrusted text (`docs/SECURITY.md`).
        """
        for answer in state.get("answers_for_you", []) or []:
            answer_id = answer.get("answer_id") or ""
            task_id = answer.get("task_id") or ""
            body = (answer.get("body") or "").strip()
            if (
                not answer_id
                or not task_id
                or not body
                or answer_id in self.seen_answers
            ):
                continue
            self.seen_answers.add(answer_id)
            self.instructions.setdefault(task_id, []).append(body)
            # The room returned the task to `open` when the answer landed; forgetting
            # the refusal is what lets this worker pick it back up next cycle.
            self.forbidden.discard(task_id)
            log.info("answered on %s: %s", task_id, body[:120])

    def take_work(self, state: dict[str, Any]) -> None:
        """Pick up work that is *for* this worker.

        Proposals first, and by default only proposals: a proposal is another
        participant deliberately handing this worker a job, where an open task is
        merely one nobody currently holds. Treating the second as an invitation is
        how an unattended process quietly empties a shared board.
        """
        offered = [
            {
                "task_id": p["task_id"],
                "title": p.get("title", ""),
                "priority": 0,
                "targets": [],
            }
            for p in state.get("proposed_to_you", []) or []
        ]
        pool = (
            offered
            if not self.take_unassigned
            else offered + list(state.get("claimable", []) or [])
        )
        candidates = [
            task
            for task in sorted(
                pool,
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
                {
                    "task_id": task["task_id"],
                    "requested_lease_seconds": self.lease_seconds,
                    "connection_id": self.connection_id,
                },
            )
        except CottageError as exc:
            if exc.code == "steering_halted":
                # Told not to, before we ever read the directive. Remember it rather
                # than rediscovering it every cycle.
                self.forbidden.add(task["task_id"])
                return
            if exc.code in {"not_found", "invalid_command"}:
                # Offered work that cannot be taken — already finished, or gone.
                # Remembered rather than retried: a loop that re-attempts the same
                # refusal every cycle is busy without being useful.
                self.forbidden.add(task["task_id"])
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
            description=result["task"].get("description", ""),
            targets=list(result["task"].get("targets") or []),
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
        if not self.lease.needs_renewal(now=now, lease_seconds=self.lease_seconds):
            return
        try:
            result = self.call(
                "POST",
                "/tasks/renew",
                {
                    "task_id": self.lease.task_id,
                    "fence": self.lease.fence,
                    "extend_seconds": self.lease_seconds,
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
        log.info("renewed %s (expires in %ss)", self.lease.task_id, self.lease_seconds)

    def advance(self) -> None:
        """Do one step of work, record it, and finish only when there is none left.

        One step per cycle is the load-bearing part: between steps the loop returns to
        the top, re-reads directives, and renews. So the longest a stop can take to be
        obeyed is one step — and a task that took a single step made that guarantee
        true but empty, which is how the first preemption attempt found nothing left
        to preempt.

        The step itself is done by an `Executor` this loop knows nothing about beyond
        its interface (`worker/executors.py`). That separation is the safety property
        rather than tidiness: swapping in something model-backed must not be able to
        break lease renewal, and a bug in lease renewal must not be reachable from a
        prompt.
        """
        assert self.lease is not None
        task_id = self.lease.task_id
        step = self.progress.get(task_id, 0) + 1

        if step == 1:
            # Say on the board that this is being worked, not merely held.
            try:
                self.call(
                    "POST",
                    "/tasks/update",
                    {
                        "task_id": task_id,
                        "fence": self.lease.fence,
                        "in_progress": True,
                        "connection_id": self.connection_id,
                    },
                )
            except CottageError as exc:
                log.warning("could not mark in_progress: %s", exc)

        result = self.run_step_watched(
            StepContext(
                task_id=task_id,
                title=self.lease.title,
                description=self.lease.description,
                targets=tuple(self.lease.targets),
                step=step,
                total_steps=self.steps_per_task,
                instructions=tuple(self.instructions.get(task_id, ())),
                checkpoints=tuple(self.checkpoints.get(task_id, ())),
            )
        )
        if result is None:
            # Stopped mid-step. The room already halted the task and the executor's
            # child is dead; there is nothing to record and nothing to complete.
            return
        if result.concern:
            log.warning("step %s on %s: %s", step, task_id, result.concern)

        if result.question:
            # Asked *before* the checkpoint is written separately, because a blocking
            # ask writes its own checkpoint inside the same transaction as the release
            # — two checkpoints for one step would put the same progress on the board
            # twice and make the count meaningless.
            self.ask(result, task_id=task_id, step=step)
            return

        self.record_checkpoint(result, task_id=task_id, step=step)
        self.progress[task_id] = step

        if not result.done and step < self.steps_per_task:
            log.info("step %s/%s on %s", step, self.steps_per_task, task_id)
            return

        try:
            self.call(
                "POST",
                "/tasks/complete",
                {
                    "task_id": task_id,
                    "fence": self.lease.fence,
                    "result": result.summary,
                    "connection_id": self.connection_id,
                },
            )
            log.info("completed %s", task_id)
        except CottageError as exc:
            if exc.code == "steering_halted":
                log.info("told to stop %s before finishing it", task_id)
                self.forbidden.add(task_id)
            elif exc.code in {"stale_fence", "lease_required", "executor_conflict"}:
                log.info("no longer ours: %s", exc)
            else:
                raise
        finally:
            self.progress.pop(task_id, None)
            self.checkpoints.pop(task_id, None)
            self.lease = None

    def run_step_watched(self, context: StepContext) -> StepResult | None:
        """Run one step, watching for a stop while it runs.

        A step that returns in milliseconds needs none of this — the loop's ordering
        rule already means the longest a stop can wait is one step. A step that shells
        out to an agent CLI is different: it can run for minutes, and "obeyed at the
        next step boundary" would be a stop that visibly does nothing while the thing
        it stopped keeps working. So the step runs on a thread and this polls the
        room; when a halt arrives, `cancel()` takes the child's whole process tree
        down and the step is abandoned.

        The lease is renewed here too. Without that, a long step would let a lease
        lapse *while the work was still running* — the room would reap it, someone
        else could take the task, and two runtimes would be doing it at once, which is
        the failure leases exist to prevent (`docs/PROTOCOL.md` §4).

        Returns `None` when the step was abandoned.
        """
        assert self.lease is not None
        if self.watch_interval_seconds <= 0:
            return self.executor.run_step(context)

        outcome: dict[str, StepResult] = {}
        failure: dict[str, BaseException] = {}

        def worker() -> None:
            try:
                outcome["result"] = self.executor.run_step(context)
            except BaseException as exc:  # noqa: BLE001 - re-raised on the loop thread
                failure["error"] = exc

        thread = threading.Thread(
            target=worker, name=f"step-{context.step}", daemon=True
        )
        thread.start()
        while thread.is_alive():
            thread.join(self.watch_interval_seconds)
            if not thread.is_alive():
                break
            if self.stopping or self.halted(context.task_id):
                log.info(
                    "stopping step %s on %s mid-flight", context.step, context.task_id
                )
                self.executor.cancel()
                thread.join(30)
                return None
            # Both, and the heartbeat is the one that was missing. Renewal keeps the
            # *lease* alive; presence is graded on heartbeat age, and a step that
            # thinks for longer than a few heartbeat intervals had its seat graded
            # stale and its own claim reaped out from under it — after which `halted`
            # correctly saw no claim and abandoned the step. A worker that is plainly
            # working must not read as absent.
            self.beat()
            self.renew_if_needed()

        if "error" in failure:
            raise failure["error"]
        return outcome.get("result")

    def halted(self, task_id: str) -> bool:
        """Whether a human has halted this task since the step began.

        Read straight from the room rather than from a cached directive list: the
        whole point is to notice something that arrived *after* this cycle's
        hydration. A failed read is treated as "not halted" — the loop must not stop
        working because a poll timed out, and the next boundary check will catch it.
        """
        try:
            state = self.call("GET", f"/tasks/{task_id}")
        except (CottageError, urllib.error.URLError):
            return False
        task = state.get("task") or {}
        if task.get("steering") in {"paused", "stopped"}:
            return True
        claim = task.get("claim")
        return claim is None or claim.get("participant_id") != self.participant_id

    def record_checkpoint(self, result: StepResult, *, task_id: str, step: int) -> None:
        """Write progress to the room, so a restart is not an amnesia.

        Failure here is logged and not fatal. A worker that died because it could not
        record its progress would trade a recoverable gap for a lost lease — and the
        room's next reader would then have neither the progress *nor* the worker.
        """
        assert self.lease is not None
        payload: dict[str, Any] = {
            "task_id": task_id,
            "fence": self.lease.fence,
            "summary": result.summary[:1200],
            "connection_id": self.connection_id,
            # Idempotent per (task, fence, step): the moment a worker checkpoints is
            # the moment it is most likely to be interrupted, so the retry must not
            # append a second record of the same step.
            "command_id": f"ckp-{task_id}-{self.lease.fence}-{step}",
        }
        if result.resume:
            payload["resume_state"] = _resume_state(result.resume, step=step)
        try:
            self.call("POST", "/tasks/checkpoint", payload)
        except CottageError as exc:
            log.warning("could not checkpoint %s step %s: %s", task_id, step, exc)
            return
        self.checkpoints.setdefault(task_id, []).append(result.summary)

    def ask(self, result: StepResult, *, task_id: str, step: int) -> None:
        """Raise what the executor could not work out for itself.

        A blocking ask gives the lease back in the same transaction that parks the
        task, so this worker stops holding work it cannot advance — and is free to
        pick up anything else. The task is not offered to another claimant while the
        question stands, because the next worker would hit the same wall.
        """
        assert self.lease is not None
        payload: dict[str, Any] = {
            "body": result.question or "",
            "task_id": task_id,
            "blocking": result.blocking,
            "connection_id": self.connection_id,
            "command_id": f"qst-{task_id}-{self.lease.fence}-{step}",
        }
        if result.blocking:
            payload["fence"] = self.lease.fence
            payload["checkpoint_summary"] = result.summary[:1200]
            if result.resume:
                payload["resume_state"] = _resume_state(result.resume, step=step)
        try:
            self.call("POST", "/questions", payload)
        except CottageError as exc:
            log.warning("could not ask about %s: %s", task_id, exc)
            return
        log.info(
            "asked%s about %s: %s",
            " (standing down)" if result.blocking else "",
            task_id,
            (result.question or "")[:120],
        )
        if result.blocking:
            # The room released it as part of the ask. Dropping the local lease keeps
            # this worker's belief and the room's state from diverging — the same rule
            # applied when a directive stops us.
            self.progress.pop(task_id, None)
            self.lease = None

    def beat(self) -> None:
        """Say we are still here.

        Separate from `wait` because the two callers have opposite shapes: the idle
        loop beats between polls, and a *working* loop has to beat from inside a step
        that may run for many minutes. Only the first existed, so a companion doing
        long work went stale precisely because it was busy.

        Never fatal. A missed beat costs presence grading, which recovers on the next
        one; raising here would cost the step.
        """
        try:
            self.call("POST", "/heartbeat", {"connection_id": self.connection_id})
        except (CottageError, urllib.error.URLError) as exc:
            log.debug("heartbeat failed, continuing: %s", exc)

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
        self.beat()
        try:
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


def _refuses(env_name: str) -> type[argparse.Action]:
    """An option that exists only to explain why it is not accepted.

    A credential given on a command line is readable by every process listing on the
    machine for the whole life of the worker, which is how the running workers' tokens
    were recovered during testing. Removing the option outright would meet an operator
    following an older recipe with `unrecognized arguments`, so the flag stays and
    refuses with the reason.
    """

    class Refuse(argparse.Action):
        def __call__(self, parser, namespace, values, option_string=None):  # type: ignore[no-untyped-def]
            parser.error(
                f"{option_string} is not accepted: a value passed on a command line is "
                f"visible in process listings to anything that can read them. Set the "
                f"{env_name} environment variable instead."
            )

    return Refuse


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="An unattended Cottage worker.")
    parser.add_argument(
        "--base", default=os.environ.get("COTTAGE_BASE", "https://agent-rooms.fly.dev")
    )
    parser.add_argument("--room", default=os.environ.get("COTTAGE_ROOM"))
    parser.add_argument(
        "--token",
        nargs="?",
        action=_refuses("COTTAGE_PARTICIPANT_TOKEN"),
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--invitation",
        nargs="?",
        action=_refuses("COTTAGE_INVITATION"),
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--display-name",
        default=os.environ.get("COTTAGE_DISPLAY_NAME", "Unattended worker"),
        help="How this worker appears in the room when joining with --invitation.",
    )
    parser.add_argument(
        "--label", default=os.environ.get("COTTAGE_LABEL", "worker-main")
    )
    parser.add_argument("--poll-seconds", type=int, default=20)
    parser.add_argument("--max-cycles", type=int, default=None)
    parser.add_argument(
        "--executor",
        default=os.environ.get("COTTAGE_EXECUTOR", "echo"),
        choices=["echo", "subprocess"],
        help=(
            "How a step gets done. 'echo' is deterministic and credential-free, and "
            "is what the coordination guarantees are tested against. 'subprocess' "
            "delegates to an agent CLI its owner already runs and already authorized."
        ),
    )
    parser.add_argument(
        "--executor-command",
        default=os.environ.get("COTTAGE_EXECUTOR_COMMAND"),
        help=(
            "The agent CLI to run for --executor subprocess, as a fixed command. The "
            "task goes to it over stdin, so it must NOT interpolate the prompt."
        ),
    )
    parser.add_argument(
        "--executor-cwd",
        default=os.environ.get("COTTAGE_EXECUTOR_CWD"),
        help="Working directory for the child. Give it one rather than inheriting ours.",
    )
    parser.add_argument(
        "--executor-env",
        action="append",
        default=[],
        metavar="NAME",
        help=(
            "Extra environment variable to pass to the child, by name. The child "
            "otherwise gets an allowlist only, because this process holds a room "
            "credential in its environment. Repeatable."
        ),
    )
    parser.add_argument(
        "--declare-model",
        default=os.environ.get("COTTAGE_DECLARE_MODEL", ""),
        help=(
            "Provider or model to declare in the room, if you want to say. Left "
            "empty by default: a worker delegating to an agent CLI does not observe "
            "which model answered, and naming one anyway would be a guess presented "
            "as a fact."
        ),
    )
    parser.add_argument(
        "--executor-timeout",
        type=int,
        default=int(os.environ.get("COTTAGE_EXECUTOR_TIMEOUT", "180")),
        help="Seconds a single step may take before its process tree is killed.",
    )
    parser.add_argument(
        "--ask-at-step",
        type=int,
        default=None,
        help=(
            "Echo executor only: raise a blocking question before this step, to "
            "exercise the ask/answer path deterministically."
        ),
    )
    parser.add_argument(
        "--steps",
        type=int,
        default=12,
        help="Cycles of work a claimed task takes before it is completed.",
    )
    parser.add_argument(
        "--lease-seconds",
        type=int,
        default=60,
        help="Lease length to request. Short on purpose, so renewal is exercised.",
    )
    parser.add_argument(
        "--take-unassigned",
        action="store_true",
        help=(
            "Also claim open tasks nobody proposed to this worker. Off by default: "
            "'open' means unheld, not unowned."
        ),
    )
    parser.add_argument(
        "--log-file",
        default=os.environ.get("COTTAGE_LOG_FILE"),
        help=(
            "Append the worker's log here as well as to the console. A companion "
            "outlives the terminal that started it, and its console output dies with "
            "that terminal — which leaves an exited worker with no recoverable reason."
        ),
    )
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    handlers: list[logging.Handler] = [logging.StreamHandler()]
    if args.log_file:
        # `logging` flushes each record as it emits, so a worker killed mid-cycle has
        # still written the line that says what it was doing.
        handlers.append(logging.FileHandler(args.log_file, encoding="utf-8"))
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        handlers=handlers,
    )
    # Credentials come from the environment only; the flags above exist to say so.
    invitation = os.environ.get("COTTAGE_INVITATION")
    room_id, token = args.room, os.environ.get("COTTAGE_PARTICIPANT_TOKEN")
    if invitation:
        room_id, token = join_with_invitation(
            args.base.rstrip("/"),
            invitation,
            display_name=args.display_name,
            description=(
                "Unattended executor. Polls, renews its own leases, and takes only "
                "work proposed to it. It runs a fixed handler and does not reason."
            ),
        )
        log.info("joined %s as %s", room_id, args.display_name)
    if not room_id or not token:
        parser.error(
            "no credential in the environment: set COTTAGE_INVITATION (a room key), "
            "or COTTAGE_ROOM + COTTAGE_PARTICIPANT_TOKEN. Neither is accepted as a "
            "command-line argument."
        )

    worker = Worker(
        base=args.base.rstrip("/"),
        room_id=room_id,
        token=token,
        label=args.label,
        poll_seconds=args.poll_seconds,
        max_cycles=args.max_cycles,
        executor=build_executor(
            args.executor,
            command=args.executor_command,
            ask_at_step=args.ask_at_step,
            cwd=args.executor_cwd,
            env_passthrough=args.executor_env,
            timeout_seconds=args.executor_timeout,
        ),
        # No watcher for the instant executor: its steps end before anything could
        # interrupt them, and polling between them would be pure cost.
        watch_interval_seconds=0.0 if args.executor == "echo" else 5.0,
        declared_model=args.declare_model,
        take_unassigned=args.take_unassigned,
        steps_per_task=args.steps,
        lease_seconds=args.lease_seconds,
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
