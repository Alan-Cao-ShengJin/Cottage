"""The relay's supervisor: a lifetime that is not a session's (D-091).

The `>` chat path depends on one thing being true — something is listening on 127.0.0.1:8787.
The relay itself was already correct; what was wrong was *who owned it*. It was started as a
child of a Claude Code session, so a restart took it down, and `cottage_chat_hook.py` correctly
stands down when the port refuses. A dead relay is therefore indistinguishable from a slow one,
which is the same shape as the reconnect bug that started all of this: silence that reads as
nothing-happened.

These tests hold the three properties that make the supervisor worth having over `Start-Process`:

* **The port is the truth, not the pid.** A process can be alive with its relay thread dead.
* **It refuses to start without a credential, and says which options exist.** Failing here is
  cheap; failing after somebody has typed `>lunch?` is not.
* **The token is never an argument.** Only the path is, because a command line is readable from
  any process listing for the life of the process (D-058).
"""

from __future__ import annotations

import importlib.util
import json
import socket
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]


def _load_service():
    name = "_relay_service_under_test"
    if name in sys.modules:
        return sys.modules[name]
    path = REPO / "scripts" / "cottage_relay_service.py"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        del sys.modules[name]
        raise
    return module


service = _load_service()


@pytest.fixture
def state_dir(tmp_path):
    return tmp_path / "state"


def _args(**over):
    base = {
        "room": "room_01TEST",
        "base": "https://app.cottageai.dev",
        "port": 1,
        "state_dir": None,
        "token_file": None,
        "save_token": False,
    }
    base.update(over)
    return type("Args", (), base)()


class _Spawned:
    """Records what `start` would exec instead of launching it.

    It intercepts the wake-channel launch **only**, and delegates everything else to the real
    `Popen`. Patching `subprocess.Popen` wholesale also captured the `icacls` call inside
    `_restrict_to_owner` -- so the permission test would have passed by never narrowing anything.
    A stub broad enough to swallow the security-relevant call is a stub that proves nothing.
    """

    def __init__(self):
        self.command: list[str] = []
        self.env: dict[str, str] = {}
        self.pid = 4242
        self._real = subprocess.Popen

    def __call__(self, command, **kwargs):
        if not any(str(part).endswith("wake_channel.py") for part in command):
            return self._real(command, **kwargs)
        self.command = list(command)
        self.env = dict(kwargs.get("env") or {})
        return self


# ---------------------------------------------------------------------------
# Refusing to start
# ---------------------------------------------------------------------------


def test_no_credential_at_all_refuses_and_names_both_ways_to_supply_one(
    state_dir, monkeypatch, capsys
):
    """The failure somebody meets is a refusal at `start`, not a silent stand-down at `>`."""
    monkeypatch.delenv("AGENT_ROOMS_TOKEN", raising=False)
    code = service.start(_args(state_dir=state_dir))
    out = capsys.readouterr().out
    assert code == 2
    assert "--token-file" in out
    assert "AGENT_ROOMS_TOKEN" in out
    assert not state_dir.exists(), "a refused start must leave no pidfile to mislead status"


def test_a_token_file_that_is_not_a_file_says_so_rather_than_starting(
    state_dir, monkeypatch, capsys
):
    """The likeliest real mistake: a path typo. Starting anyway would leave the channel to fail
    on its own, in a log nobody is watching yet."""
    monkeypatch.delenv("AGENT_ROOMS_TOKEN", raising=False)
    missing = state_dir / "nope.txt"
    code = service.start(_args(state_dir=state_dir, token_file=str(missing)))
    out = capsys.readouterr().out
    assert code == 2
    assert "nope.txt" in out


def test_a_room_is_required_because_a_relay_posts_somewhere(state_dir, monkeypatch, capsys):
    monkeypatch.setenv("AGENT_ROOMS_TOKEN", "tok_test")
    code = service.start(_args(room=None, state_dir=state_dir))
    assert code == 2
    assert "--room" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# What reaches the child, and what must not
# ---------------------------------------------------------------------------


def test_the_token_never_appears_on_the_childs_command_line(state_dir, monkeypatch):
    """D-058, restated for this process tree. The token travels by inherited environment or by
    file; the command line carries neither."""
    secret = "tok_do_not_leak_0123456789"
    monkeypatch.setenv("AGENT_ROOMS_TOKEN", secret)
    spawned = _Spawned()
    monkeypatch.setattr(service.subprocess, "Popen", spawned)

    assert service.start(_args(state_dir=state_dir)) == 0
    assert secret not in " ".join(spawned.command)
    assert spawned.env.get("AGENT_ROOMS_TOKEN") == secret


