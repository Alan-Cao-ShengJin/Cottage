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

import os
import shlex
import shutil
import signal
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol


@dataclass(frozen=True)
class StepContext:
    """Everything an executor is allowed to see about the work.

    Deliberately bounded, privacy-filtered, and never a private transcript. An
    executor gets its task plus durable continuity selected by the companion: room
    charter, current work, recent relevant events, checkpoints, blockers, and
    collaborator outputs. It stays a closed dataclass so context cannot widen by
    accident.
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
    #: Bounded durable continuity assembled by the companion, never hidden model
    #: state. These fields survive fresh CLI invocations and process restarts.
    room_charter: str = ""
    current_work: tuple[str, ...] = ()
    recent_events: tuple[str, ...] = ()
    blockers: tuple[str, ...] = ()
    collaborator_outputs: tuple[str, ...] = ()


@dataclass(frozen=True)
class ReactionContext:
    """Bounded room context for cognition not tied to a claimed task."""

    room_charter: str = ""
    current_work: tuple[str, ...] = ()
    recent_events: tuple[str, ...] = ()
    checkpoints: tuple[str, ...] = ()
    blockers: tuple[str, ...] = ()
    collaborator_outputs: tuple[str, ...] = ()


@dataclass(frozen=True)
class ReactionResult:
    """A public reaction, or an explicit decision that no response is useful."""

    summary: str
    message: str | None = None
    concern: str | None = None


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
    #: Something the executor cannot work out for itself. The loop turns this into a
    #: room question; whether it *blocks* is `blocking` below, and the default is no.
    question: str | None = None
    #: Set only when the executor genuinely cannot proceed without an answer. It costs
    #: the lease, so it is opt-in: an executor that blocks on every uncertainty makes
    #: an unattended worker useless, and one that never blocks will guess at things it
    #: should not have guessed at. The judgement belongs to whatever does the work.
    blocking: bool = False
    #: Where this runtime got to, for its own restart. Never room-visible.
    resume: dict[str, object] | None = None


class Executor(Protocol):
    """How a step gets done.

    `cancel` exists so a stop can reach *inside* a step. Without it the loop could
    only refuse to start the next one, which is fine for an executor that returns in
    milliseconds and useless for one that shells out to an agent that runs for
    minutes. It must be safe to call from another thread and safe to call when
    nothing is running.
    """

    name: str

    def run_step(self, context: StepContext) -> StepResult: ...

    def run_reaction(self, context: ReactionContext) -> ReactionResult: ...

    def cancel(self) -> None: ...


class EchoExecutor:
    """Deterministic, credential-free, and useful precisely because it is dull.

    Every property the loop must have — claiming, renewing, checkpointing, obeying a
    stop between steps, releasing on shutdown — is testable against this without a
    key, a network call, or a bill. A model-backed executor then has to satisfy the
    same interface rather than being the only way to exercise the loop at all.
    """

    name = "echo"

    #: A step at which to raise a blocking question, so the ask/answer path can be
    #: exercised deterministically. `None` means never — the default, because a
    #: worker that stops to ask on every run is not a baseline for anything.
    ask_at_step: int | None = None

    def __init__(self, *, ask_at_step: int | None = None) -> None:
        self.ask_at_step = ask_at_step

    def cancel(self) -> None:
        """Nothing to cancel: a step here returns before anyone could ask."""

    def run_reaction(self, context: ReactionContext) -> ReactionResult:
        # Observing conversation must not manufacture a deterministic canned reply.
        return ReactionResult(summary="No room response needed")

    def run_step(self, context: StepContext) -> StepResult:
        done = context.step >= context.total_steps
        heard = (
            f" Following {len(context.instructions)} instruction(s)."
            if context.instructions
            else ""
        )
        # Ask once, and only if nobody has answered yet: an executor that re-asks
        # after being told would park the same work forever, which looks like
        # patience and is a loop.
        if self.ask_at_step is not None and context.step == self.ask_at_step and not heard:
            return StepResult(
                summary=(
                    f"Step {context.step} of {context.total_steps} on '{context.title}'. "
                    f"Stopping here: the next step is not mine to guess at."
                ),
                question=(
                    f"Before step {context.step + 1} of '{context.title}': which target "
                    f"should I use? I will not guess at this one."
                ),
                blocking=True,
                resume={"phase": f"blocked-before-step-{context.step + 1}"},
            )
        return StepResult(
            summary=(f"Step {context.step} of {context.total_steps} on '{context.title}'.{heard}"),
            done=done,
            resume={
                "phase": f"step-{context.step}",
                "next_action": f"step {context.step + 1}",
            },
        )


#: Environment variables a child is allowed to inherit. An allowlist rather than a
#: denylist, because the thing being excluded is *everything nobody thought of*: this
#: process holds a room credential in its environment, and a denylist protects only
#: the names someone remembered to write down.
#:
#: An operator who needs more names them explicitly (`--executor-env`), which turns
#: "the child inherited a secret" into a decision with a diff rather than an accident.
_ENV_ALLOWLIST: tuple[str, ...] = (
    "PATH",
    "PATHEXT",
    "HOME",
    "LANG",
    "LC_ALL",
    "TZ",
    "TERM",
    # Windows needs these to start a process at all.
    "SystemRoot",
    "SystemDrive",
    "ComSpec",
    "USERPROFILE",
    "HOMEDRIVE",
    "HOMEPATH",
    "APPDATA",
    "LOCALAPPDATA",
    "ProgramData",
    "ProgramFiles",
    "ProgramFiles(x86)",
    "NUMBER_OF_PROCESSORS",
    "TEMP",
    "TMP",
    "TMPDIR",
)

#: Shells, refused as the executable. Running one would put the whole hardening
#: exercise back where it started, since a shell's first job is to re-parse text.
_SHELLS: frozenset[str] = frozenset(
    {
        "sh",
        "bash",
        "zsh",
        "dash",
        "fish",
        "csh",
        "tcsh",
        "ksh",
        "cmd",
        "cmd.exe",
        "powershell",
        "powershell.exe",
        "pwsh",
        "pwsh.exe",
        "wsl",
        "wsl.exe",
    }
)

#: Cap on what a child may return. An agent CLI that decides to print a repository
#: would otherwise be a memory problem and, worse, a disclosure one — everything read
#: here is a candidate for a room-visible summary.
MAX_CHILD_OUTPUT_CHARS = 20_000

#: How a subprocess executor says "I cannot work this out". The single convention
#: between the loop and whatever runs the work — taught in the prompt and parsed on
#: the way back, in one file, so the two halves cannot drift apart.
QUESTION_MARKER = "QUESTION"

# Fixed bootstrap, never room content. It registers the new session with the
# worker's external watchdog before the configured executable can do any work.
# Python's `preexec_fn` cannot be used here: `run_step` runs on a thread and the
# subprocess module explicitly warns that pre-exec callbacks can deadlock there.
_POSIX_CONTAINED_EXEC = r"""
import os
import sys

