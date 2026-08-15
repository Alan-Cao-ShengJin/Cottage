"""What the subprocess executor refuses, and what it never hands a child (D-052).

This is the executor that makes a worker *think*, by delegating to an agent CLI its
owner already runs. It is also the one place in this project where untrusted room
content meets process execution, so each property below is written as a refusal
rather than a convention: the prompt cannot reach argv, a shell cannot be the
executable, and the child cannot inherit the room credential this process holds.

Requested by the ChatGPT participant before the first intelligent unattended run,
and the ordering was right — a hardening list applied after a live run is a list of
things that already happened.
"""

from __future__ import annotations

import os
import sys
import textwrap
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from executors import (  # noqa: E402
    MAX_CHILD_OUTPUT_CHARS,
    StepContext,
    SubprocessExecutor,
    build,
)


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


def _python(script: str) -> list[str]:
    """Run a snippet with this interpreter, as a fixed argv the executor accepts."""
    return [sys.executable, "-c", textwrap.dedent(script)]


def test_a_command_that_interpolates_the_prompt_is_refused():
    """The strongest form of "untrusted text never reaches argv".

    Not "escape it carefully" but "there is nowhere to put it": the task goes over
    stdin, so a template with a substitution point is a configuration error rather
    than a thing to sanitise.
    """
    with pytest.raises(ValueError) as exc:
        SubprocessExecutor("some-agent --ask {prompt}")
    assert "stdin" in str(exc.value)


def test_a_shell_cannot_be_the_executable():
    """Running one would re-parse text this executor never lets a parser see."""
    for shell in ("bash", "/bin/sh", "cmd.exe", "powershell"):
        with pytest.raises(ValueError) as exc:
            SubprocessExecutor([shell, "-c", "agent"])
        assert "shell" in str(exc.value).lower()


def test_the_prompt_goes_over_stdin_and_never_appears_in_the_arguments():
    """The property the whole design rests on, checked against a real child.

    The child prints its own argv and what it read from stdin, so this asserts the
    separation actually held in the process rather than in the code that built it.
    """
    executor = SubprocessExecutor(
        _python(
            """
            import sys
            data = sys.stdin.read()
            print("ARGV:", " ".join(sys.argv[1:]))
            print("STDIN-LEN:", len(data))
            print("SAW-TITLE:", "Deploy the staging environment" in data)
            """
        )
    )
    result = executor.run_step(_context())
    assert "SAW-TITLE: True" in result.summary, "the child did receive the task"
    assert "Deploy" not in result.summary.split("ARGV:")[1].split("\n")[0]


def test_a_malicious_task_title_never_becomes_a_command():
    """Room content is written by other participants and is untrusted.

    A title full of shell syntax has to end up as characters in a string the child
    read, and nowhere else. There is no shell in this path at all, which is what
    makes the guarantee structural rather than a matter of quoting.
    """
    nasty = 'deploy"; rm -rf /; echo "'
    executor = SubprocessExecutor(
        _python(
            """
            import sys
            data = sys.stdin.read()
            print("LEN:", len(data))
            """
        )
    )
    prompt = executor.build_prompt(_context(title=nasty))
    assert nasty in prompt, "it is carried faithfully, as data"

    result = executor.run_step(_context(title=nasty))
    assert result.concern is None
    assert "LEN:" in result.summary


def test_the_child_does_not_inherit_this_process_s_environment(monkeypatch):
    """An allowlist, because a denylist protects only the names someone remembered.

    This process holds a room credential in its environment. The child is an agent
    CLI its owner authorized to think, not one this project has any business handing
    a token to.
    """
    monkeypatch.setenv("COTTAGE_PARTICIPANT_TOKEN", "a-real-looking-secret")
    monkeypatch.setenv("SOME_OTHER_API_KEY", "another-secret")

    executor = SubprocessExecutor(
        _python(
            """
            import os, sys
            sys.stdin.read()
            print("TOKEN:", os.environ.get("COTTAGE_PARTICIPANT_TOKEN", "<absent>"))
            print("OTHER:", os.environ.get("SOME_OTHER_API_KEY", "<absent>"))
            print("PATH:", "yes" if os.environ.get("PATH") else "no")
            """
        )
    )
    result = executor.run_step(_context())
    assert "TOKEN: <absent>" in result.summary
    assert "OTHER: <absent>" in result.summary
    assert "PATH: yes" in result.summary, "but it can still find its own tools"


def test_an_operator_can_name_a_variable_explicitly(monkeypatch):
    """Because the alternative is an agent CLI that cannot find its own config.

    Naming it turns "the child inherited a secret" from an accident into a decision
    with a diff, which is the only version of this that stays true over time.
    """
    monkeypatch.setenv("MY_AGENT_HOME", "/opt/agent")
    executor = SubprocessExecutor(
        _python(
            """
            import os, sys
            sys.stdin.read()
            print("HOME:", os.environ.get("MY_AGENT_HOME", "<absent>"))
            """
        ),
        env_passthrough=["MY_AGENT_HOME"],
    )
    assert "HOME: /opt/agent" in executor.run_step(_context()).summary


def test_the_child_runs_where_it_was_told_to(tmp_path):
    """Not wherever the worker happened to be started.

    An unattended process's cwd is an accident of how somebody launched it, and an
    agent CLI that writes files should not have that decide where they land.
    """
    executor = SubprocessExecutor(
        _python(
            """
            import os, sys
            sys.stdin.read()
            print("CWD:", os.getcwd())
            """
        ),
        cwd=str(tmp_path),
    )
    result = executor.run_step(_context())
    assert str(tmp_path.resolve()).lower() in result.summary.lower()


