"""The executor boundary: what a background process may see, and may run.

Two properties matter more than the summaries these produce.

**Bounded context.** An executor gets its own task and its own history. It cannot
reach the room, other participants' work, or anything resembling a chat transcript
— which is what stops an unattended process becoming a channel for context nobody
agreed to share.

**Untrusted input stays data.** The prompt is assembled from room content written
by other participants. It is never instructions to this process and never reaches
a shell.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from executors import EchoExecutor, StepContext, SubprocessExecutor, build  # noqa: E402


def _context(**kwargs) -> StepContext:
    base = {
        "task_id": "tsk_1",
        "title": "Deploy the staging environment",
        "description": "",
        "targets": (),
        "step": 1,
        "total_steps": 3,
    }
    base.update(kwargs)
    return StepContext(**base)


def test_the_deterministic_executor_needs_no_credential():
    """The loop must be exercisable without a key, a network call, or a bill.

    Otherwise a model-backed executor becomes the only way to test claiming,
    renewal and preemption at all — and the coordination guarantees would only
    ever be checked by the least reliable path.
    """
    result = EchoExecutor().run_step(_context())
    assert not result.done
    assert "Step 1 of 3" in result.summary
    assert EchoExecutor().run_step(_context(step=3)).done


def test_the_context_cannot_quietly_grow():
    """A frozen dataclass rather than a dict, so widening it is a decision.

    The failure this prevents is gradual: someone passes "just the room state" for
    convenience, and a background process ends up holding context its human never
    agreed to share with it.
    """
    context = _context()
    with pytest.raises(Exception):
        context.title = "something else"  # type: ignore[misc]
    with pytest.raises(TypeError):
        StepContext(**{**context.__dict__, "chat_transcript": "..."})


def test_a_missing_executor_command_is_a_concern_not_a_crash():
    """An unattended worker that exits on a bad config is attended by whoever
    restarts it."""
    result = SubprocessExecutor("definitely-not-a-real-binary").run_step(_context())
    assert result.done is False
    assert result.concern is not None
    assert "not found" in result.concern


def test_the_prompt_carries_progress_and_instructions_but_stays_bounded():
    executor = SubprocessExecutor("noop-agent")
    prompt = executor.build_prompt(
        _context(
            checkpoints=tuple(f"checkpoint {i}" for i in range(20)),
            instructions=tuple(f"instruction {i}" for i in range(20)),
        )
    )
    assert "checkpoint 19" in prompt
    assert "checkpoint 5" not in prompt, "only the recent tail, not the whole history"
    assert "instruction 19" in prompt
    assert "Do not include your reasoning" in prompt


def test_a_command_that_wants_the_prompt_in_argv_is_refused():
    """Inverted from what it was, and the inversion is the hardening (D-052).

    The command used to *require* a `{prompt}` slot. That put untrusted room content
    in argv and left correctness resting on quoting. The task now goes over stdin, so
    a template with a substitution point is a configuration error rather than
    something to sanitise: there is nowhere for a task title to be substituted into.
    """
    with pytest.raises(ValueError):
        SubprocessExecutor("claude --print {prompt}")
    with pytest.raises(ValueError):
        build("subprocess", command="claude --print {prompt}")