def test_a_token_file_is_passed_by_path_so_a_rotated_token_needs_no_edit(state_dir, monkeypatch):
    monkeypatch.delenv("AGENT_ROOMS_TOKEN", raising=False)
    state_dir.mkdir(parents=True)
    token_path = state_dir / "token.txt"
    token_path.write_text("tok_from_a_file", encoding="ascii")
    spawned = _Spawned()
    monkeypatch.setattr(service.subprocess, "Popen", spawned)

    assert service.start(_args(state_dir=state_dir, token_file=str(token_path))) == 0
    command = " ".join(spawned.command)
    assert "--token-file" in command
    assert str(token_path) in command
    assert "tok_from_a_file" not in command, "the path is not a secret; the contents are"


def test_the_child_is_the_wake_channel_with_the_relay_on_the_asked_for_port(state_dir, monkeypatch):
    monkeypatch.setenv("AGENT_ROOMS_TOKEN", "tok_test")
    spawned = _Spawned()
    monkeypatch.setattr(service.subprocess, "Popen", spawned)

    assert service.start(_args(state_dir=state_dir, port=9999)) == 0
    command = spawned.command
    assert command[0] == sys.executable, "the venv interpreter, not whatever `python` resolves to"
    assert command[1].endswith("wake_channel.py")
    assert "--relay-port" in command and "9999" in command
    # Also in the environment, because the hook reads the same variable to find the port.
    assert spawned.env.get("COTTAGE_RELAY_PORT") == "9999"


def test_the_pidfile_records_what_status_and_stop_will_need(state_dir, monkeypatch):
    monkeypatch.setenv("AGENT_ROOMS_TOKEN", "tok_test")
    monkeypatch.setattr(service.subprocess, "Popen", _Spawned())

    assert service.start(_args(state_dir=state_dir, port=9999)) == 0
    written = json.loads((state_dir / "relay-service.json").read_text(encoding="utf-8"))
    assert written["pid"] == 4242
    assert written["room"] == "room_01TEST"
    assert written["port"] == 9999
    assert "tok_test" not in json.dumps(written), "no credential at rest that nobody asked for"


def test_the_log_is_appended_so_the_reason_a_relay_died_survives_the_restart(
    state_dir, monkeypatch
):
    """The evidence is worth more than a tidy file, and a truncating restart would erase it at
    exactly the moment somebody went looking."""
    monkeypatch.setenv("AGENT_ROOMS_TOKEN", "tok_test")
    monkeypatch.setattr(service.subprocess, "Popen", _Spawned())
    state_dir.mkdir(parents=True)
    log = state_dir / "relay-service.log"
    log.write_text("an earlier crash\n", encoding="utf-8")

    assert service.start(_args(state_dir=state_dir)) == 0
    assert "an earlier crash" in log.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Status: the port is the truth
# ---------------------------------------------------------------------------


def test_status_exits_non_zero_when_chat_would_fall_back_to_the_slow_path(state_dir, capsys):
    """Usable in a check, which is the point: "is `>` fast right now" gets a shell answer."""
    code = service.status(_args(state_dir=state_dir))
    assert code == 1
    assert "not listening" in capsys.readouterr().out


def test_status_reports_a_listening_port_it_did_not_start_itself(state_dir, capsys):
    """A relay started by hand still serves `>`. Reporting "down" because there is no pidfile
    would be the tool believing its own bookkeeping over the socket."""
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]
    try:
        code = service.status(_args(state_dir=state_dir, port=port))
    finally:
        listener.close()
    out = capsys.readouterr().out
    assert code == 0
    assert "LISTENING" in out
    assert "no pidfile" in out, "it should still admit it is not supervising this one"


def test_an_alive_process_with_a_dead_relay_thread_is_called_out(state_dir, monkeypatch, capsys):
    """The failure that a pid check alone would pass. What `>` needs is the port, and saying
    "running" here would send somebody to debug the wrong half."""
    state_dir.mkdir(parents=True)
    (state_dir / "relay-service.json").write_text(
        json.dumps({"pid": 4242, "room": "room_01TEST", "port": 1, "log": "x"}), encoding="utf-8"
    )
    monkeypatch.setattr(service, "_alive", lambda pid: True)

    code = service.status(_args(state_dir=state_dir))
    out = capsys.readouterr().out
    assert code == 1, "alive-but-unreachable is a failure, because chat is what is being asked"
    assert "port is not answering" in out


def test_status_survives_a_corrupt_pidfile(state_dir, capsys):
    """A half-written pidfile is exactly what a kill during start leaves behind, and status is
    the thing somebody runs *after* that."""
    state_dir.mkdir(parents=True)
    (state_dir / "relay-service.json").write_text('{"pid": ', encoding="utf-8")
    assert service.status(_args(state_dir=state_dir)) == 1
    assert "no pidfile" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# Stop