def test_a_chatty_child_is_truncated_rather_than_swallowing_memory():
    """Output is a disclosure surface as much as a resource one.

    Everything read here is a candidate for a room-visible summary, so an agent that
    decides to print a repository must be cut off rather than relayed.
    """
    executor = SubprocessExecutor(
        _python(
            f"""
            import sys
            sys.stdin.read()
            sys.stdout.write("x" * {MAX_CHILD_OUTPUT_CHARS * 3})
            """
        )
    )
    result = executor.run_step(_context())
    assert len(result.summary) <= 1500


def test_a_step_that_hangs_is_a_concern_not_a_dead_worker():
    """A slow step must degrade into information, not into a stuck process."""
    executor = SubprocessExecutor(
        _python(
            """
            import sys, time
            sys.stdin.read()
            time.sleep(60)
            """
        ),
        timeout_seconds=2,
    )
    result = executor.run_step(_context())
    assert result.done is False
    assert result.concern == "executor timed out"


def test_cancel_kills_the_child_and_its_descendants(tmp_path):
    """A stop that leaves the work running is a lie about a stop.

    The child spawns a grandchild that would outlive a naive `terminate()`, and the
    grandchild's own file is the evidence: if the tree survived, it keeps writing.
    """
    import threading
    import time

    marker = tmp_path / "grandchild.txt"
    # Written to files rather than nested inside a string literal: the escaping needed
    # to embed a grandchild in a child in a test is exactly the kind of thing that
    # silently produces a no-op test that passes.
    grandchild = tmp_path / "grandchild.py"
    grandchild.write_text(
        "import time\n"
        f"path = {str(marker)!r}\n"
        "for _ in range(400):\n"
        "    open(path, 'a').write('tick\\n')\n"
        "    time.sleep(0.05)\n",
        encoding="utf-8",
    )
    child = tmp_path / "child.py"
    child.write_text(
        "import subprocess, sys\n"
        "sys.stdin.read()\n"
        f"proc = subprocess.Popen([sys.executable, {str(grandchild)!r}])\n"
        "print('SPAWNED', flush=True)\n"
        "proc.wait()\n",
        encoding="utf-8",
    )
    executor = SubprocessExecutor([sys.executable, str(child)], timeout_seconds=60)

    result: dict[str, object] = {}

    def run() -> None:
        result["r"] = executor.run_step(_context())

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    deadline = time.time() + 15
    while time.time() < deadline and not marker.exists():
        time.sleep(0.05)
    assert marker.exists(), "the grandchild never started, so this proves nothing"

    executor.cancel()
    thread.join(20)
    settled = marker.read_text().count("tick")
    time.sleep(1.0)
    assert marker.read_text().count("tick") == settled, "a descendant outlived the stop"


def test_a_non_ascii_answer_survives_the_pipe():
    """Found in the first live run with a real model, not in this suite (D-052).

    `text=True` alone decodes with the platform's preferred encoding, which on
    Windows is a legacy codepage — so an agent that answered with an em dash wrote
    `â€”` into a room-visible checkpoint. Every test until then produced ASCII, which
    is exactly the kind of blind spot a deterministic executor creates: it is honest
    about the loop and says nothing about what real output looks like.
    """
    executor = SubprocessExecutor(
        _python(
            """
            import sys
            sys.stdin.read()
            sys.stdout.reconfigure(encoding="utf-8")
            print("a stop is trustworthy \\u2014 durably \\u2014 in \\u65e5\\u672c\\u8a9e too")
            """
        )
    )
    result = executor.run_step(_context())
    assert "—" in result.summary, "an em dash is an em dash"
    assert "日本語" in result.summary
    assert "â€”" not in result.summary, "and never its cp1252 misreading"


def test_an_executor_that_cannot_proceed_asks_instead_of_guessing():
    """The convention that lets a subprocess executor decline to invent an answer.

    Without a way to say "I need to ask", an agent asked for something it was never
    told will produce a plausible value — and a confident guess presented as work is
    exactly the failure blocking questions exist to prevent (D-051). The marker is
    taught in the prompt and parsed here, in one file, so the two halves cannot drift.
    """
    executor = SubprocessExecutor(
        _python(
            """
            import sys
            sys.stdin.read()
            print("QUESTION: which environment did this ship to?")
            """
        )
    )
    result = executor.run_step(_context())
    assert result.blocking is True
    assert result.question == "which environment did this ship to?"
    assert result.done is False
    assert "rather than guess" in result.summary


def test_ordinary_output_is_never_mistaken_for_a_question():
    """The marker has to be the first word, or a summary that merely mentions a
    question would park the task it was reporting progress on."""
    executor = SubprocessExecutor(
        _python(
            """
            import sys
            sys.stdin.read()
            print("Drafted the note. One open QUESTION remains for the reviewer.")
            """
        )
    )
    result = executor.run_step(_context())
    assert result.blocking is False
    assert result.question is None


def test_the_prompt_tells_the_agent_how_to_ask():
    """A convention the executor parses but never explains is one no agent follows."""
    prompt = SubprocessExecutor("noop-agent").build_prompt(_context())
    assert "QUESTION" in prompt
    assert "do NOT guess" in prompt or "not guess" in prompt


def test_cancel_is_safe_when_nothing_is_running():
    """It is called from the loop's own polling path, which does not synchronise
    with the step it is watching."""
    build("subprocess", command=_python("pass")).cancel()
    build("echo").cancel()


def test_the_resolved_command_is_an_absolute_path_where_one_exists():
    """Resolved once, at configuration time.

    A `PATH` change under a long-lived worker would otherwise silently swap which
    binary its owner authorised — the sort of thing nobody notices until it matters.
    """
    executor = SubprocessExecutor([sys.executable, "-c", "pass"])
    assert os.path.isabs(executor.argv[0])