fd = int(sys.argv[1])
command = sys.argv[2:]
os.setsid()
pid = os.getpid()
marker = ""
try:
    raw = open(f"/proc/{pid}/stat", encoding="ascii").read()
    marker = "proc-start:" + raw[raw.rfind(")") + 2:].split()[19]
except (OSError, IndexError):
    pass
os.write(fd, f"R\t{pid}\t{os.getpgrp()}\t{marker}\n".encode("ascii"))
os.close(fd)
os.execve(command[0], command, os.environ)
"""


class SubprocessExecutor:
    """Delegate the thinking to an agent CLI its owner already runs.

    This is bring-your-own-agent applied one layer below where the server holds the
    same line. No API key reaches this process, no vendor SDK is imported, and the
    model is whichever one the human already pays for and has already authorized on
    this machine. Cottage sees none of it.

    Six properties, each of which exists because the alternative is a real failure
    rather than a theoretical one. **The prompt is built from room content written by
    other participants** — untrusted text, by definition (`docs/SECURITY.md`).

    1. **Fixed argv, and the prompt never appears in it.** The command is decided at
       configuration time and the task data goes over **stdin**. There is no
       substitution, so there is nothing for a task title to be substituted into.
    2. **Never a shell**, and a shell is refused as the executable outright.
    3. **A minimal environment, by allowlist.** This process holds a room credential;
       the child gets `PATH` and the handful of names an OS needs to start a process,
       plus whatever the operator named explicitly.
    4. **A working directory it was given**, not wherever the worker happened to run.
    5. **Bounded output and a timeout**, because a slow or chatty step must degrade
       into a concern rather than into a dead worker.
    6. **`cancel()` kills the process tree**, not just the child. An agent CLI that
       spawns its own helpers would otherwise leave them running after a stop — and
       a stop that leaves the work running is not a stop.
    """

    name = "subprocess"

    def __init__(
        self,
        command: str | Sequence[str],
        *,
        timeout_seconds: int = 180,
        cwd: str | None = None,
        env_passthrough: Sequence[str] = (),
        containment_fd: int | None = None,
    ) -> None:
        argv = list(command) if not isinstance(command, str) else shlex.split(command, posix=False)
        if not argv:
            raise ValueError("the executor command is empty")
        if "{prompt}" in " ".join(argv):
            raise ValueError(
                "The command must not interpolate the prompt. Task data goes to the "
                "child over stdin, so there is nothing for room content to be "
                "substituted into — which is the point."
            )
        head = Path(argv[0]).name.lower()
        if head in _SHELLS:
            raise ValueError(
                f"{argv[0]!r} is a shell. Running one would re-parse text this "
                f"executor deliberately never lets a parser see. Name the agent "
                f"binary directly."
            )
        resolved = shutil.which(argv[0])
        self.argv = [resolved or argv[0], *argv[1:]]
        self.timeout_seconds = timeout_seconds
        self.cwd = cwd
        self.env = {
            name: os.environ[name]
            for name in (*_ENV_ALLOWLIST, *env_passthrough)
            if name in os.environ
        }
        self._process: subprocess.Popen[str] | None = None
        self._cancelled = False
        self._containment_fd = containment_fd

    def build_prompt(self, context: StepContext) -> str:
        """Assemble what the agent is asked to do.

        Bounded on purpose. Room continuity is privacy-filtered by Cottage and capped
        by the companion; a prompt cannot grow into an unbounded transcript or carry
        a control surface's private context.
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
        if context.room_charter:
            lines.append("Room charter: " + context.room_charter[:2000])
        for heading, values, cap in (
            ("Current work", context.current_work, 8),
            ("Recent relevant room events", context.recent_events, 16),
            ("Current blockers", context.blockers, 8),
            ("Collaborator outputs", context.collaborator_outputs, 8),
        ):
            if values:
                lines.append(f"{heading}:")
                lines += [f"  - {value}" for value in values[-cap:]]
        lines.append(
            "Do one step of this work now. Reply with a short summary of what you "
            "did and what is next. Do not include your reasoning."
        )
        # The one convention between this loop and whatever runs the work. Told to
        # the agent here and parsed in `run_step`, so the two halves cannot drift:
        # an executor that could not say "I need to ask" would guess instead, and a
        # confident guess is the failure blocking questions exist to prevent (D-051).
        lines.append(
            f"If you cannot proceed without information you have not been given, do "
            f"NOT guess. Reply with {QUESTION_MARKER} as the first word, followed by "
            f"the single question you need answered. Anything else is treated as work "
            f"you completed."
        )
        return "\n".join(lines)

    def run_reaction(self, context: ReactionContext) -> ReactionResult:
        """Use the same hardened subprocess path for a room-level cognition turn."""
        step = self.run_step(
            StepContext(
                task_id="room-reaction",
                title="Respond to relevant room activity",
                description=(
                    "Decide whether a concise room-public response helps coordination. "
                    "Reply with NO_ACTION if it does not; otherwise reply only with "
                    "the message to post."
                ),
                targets=(),
                step=1,
                total_steps=1,
                instructions=(),
                checkpoints=context.checkpoints,
                room_charter=context.room_charter,
                current_work=context.current_work,
                recent_events=context.recent_events,
                blockers=context.blockers,
                collaborator_outputs=context.collaborator_outputs,
            )
        )
        if step.concern:
            return ReactionResult(summary="Room reaction failed", concern=step.concern)
        if step.question or step.summary.strip().upper().startswith("NO_ACTION"):
            return ReactionResult(summary="No room response needed")
        return ReactionResult(summary="Responded to room activity", message=step.summary[:2000])

    def cancel(self) -> None:
        """Terminate the child and everything it started.

        Called when a human stops the work. Killing only the direct child would leave
        an agent CLI's own helpers running, and a stop that leaves the work running
        is not a stop — it is a lie about a stop, which is worse.
        """
        self._cancelled = True
        process = self._process
        if process is None or process.poll() is not None:
            return
        _kill_tree(process)

    def run_step(self, context: StepContext) -> StepResult:
        prompt = self.build_prompt(context)
        self._cancelled = False
        try:
            argv = self.argv
            group_options = _new_process_group()
            if os.name != "nt" and self._containment_fd is not None:
                argv = [
                    sys.executable,
                    "-c",
                    _POSIX_CONTAINED_EXEC,
                    str(self._containment_fd),
                    *self.argv,
                ]
                group_options = {"pass_fds": (self._containment_fd,)}
            process = subprocess.Popen(  # noqa: S603 - fixed argv, never shell=True
                argv,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                # Named rather than inherited from the locale. `text=True` alone
                # decodes with the platform's preferred encoding, which on Windows is
                # a legacy codepage — so an agent that answered with an em dash put
                # `â€”` into a room-visible checkpoint. Found in the first live run
                # with a real model, because every test until then produced ASCII.
                # `replace` rather than `strict`: a worker must not die on a byte it
                # cannot decode, and a visible replacement character is a better
                # outcome than a lost step.
                encoding="utf-8",
                errors="replace",
                cwd=self.cwd,
                env=self.env,
                **group_options,
            )
        except FileNotFoundError:
            return StepResult(
                summary=f"Step {context.step} could not run: {self.argv[0]!r} is not installed.",
                concern=f"executor command not found: {self.argv[0]!r}",
            )
        except OSError as exc:
            return StepResult(
                summary=f"Step {context.step} could not start the executor.",
                concern=f"executor could not start: {exc}"[:400],
            )

        self._process = process
        try:
            stdout, stderr = process.communicate(prompt, timeout=self.timeout_seconds)
        except subprocess.TimeoutExpired:
            # Not fatal to the task. The tree is killed, the loop renews and tries the
            # next step — the difference between a slow step and a dead worker.
            _kill_tree(process)
            process.communicate()
            return StepResult(
                summary=f"Step {context.step} timed out after {self.timeout_seconds}s.",
                concern="executor timed out",
            )
        finally:
            self._notify_process_done(process.pid)
            self._process = None

        if self._cancelled:
            return StepResult(
                summary=f"Step {context.step} was stopped before it finished.",
                concern="executor cancelled",
            )

        output = (stdout or "")[:MAX_CHILD_OUTPUT_CHARS].strip()
        if process.returncode != 0:
            return StepResult(
                summary=f"Step {context.step} failed (exit {process.returncode}).",
                concern=(stderr or "").strip()[:400] or "non-zero exit",
            )
        if output.upper().startswith(QUESTION_MARKER):
            asked = output[len(QUESTION_MARKER) :].lstrip(" :—-\n").strip()
            return StepResult(
                summary=(f"Stopped before step {context.step} rather than guess: {asked[:400]}"),
                question=asked[:2000] or "The executor asked for something it did not name.",
                blocking=True,
                resume={"phase": f"blocked-before-step-{context.step}"},
            )
        return StepResult(
            summary=output[:1500] or f"Step {context.step} produced no output.",
            done=True,
            resume={"phase": f"step-{context.step}"},
        )

    def _notify_process_done(self, pid: int) -> None:
        if os.name == "nt" or self._containment_fd is None:
            return
        marker = _process_start_marker(pid) or ""
        try:
            os.write(self._containment_fd, f"D\t{pid}\t{marker}\n".encode("ascii"))
        except OSError:
            pass


