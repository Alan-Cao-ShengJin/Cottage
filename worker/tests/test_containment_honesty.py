"""A worker that cannot contain its executor does not claim work.

The three POSIX blockers that held this at no-go - a watchdog sharing the terminal's
process group, a group kill overtaking a late registration, a deregistration losing
daemonised descendants - were symptoms. The design underneath them was escapable by
construction: a POSIX child may call `setsid()`, leave the process group, and walk out
of any group kill however carefully that kill is written.

Patching the three would have produced something that looked contained and was not,
which is worse than the original bug, because the original bug at least announced
itself when an orphan committed to the repository.

So the escapable path stops being the guarantee. This worker now asks the OS what it can
actually enforce and behaves accordingly:

  * Windows Job Objects with `KILL_ON_JOB_CLOSE` are kernel-enforced and inherited, so
    that path is kept and claiming is allowed.
  * Everywhere else reports `none` - including Linux, until the placement half of the
    cgroup story exists. Detecting a primitive is not placing a process into it, and
    reporting a boundary that nothing puts anything inside is the same lie in a new
    costume.

With `none`, the worker refuses to claim. That is the trade being made deliberately:
unclaimed work is recoverable by anyone, while an executor that escapes its supervisor
and keeps writing to a shared repository is the failure this whole exercise came from.
A lease is a promise that one runtime and no other is doing the work; a worker that
cannot stop its own executor cannot make that promise honestly.
"""

from __future__ import annotations

import os
from typing import Any

import cottage_worker as cw


def test_the_probe_reports_none_where_nothing_is_enforced(monkeypatch):
    """Reporting `strong` is a claim about the kernel, so it needs a reason."""
    monkeypatch.setattr(os, "name", "posix")
    assert cw.detect_containment_strength() == cw.CONTAINMENT_NONE


def test_windows_job_objects_still_count_as_a_real_boundary(monkeypatch):
    """The Windows half was never the broken part and must not be lost in the repair."""
    monkeypatch.setattr(os, "name", "nt")
    assert cw.detect_containment_strength() == cw.CONTAINMENT_STRONG


def test_a_worker_without_containment_takes_no_work():
    """The refusal, which is the entire fix.

    The board is offering this worker a task addressed to it by name - the strongest
    invitation the protocol has - and it still declines, because the question claiming
    asks is not "is this mine?" but "can I stop myself?".
    """
    calls: list[str] = []
    worker = cw.Worker(
        base="http://unused",
        room_id="room_x",
        token="unused",
        label="uncontained",
        containment=cw.CONTAINMENT_NONE,
    )
    # Intercept at the wire, not at a helper: what matters is that no claim request
    # leaves this process, however the loop chooses to phrase it.
    worker.call = lambda method, path, payload=None: calls.append(path) or {}  # type: ignore[assignment,method-assign]

    worker.take_work(
        {
            "proposed_to_you": [{"task_id": "tsk_offered", "title": "Please do this"}],
            "claimable": [{"task_id": "tsk_open", "title": "Anyone", "priority": 99}],
        }
    )
    assert calls == [], "a claim request left an uncontained worker"


def test_the_default_is_refusal_rather_than_assumption():
    """Anything that forgets to set containment must fail closed.

    The original defect was a worker reporting itself contained without checking, so a
    field that defaults to "contained" would rebuild it one constructor away.
    """
    worker = cw.Worker(base="http://unused", room_id="r", token="t", label="l")
    assert worker.containment == cw.CONTAINMENT_NONE


def test_a_contained_worker_still_claims_what_is_offered_to_it(monkeypatch):
    """The refusal has to be about containment and nothing else.

    Without this, a bug that silently stopped all claiming would pass the test above and
    look like the fix working.
    """
    calls: list[tuple[str, Any]] = []
    worker = cw.Worker(
        base="http://unused",
        room_id="room_x",
        token="unused",
        label="contained",
        containment=cw.CONTAINMENT_STRONG,
    )

    def fake_call(method: str, path: str, payload: Any = None) -> dict[str, Any]:
        calls.append((path, payload))
        return {
            "task": {
                "id": "tsk_offered",
                "claim": {"fence": 1, "expires_at": "2099-01-01T00:00:00Z"},
            }
        }

    monkeypatch.setattr(worker, "call", fake_call)
    worker.take_work({"proposed_to_you": [{"task_id": "tsk_offered", "title": "Please"}]})
    # First call, not only call: a successful claim is followed by declaring the work,
    # which is the loop behaving correctly and not something this test should pin.
    assert calls[0][0] == "/tasks/claim"
    assert calls[0][1]["task_id"] == "tsk_offered"
