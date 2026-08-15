"""Security properties of the worker's operator-facing CLI."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import cottage_worker  # noqa: E402


def test_main_reads_runtime_token_from_environment_not_argv(monkeypatch):
    """The documented launch path must not put the credential in process argv."""
    captured: dict[str, object] = {}

    class FakeWorker:
        def __init__(self, **kwargs):
            captured.update(kwargs)
            self.stopping = False

        def run(self):
            captured["ran"] = True

    monkeypatch.setenv("COTTAGE_PARTICIPANT_TOKEN", "runtime-secret-from-env")
    monkeypatch.setattr(cottage_worker, "Worker", FakeWorker)
    monkeypatch.setattr(cottage_worker.signal, "signal", lambda *_: None)

    argv = ["--room", "room_test", "--max-cycles", "0"]
    assert "--token" not in argv
    assert cottage_worker.main(argv) == 0
    assert captured["token"] == "runtime-secret-from-env"
    assert captured["ran"] is True