def _new_process_group() -> dict[str, Any]:
    """Start the child in its own group, so the whole tree can be signalled later."""
    if os.name == "nt":
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


def _process_start_marker(pid: int) -> str | None:
    try:
        raw = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
        fields = raw[raw.rfind(")") + 2 :].split()
        return f"proc-start:{fields[19]}"
    except (OSError, IndexError):
        return None


def _kill_tree(process: subprocess.Popen[str]) -> None:
    """Kill a child and its descendants, on either platform, without raising.

    Best effort by nature — a process can always be gone already, or be unkillable
    for reasons outside this process's authority. It never raises, because the caller
    is usually in the middle of obeying a stop and failing to kill something must not
    also stop the worker from *reporting* that it stopped.
    """
    try:
        if os.name == "nt":
            subprocess.run(  # noqa: S603, S607 - fixed argv, no shell
                ["taskkill", "/F", "/T", "/PID", str(process.pid)],
                capture_output=True,
                check=False,
                timeout=15,
            )
        else:
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)
    except (OSError, subprocess.SubprocessError, PermissionError):
        pass
    try:
        process.kill()
    except OSError:
        pass


def build(
    kind: str,
    *,
    command: str | None = None,
    ask_at_step: int | None = None,
    cwd: str | None = None,
    env_passthrough: Sequence[str] = (),
    timeout_seconds: int = 180,
    containment_fd: int | None = None,
) -> Executor:
    if kind == "echo":
        return EchoExecutor(ask_at_step=ask_at_step)
    if kind == "subprocess":
        if not command:
            raise ValueError("--executor subprocess needs --executor-command")
        return SubprocessExecutor(
            command,
            cwd=cwd,
            env_passthrough=env_passthrough,
            timeout_seconds=timeout_seconds,
            containment_fd=containment_fd,
        )
    raise ValueError(f"unknown executor {kind!r}")
