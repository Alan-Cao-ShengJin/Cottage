"""Security properties of the worker's operator-facing CLI.

A credential on a command line is readable, for the whole life of the process, by
anything that can list processes. That is not theoretical here: the tokens of two
stranded workers were recovered exactly that way while testing on 2026-08-15, and a
running companion was stopped by another participant for the same exposure.

Documentation cannot enforce this. `docs/COMPANION.md` said "never lands in a command
line" on one line while its own example did it on another, so the option is refused in
code and the doc is now describing a rule rather than requesting one.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import cottage_worker  # noqa: E402


class FakeWorker:
    """Stands in for the real loop; records what main() decided to hand it."""

    captured: dict[str, object] = {}

    def __init__(self, **kwargs):
        FakeWorker.captured = dict(kwargs)
        self.stopping = False

    def run(self):
        FakeWorker.captured["ran"] = True


@pytest.fixture
def launched(monkeypatch):
    FakeWorker.captured = {}  # class-level, so a previous test's launch would linger
    monkeypatch.setattr(cottage_worker, "Worker", FakeWorker)
    monkeypatch.setattr(cottage_worker.signal, "signal", lambda *_: None)
    return FakeWorker


def test_the_runtime_credential_is_read_from_the_environment(launched, monkeypatch):
    monkeypatch.setenv("COTTAGE_PARTICIPANT_TOKEN", "runtime-secret-from-env")

    assert cottage_worker.main(["--room", "room_test"]) == 0
    assert launched.captured["token"] == "runtime-secret-from-env"
    assert launched.captured["ran"] is True


def test_passing_the_token_on_the_command_line_is_refused(launched, monkeypatch):
    """The failure mode this exists for: an operator following an older recipe.

    Refused rather than quietly ignored, because a worker that started anyway would
    have taken the credential from the environment and left the operator believing the
    argv form is supported — the exposure would then recur on the next machine.
    """
    monkeypatch.setenv("COTTAGE_PARTICIPANT_TOKEN", "runtime-secret-from-env")

    with pytest.raises(SystemExit) as exit_info:
        cottage_worker.main(["--room", "room_test", "--token", "secret-in-argv"])

    assert exit_info.value.code == 2
    assert "ran" not in launched.captured


def test_the_refusal_says_what_to_do_instead(launched, monkeypatch, capsys):
    monkeypatch.setenv("COTTAGE_PARTICIPANT_TOKEN", "runtime-secret-from-env")

    with pytest.raises(SystemExit):
        cottage_worker.main(["--room", "room_test", "--token", "secret-in-argv"])

    message = capsys.readouterr().err
    assert "COTTAGE_PARTICIPANT_TOKEN" in message
    assert "process listings" in message
    assert "secret-in-argv" not in message, "the refusal must not echo the credential"


def test_a_room_key_on_the_command_line_is_refused_too(launched, monkeypatch):
    """An invitation is a credential: it is enough to obtain a seat and a token."""
    with pytest.raises(SystemExit) as exit_info:
        cottage_worker.main(["--invitation", "key-in-argv"])

    assert exit_info.value.code == 2


def test_a_bare_flag_with_no_value_is_refused_as_well(launched, monkeypatch):
    """`--token` alone would otherwise fall through to the environment and start.

    `nargs="?"` makes the value optional, so the option must refuse on being present
    rather than on having a value — otherwise the one form that looks harmless is the
    one that works, and the recipe drifts back.
    """
    monkeypatch.setenv("COTTAGE_PARTICIPANT_TOKEN", "runtime-secret-from-env")

    with pytest.raises(SystemExit):
        cottage_worker.main(["--room", "room_test", "--token"])


def test_the_credential_never_appears_in_the_worker_s_own_argv(launched, monkeypatch):
    """The property the operator actually cares about, stated end to end."""
    monkeypatch.setenv("COTTAGE_PARTICIPANT_TOKEN", "runtime-secret-from-env")
    argv = ["--room", "room_test", "--label", "companion"]

    assert cottage_worker.main(argv) == 0
    assert "runtime-secret-from-env" not in " ".join(argv)
    assert launched.captured["token"] == "runtime-secret-from-env"
