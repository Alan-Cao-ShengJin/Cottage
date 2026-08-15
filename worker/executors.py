"""What "doing the work" means, kept separate from the coordination loop.

The loop in `cottage_worker.py` knows about leases, directives, checkpoints and
polling. It knows nothing about *how* a step is performed, and that separation is
the point: swapping in a model-backed executor must not be able to break lease
renewal, and a bug in lease renewal must not be reachable from a prompt.

**Provider-neutral by construction.** Nothing here imports a vendor SDK. The two
implementations are a deterministic one for tests and a subprocess one that runs
whatever agent binary its owner already pays for — which is the same principle the
server holds to: bring your own agent, we never call a model for you.

The subprocess executor is the honest answer to "make the worker actually think".
It does not need an API key, a vendor account, or any credential reaching this
process, because it delegates to a CLI its human has already authorized. A hosted
API implementation is a third case, not a privileged one.
"""

from __future__ import annotations

import shlex
import subprocess
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class StepContext:
    """Everything an executor is allowed to see about the work.

    Deliberately small, and deliberately *not* a conversation. An executor gets the
    task it was assigned, where it has got to, and what it was told — never a chat
    transcript, never other participants' work, never room history. Widening this
    is how private context leaks into a background process, so it stays a closed
    dataclass rather than a dict someone can quietly add a field to.
    """

    task_id: str
    title: str
    description: str
    targets: tuple[str, ...]
    step: int
    total_steps: int
    #: Instructions a human sent for this task, oldest first. Bounded and normalized
    #: by the room, never raw chat.
    instructions: tuple[str, ...] = ()
    #: What previous steps recorded, so a restarted process can pick up.
    checkpoints: tuple[str, ...] = ()


@dataclass(frozen=True)
class StepResult:
    """One step's outcome.

    `summary` is room-visible and must read as an outcome — what was done, what it
    means, what is next. Never reasoning: a checkpoint is a progress record, not a
    scratchpad, and the room is the wrong place for either a model's working or its
    private context.
    """

    summary: str
    #: True when the executor believes the task is finished. The loop still decides,
    #: because finishing is a coordination act and this is not the coordination layer.
    done: bool = False
    #: Non-fatal trouble worth surfacing to a human without stopping the work.
    concern: str | None = None


class Executor(Protocol):
    """How a step gets done. Two methods so a restart can be honest about itself."""

    name: str

    def run_step(self, context: StepContext) -> StepResult: ...


class EchoExecutor:
    """Deterministic, credential-free, and useful precisely because it is dull.

    Every property the loop must have — claiming, renewing, checkpointing, obeying a
    stop between steps, releasing on shutdown — is testable against this without a
    key, a network call, or a bill. A model-backed executor then has to satisfy the
    same interface rather than being the only way to exercise the loop at all.
    """

    name = "echo"

    def run_step(self, context: StepContext) -> StepResult:
        done = context.step >= context.total_steps
        heard = (
            f" Following {len(context.instructions)} instruction(s)."
            if context.instructions
            else ""
        )
        return StepResult(
            summary=(
                f"Step {context.step} of {context.total_steps} on '{context.title}'.{heard}"
            ),
            done=done,
        )


class SubprocessExecutor:
    """Delegate the thinking to an agent CLI its owner already runs.

    This is the bring-your-own-agent principle applied one layer down. No API key
    reaches this process, no vendor SDK is imported, and the model is whichever one
    the human already pays for and has already authorized on this machine. Cottage
    never sees any of it.

    The command is a template with `{prompt}` substituted, split with `shlex` and
    executed **without a shell**, so a task title containing a semicolon is an
    awkward string rather than a command. That matters more than usual here: the
    prompt is assembled from room content, which is untrusted data written by other
    participants — never instructions to this process, and certainly never to a
    shell (`docs/SECURITY.md`).
    """

    name = "subprocess"

    def __init__(self, command_template: str, *, timeout_seconds: int = 180) -> None:
        if "{prompt}" not in command_template:
            raise ValueError(
                "The command template must contain {prompt}, or the executor would "
                "run the same command regardless of the work."
            )
        self.command_template = command_template
        self.timeout_seconds = timeout_seconds

    def build_prompt(self, context: StepContext) -> str:
        """Assemble what the agent is asked to do.

        Bounded on purpose. An executor sees its own task and its own history, so a
        prompt cannot grow to include the room — which is what stops a background
        process becoming a channel for context nobody agreed to share.
        """
        lines = [
            f"Task: {context.title}",
            f"Step {context.step} of {context.total_steps}.",
        ]
        if context.description:
            lines.append(f"Description: {context.description}")
        if context.targets:
            lines.append("Targets: " + ", ".join(context.targets))
        if context.checkpoints:
            lines.append("Progress so far:")
            lines += [f"  - {c}" for c in context.checkpoints[-5:]]
        if context.instructions:
            lines.append("Instructions from a human:")
            lines += [f"  - {i}" for i in context.instructions[-5:]]
        lines.append(
            "Do one step of this work now. Reply with a short summary of what you "
            "did and what is next. Do not include your reasoning."
        )
        return "\n".join(lines)

    def run_step(self, context: StepContext) -> StepResult:
        prompt = self.build_prompt(context)
        argv = [
            part.replace("{prompt}", prompt)
            for part in shlex.split(self.command_template, posix=False)
        ]
        try:
            completed = subprocess.run(  # noqa: S603 - argv list, never shell=True
                argv,
                capture_output=True,
                text=True,
                timeout=self.timeout_seconds,
                check=False,
            )
        except FileNotFoundError:
            return StepResult(
                summary=f"Step {context.step} could not run: {argv[0]!r} is not installed.",
                concern=f"executor command not found: {argv[0]!r}",
            )
        except subprocess.TimeoutExpired:
            # Not fatal to the task. The loop renews and tries the next step, which
            # is the difference between a slow step and a dead worker.
            return StepResult(
                summary=f"Step {context.step} timed out after {self.timeout_seconds}s.",
                concern="executor timed out",
            )

        output = (completed.stdout or "").strip()
        if completed.returncode != 0:
            return StepResult(
                summary=f"Step {context.step} failed (exit {completed.returncode}).",
                concern=(completed.stderr or "").strip()[:400] or "non-zero exit",
            )
        return StepResult(
            summary=output[:1500] or f"Step {context.step} produced no output.",
            done=context.step >= context.total_steps,
        )


def build(kind: str, *, command: str | None = None) -> Executor:
    if kind == "echo":
        return EchoExecutor()
    if kind == "subprocess":
        if not command:
            raise ValueError("--executor subprocess needs --executor-command")
        return SubprocessExecutor(command)
    raise ValueError(f"unknown executor {kind!r}")