# ---------------------------------------------------------------------------


def test_stopping_a_process_that_is_already_gone_clears_the_pidfile(state_dir, monkeypatch, capsys):
    state_dir.mkdir(parents=True)
    pidfile = state_dir / "relay-service.json"
    pidfile.write_text(json.dumps({"pid": 4242, "port": 1}), encoding="utf-8")
    monkeypatch.setattr(service, "_alive", lambda pid: False)

    assert service.stop(_args(state_dir=state_dir)) == 0
    assert not pidfile.exists(), "a stale pidfile makes the next status lie"
    assert "already gone" in capsys.readouterr().out


def test_stop_with_nothing_started_is_not_an_error(state_dir):
    assert service.stop(_args(state_dir=state_dir)) == 0


def test_starting_twice_does_not_leave_two_relays_fighting_for_the_port(state_dir, capsys):
    """Second start finds the port taken and stands down. The alternative is a child that dies
    on bind while the pidfile claims it is the live one."""
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]
    try:
        code = service.start(_args(state_dir=state_dir, port=port))
    finally:
        listener.close()
    assert code == 0
    assert "already listening" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# Provisioning a durable credential
# ---------------------------------------------------------------------------


def test_a_token_is_never_written_to_disk_without_being_asked(state_dir, monkeypatch):
    """The default must not quietly leave a participant credential on the filesystem. Somebody
    running `start` for one evening has not consented to that."""
    monkeypatch.setenv("AGENT_ROOMS_TOKEN", "tok_test")
    monkeypatch.setattr(service.subprocess, "Popen", _Spawned())
    token_path = state_dir / "token.txt"

    assert service.start(_args(state_dir=state_dir, token_file=str(token_path))) == 0
    assert not token_path.exists()


def test_save_token_makes_the_credential_outlive_the_session_that_had_it(state_dir, monkeypatch):
    """The whole provisioning problem in one test: the token exists only inside a session, and
    the service exists to survive one."""
    monkeypatch.setenv("AGENT_ROOMS_TOKEN", "tok_test")
    spawned = _Spawned()
    monkeypatch.setattr(service.subprocess, "Popen", spawned)
    token_path = state_dir / "token.txt"

    assert (
        service.start(_args(state_dir=state_dir, token_file=str(token_path), save_token=True)) == 0
    )
    assert token_path.read_text(encoding="ascii") == "tok_test"
    # And the child is pointed at the file, so the next start needs no environment at all.
    assert "--token-file" in spawned.command


def test_save_token_does_not_overwrite_a_token_already_on_disk(state_dir, monkeypatch):
    """The file is the durable copy; the environment is whatever this shell happens to hold.
    Clobbering the first with the second would break the running arrangement to match a
    transient one."""
    monkeypatch.setenv("AGENT_ROOMS_TOKEN", "tok_from_this_shell")
    monkeypatch.setattr(service.subprocess, "Popen", _Spawned())
    state_dir.mkdir(parents=True)
    token_path = state_dir / "token.txt"
    token_path.write_text("tok_already_here", encoding="ascii")

    service.start(_args(state_dir=state_dir, token_file=str(token_path), save_token=True))
    assert token_path.read_text(encoding="ascii") == "tok_already_here"


def test_save_token_without_a_token_to_save_still_refuses_clearly(state_dir, monkeypatch, capsys):
    monkeypatch.delenv("AGENT_ROOMS_TOKEN", raising=False)
    code = service.start(
        _args(state_dir=state_dir, token_file=str(state_dir / "t.txt"), save_token=True)
    )
    assert code == 2
    assert "No participant credential" in capsys.readouterr().out


def test_a_saved_token_is_not_readable_by_everyone(state_dir, monkeypatch):
    """Checked as far as the platform allows. On POSIX the mode is the control; on Windows the
    ACL is, and `_restrict_to_owner` says so out loud when it cannot narrow it."""
    import os
    import stat

    monkeypatch.setenv("AGENT_ROOMS_TOKEN", "tok_test")
    monkeypatch.setattr(service.subprocess, "Popen", _Spawned())
    token_path = state_dir / "token.txt"
    service.start(_args(state_dir=state_dir, token_file=str(token_path), save_token=True))

    if os.name != "nt":
        mode = stat.S_IMODE(token_path.stat().st_mode)
        assert mode & (stat.S_IRGRP | stat.S_IROTH) == 0
    else:
        done = service.subprocess.run(["icacls", str(token_path)], capture_output=True, text=True)
        assert "(F)" in done.stdout
        assert "Everyone" not in done.stdout
