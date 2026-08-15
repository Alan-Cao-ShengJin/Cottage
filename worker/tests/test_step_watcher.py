"""What the loop does *while* a step is running.

Untested until now, which is how a companion came to go stale precisely because it was
busy. The watcher renewed its lease on schedule and never once said it was still there;
presence is graded on heartbeat age, so the seat was declared absent, the reaper released
the claim, and the next `halted()` check — correctly seeing no claim — abandoned the step.
The worker then re-claimed and repeated, roughly every twenty seconds.

Every guarantee below is about a step that takes longer than one watch interval, because
that is the only case where any of this is reachable.
"""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import cottage_worker  # noqa: E402
from cottage_worker import CottageError, Lease, Worker  # noqa: E402
from executors import Executor, StepContext, StepResult  # noqa: E402


class SlowExecutor(Executor):
    """A step that outlives several watch intervals, and notices being cancelled."""

    def __init__(self, seconds: float = 0.5) -> None:
        self.seconds = seconds
        self.cancelled = threading.Event()
        self.started = threading.Event()

    def run_step(self, context: StepContext) -> StepResult:
        self.started.set()
        deadline = time.monotonic() + self.seconds
        while time.monotonic() < deadline:
            if self.cancelled.is_set():
                raise RuntimeError("cancelled")
            time.sleep(0.01)
        return StepResult(summary="done", done=True)

    def cancel(self) -> None:
        self.cancelled.set()


def _worker(executor: Executor, **kwargs) -> Worker:
    kwargs.setdefault("watch_interval_seconds", 0.05)
    worker = Worker(
        base="http://room.invalid",
        room_id="room_test",
        token="unused",
        label="companion-test",
        executor=executor,
        **kwargs,
    )
    worker.connection_id = "con_test"
    worker.participant_id = "par_test"
    worker.lease = Lease(
        task_id="tsk_test",
        fence=1,
        # Far in the future: renewal is a separate concern from staying present, and
        # these tests are about the second.
        expires_at=time.time() + 10_000,
        heartbeat_interval_s=20,
    )
    return worker


def _context() -> StepContext:
    return StepContext(
        task_id="tsk_test",
        title="A long think",
        description="",
        targets=(),
        step=1,
        total_steps=1,
        instructions=(),
        checkpoints=(),
    )


def test_a_running_step_keeps_heartbeating(monkeypatch):
    """The regression. A worker that is plainly working must not read as absent."""
    executor = SlowExecutor(seconds=0.4)
    worker = _worker(executor)
    beats: list[dict] = []

    def fake_call(method, path, payload=None):
        if path == "/heartbeat":
            beats.append(payload)
            return {"ok": True}
        if path.startswith("/tasks/"):  # halted() poll
            return {"task": {"steering": "running", "claim": {"participant_id": "par_test"}}}
        raise AssertionError(f"unexpected call {method} {path}")

    monkeypatch.setattr(worker, "call", fake_call)

    assert worker.run_step_watched(_context()) is not None
    assert len(beats) >= 3, f"only {len(beats)} heartbeats across a step of ~8 intervals"
    assert all(b == {"connection_id": "con_test"} for b in beats)


def test_a_failing_heartbeat_does_not_kill_the_step(monkeypatch):
    """A missed beat costs presence grading, which recovers. Raising would cost the work."""
    executor = SlowExecutor(seconds=0.3)
    worker = _worker(executor)

    def fake_call(method, path, payload=None):
        if path == "/heartbeat":
            raise CottageError(503, "transport", "the network blinked")
        if path.startswith("/tasks/"):
            return {"task": {"steering": "running", "claim": {"participant_id": "par_test"}}}
        raise AssertionError(f"unexpected call {method} {path}")

    monkeypatch.setattr(worker, "call", fake_call)

    result = worker.run_step_watched(_context())
    assert result is not None and result.summary == "done"


def test_a_reaped_claim_still_abandons_the_step(monkeypatch):
    """The heartbeat must not paper over a genuine loss of the lease.

    If the claim really is gone — taken over, force-released, expired despite us — the
    step has no authority behind it and must stop. That check is the reason the original
    bug was *visible* rather than silent, and it stays exactly as strict.
    """
    executor = SlowExecutor(seconds=2.0)
    worker = _worker(executor)
    polls = {"n": 0}

    def fake_call(method, path, payload=None):
        if path == "/heartbeat":
            return {"ok": True}
        if path.startswith("/tasks/"):
            polls["n"] += 1
            if polls["n"] >= 2:
                return {"task": {"steering": "running", "claim": None}}
            return {"task": {"steering": "running", "claim": {"participant_id": "par_test"}}}
        raise AssertionError(f"unexpected call {method} {path}")

    monkeypatch.setattr(worker, "call", fake_call)

    assert worker.run_step_watched(_context()) is None
    assert executor.cancelled.is_set(), "an abandoned step must cancel its child"


def test_the_idle_path_still_beats(monkeypatch):
    """`wait` used to own the only heartbeat; extracting it must not have lost one."""
    worker = _worker(SlowExecutor(), poll_seconds=0)
    seen: list[str] = []

    def fake_call(method, path, payload=None):
        seen.append(path)
        if path == "/heartbeat":
            return {"ok": True}
        return {"events": [], "cursor": 0}

    monkeypatch.setattr(worker, "call", fake_call)
    monkeypatch.setattr(cottage_worker.time, "sleep", lambda _: None)

    worker.wait()
    assert "/heartbeat" in seen


def test_a_watcherless_worker_needs_no_heartbeat(monkeypatch):
    """The instant executor: its step ends before any grading window could elapse."""
    worker = _worker(SlowExecutor(seconds=0.0), watch_interval_seconds=0.0)

    def fake_call(method, path, payload=None):
        raise AssertionError(f"no call expected, got {method} {path}")

    monkeypatch.setattr(worker, "call", fake_call)
    assert worker.run_step_watched(_context()) is not None


@pytest.mark.parametrize("steering", ["paused", "stopped"])
def test_steering_still_stops_a_step_mid_flight(monkeypatch, steering):
    """The property gate 6 was built to prove, re-asserted beside the fix."""
    executor = SlowExecutor(seconds=2.0)
    worker = _worker(executor)

    def fake_call(method, path, payload=None):
        if path == "/heartbeat":
            return {"ok": True}
        if path.startswith("/tasks/"):
            return {"task": {"steering": steering, "claim": {"participant_id": "par_test"}}}
        raise AssertionError(f"unexpected call {method} {path}")

    monkeypatch.setattr(worker, "call", fake_call)

    assert worker.run_step_watched(_context()) is None
    assert executor.cancelled.is_set()
