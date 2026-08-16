"""A stopped worker cannot leave an invisible executor running behind it."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path

import pytest

WORKER = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(WORKER))

from cottage_worker import Worker  # noqa: E402


HELPER = textwrap.dedent(
    r"""
    import os
    import subprocess
    import sys
    import time
    from pathlib import Path

    from cottage_worker import ContainmentError, RuntimeContainment
    from executors import StepContext, SubprocessExecutor

    state_dir, identity, mode, marker = sys.argv[1:]
    try:
        guard = RuntimeContainment.acquire(
            identity_key=identity,
            label="worker-test",
            state_dir=state_dir,
        )
    except ContainmentError as exc:
        print(f"REFUSED {exc}", flush=True)
        raise SystemExit(9)

    print(guard.runtime_id, flush=True)
    if marker != "-":
        code = (
            "import sys, time\n"
            "sys.stdin.read()\n"
            f"path = {marker!r}\n"
            "while True:\n"
            "    open(path, 'a', encoding='utf-8').write('tick\\n')\n"
            "    time.sleep(0.03)\n"
        )
        executor = SubprocessExecutor(
            [sys.executable, "-c", code],
            timeout_seconds=60,
            containment_fd=guard.containment_fd,
        )
        executor.run_step(
            StepContext(
                task_id="task-test",
                title="Contain this process",
                description="",
                targets=(),
                step=1,
                total_steps=1,
            )
        )
    if mode == "hold":
        while True:
            time.sleep(1)
    guard.close()
    """
)


def _environment() -> dict[str, str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(WORKER) + os.pathsep + env.get("PYTHONPATH", "")
    return env


def _start(
    state_dir: Path,
    *,
    marker: Path | None = None,
    identity: str = "room-test-identity",
) -> subprocess.Popen[str]:
    process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            HELPER,
            str(state_dir),
            identity,
            "hold",
            str(marker) if marker else "-",
        ],
        env=_environment(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert process.stdout is not None
    started = process.stdout.readline().strip()
    assert started.startswith("runtime-"), process.stderr.read() if process.stderr else ""
    return process


def _run_once(
    state_dir: Path, *, identity: str = "room-test-identity"
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", HELPER, str(state_dir), identity, "once", "-"],
        env=_environment(),
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )


def _stop_abruptly(process: subprocess.Popen[str]) -> None:
    process.kill()
    process.wait(timeout=15)


def test_a_second_process_with_the_same_local_identity_is_refused(tmp_path):
    first = _start(tmp_path)
    try:
        second = _run_once(tmp_path)
        assert second.returncode == 9
        assert "already active locally" in second.stdout
        assert "refusing" in second.stdout
    finally:
        _stop_abruptly(first)


def test_abrupt_worker_death_kills_a_running_descendant(tmp_path):
    marker = tmp_path / "descendant.txt"
    worker = _start(tmp_path, marker=marker)
    deadline = time.time() + 10
    while time.time() < deadline and not marker.exists():
        time.sleep(0.03)
    assert marker.exists(), "descendant never started, so this test proves nothing"

    _stop_abruptly(worker)
    time.sleep(0.5)
    settled = marker.read_text(encoding="utf-8").count("tick")
    time.sleep(0.5)
    assert marker.read_text(encoding="utf-8").count("tick") == settled


def test_an_unclean_exit_is_audited_and_a_real_restart_gets_a_new_id(tmp_path):
    first = _start(tmp_path)
    assert first.stdout is not None
    # `_start` consumed the id line; the durable record is the evidence after death.
    first_state = next(tmp_path.glob("*.json")).read_text(encoding="utf-8")
    _stop_abruptly(first)
    time.sleep(0.5)

    restarted = _run_once(tmp_path)
    assert restarted.returncode == 0, restarted.stderr
    assert "recovering unclean runtime record" in restarted.stderr
    second_id = restarted.stdout.strip()
    assert second_id.startswith("runtime-")
    assert second_id not in first_state


def test_stop_is_immediate_and_idempotent():
    class Executor:
        name = "test"

        def __init__(self) -> None:
            self.cancel_count = 0

        def cancel(self) -> None:
            self.cancel_count += 1

        def run_step(self, _context):  # pragma: no cover - not part of this test
            pytest.fail("no step should run")

    executor = Executor()
    worker = Worker(
        base="https://example.invalid",
        room_id="room-test",
        token="not-a-real-token",
        label="worker-test",
        executor=executor,
    )

    worker.request_stop()
    worker.request_stop()

    assert worker.stopping is True
    assert worker.stop_event.is_set()
    assert executor.cancel_count == 1


def test_a_concurrently_cancelled_final_step_has_zero_room_mutation():
    from cottage_worker import Lease
    from executors import StepResult

    entered = threading.Event()
    cancelled = threading.Event()
    calls: list[tuple[str, str]] = []

    class Executor:
        name = "race"

        def cancel(self) -> None:
            cancelled.set()

        def run_step(self, _context):  # type: ignore[no-untyped-def]
            entered.set()
            assert cancelled.wait(5), "the concurrent stop never reached the executor"
            return StepResult(summary="too late", done=True)

    executor = Executor()
    worker = Worker(
        base="https://example.invalid",
        room_id="room-test",
        token="not-a-real-token",
        label="worker-test",
        executor=executor,
        watch_interval_seconds=0.01,
        steps_per_task=2,
    )
    worker.connection_id = "connection-test"
    worker.participant_id = "participant-test"
    worker.lease = Lease(
        task_id="task-test",
        fence=1,
        expires_at=time.time() + 60,
        heartbeat_interval_s=20,
        title="Race",
    )
    # Exercise the final step without the first-step in_progress mutation. From the
    # instant this real concurrent step starts, cancellation must make the room a
    # read/write-free boundary.
    worker.progress["task-test"] = 1
    worker.call = lambda method, path, payload=None: calls.append((method, path)) or {}  # type: ignore[method-assign]

    advancing = threading.Thread(target=worker.advance)
    advancing.start()
    assert entered.wait(5), "the executor never entered its final step"
    worker.request_stop()
    advancing.join(5)

    assert not advancing.is_alive(), "the cancelled final step did not drain"
    assert calls == []


def test_invitation_duplicate_is_refused_before_join(monkeypatch, tmp_path):
    import cottage_worker

    invitation = "test-invitation-never-sent"
    base = "https://example.invalid"
    fingerprint = __import__("hashlib").sha256(invitation.encode()).hexdigest()
    identity = f"invitation:{base}:{fingerprint}"
    incumbent = _start(tmp_path, identity=identity)
    joined = False

    def join_would_mutate(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        nonlocal joined
        joined = True
        pytest.fail("duplicate launch reached invitation join")

    monkeypatch.setattr(cottage_worker, "join_with_invitation", join_would_mutate)
    monkeypatch.setenv("COTTAGE_INVITATION", invitation)
    monkeypatch.delenv("COTTAGE_PARTICIPANT_TOKEN", raising=False)
    try:
        result = cottage_worker.main(
            [
                "--base",
                base,
                "--label",
                "worker-test",
                "--runtime-dir",
                str(tmp_path),
                "--max-cycles",
                "0",
            ]
        )
    finally:
        _stop_abruptly(incumbent)

    assert result == 3
    assert joined is False


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission contract")
def test_insecure_runtime_directory_is_refused(tmp_path):
    from cottage_worker import ContainmentError, RuntimeContainment

    insecure = tmp_path / "insecure"
    insecure.mkdir(mode=0o755)
    insecure.chmod(0o755)
    with pytest.raises(ContainmentError, match="permissions"):
        RuntimeContainment.acquire(
            identity_key="room-test-identity",
            label="worker-test",
            state_dir=str(insecure),
        )


@pytest.mark.skipif(os.name == "nt", reason="POSIX link and ownership contract")
def test_runtime_directory_symlink_is_refused(tmp_path):
    from cottage_worker import ContainmentError, RuntimeContainment

    real = tmp_path / "real"
    real.mkdir(mode=0o700)
    linked = tmp_path / "linked"
    linked.symlink_to(real, target_is_directory=True)
    with pytest.raises(ContainmentError, match="real directory"):
        RuntimeContainment.acquire(
            identity_key="room-test-identity",
            label="worker-test",
            state_dir=str(linked),
        )


@pytest.mark.skipif(os.name == "nt", reason="POSIX foreground process-group contract")
def test_containment_does_not_detach_the_worker_from_its_terminal_group(tmp_path):
    code = textwrap.dedent(
        """
        import os
        import sys
        from cottage_worker import RuntimeContainment

        before = (os.getsid(0), os.getpgrp())
        guard = RuntimeContainment.acquire(
            identity_key="foreground-test",
            label="worker-test",
            state_dir=sys.argv[1],
        )
        print(before == (os.getsid(0), os.getpgrp()), flush=True)
        guard.close()
        """
    )
    result = subprocess.run(
        [sys.executable, "-c", code, str(tmp_path)],
        env=_environment(),
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "True"


@pytest.mark.skipif(sys.platform != "linux", reason="Linux-only signal/session contract")
def test_terminal_sigint_reaches_worker_but_not_its_watchdog(tmp_path):
    code = textwrap.dedent(
        """
        import os
        import signal
        import sys
        import time
        from cottage_worker import RuntimeContainment

        interrupted = False

        def on_interrupt(_signum, _frame):
            global interrupted
            interrupted = True

        signal.signal(signal.SIGINT, on_interrupt)
        guard = RuntimeContainment.acquire(
            identity_key="sigint-test",
            label="worker-test",
            state_dir=sys.argv[1],
        )
        watchdog_pid = int(guard._state["watchdog_pid"])
        print(watchdog_pid, flush=True)
        deadline = time.monotonic() + 5
        while not interrupted and time.monotonic() < deadline:
            time.sleep(0.01)
        print(interrupted, RuntimeContainment._pid_alive(watchdog_pid), flush=True)
        guard.close()
        """
    )
    worker = subprocess.Popen(
        [sys.executable, "-c", code, str(tmp_path)],
        env=_environment(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    assert worker.stdout is not None
    assert worker.stderr is not None
    try:
        watchdog_pid = int(worker.stdout.readline().strip())
        os.killpg(worker.pid, signal.SIGINT)
        stdout, stderr = worker.communicate(timeout=10)
    finally:
        if worker.poll() is None:
            os.killpg(worker.pid, signal.SIGKILL)
            worker.wait(timeout=5)

    assert worker.returncode == 0, stderr
    assert stdout.strip() == "True True"
    assert watchdog_pid != worker.pid


def _start_watchdog(read_fd: int) -> subprocess.Popen[str]:
    return subprocess.Popen(
        [
            sys.executable,
            str(WORKER / "cottage_worker.py"),
            "--containment-watchdog",
            str(read_fd),
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        close_fds=True,
        pass_fds=(read_fd,),
        start_new_session=True,
    )


@pytest.mark.skipif(sys.platform != "linux", reason="Linux-only watchdog pipe contract")
def test_graceful_record_drains_a_later_registration_before_eof():
    read_fd, write_fd = os.pipe()
    watchdog = _start_watchdog(read_fd)
    os.close(read_fd)
    victim = subprocess.Popen(
        [sys.executable, "-c", "import time\nwhile True: time.sleep(1)"],
        start_new_session=True,
    )
    try:
        # Writes at or below PIPE_BUF are atomic but ordering between independent
        # writers is not guaranteed. Model the executor bootstrap whose R arrives
        # after the worker has initiated graceful shutdown with G.
        os.write(write_fd, b"G\n")
        os.write(write_fd, f"R\t{victim.pid}\t{victim.pid}\t\n".encode("ascii"))
        os.close(write_fd)
        write_fd = -1

        stdout, stderr = watchdog.communicate(timeout=10)
        victim.wait(timeout=5)
    finally:
        if write_fd >= 0:
            os.close(write_fd)
        if watchdog.poll() is None:
            watchdog.kill()
            watchdog.wait(timeout=5)
        if victim.poll() is None:
            os.killpg(victim.pid, signal.SIGKILL)
            victim.wait(timeout=5)

    assert watchdog.returncode == 0, stderr or stdout
    assert victim.returncode is not None


@pytest.mark.skipif(sys.platform != "linux", reason="Linux-only orphan process-group contract")
def test_done_record_keeps_group_tracked_while_orphan_descendant_remains(tmp_path):
    marker = tmp_path / "orphan.txt"
    descendant_code = textwrap.dedent(
        f"""
        import time
        from pathlib import Path
        marker = Path({str(marker)!r})
        while True:
            with marker.open("a", encoding="ascii") as output:
                output.write("tick\\n")
            time.sleep(0.03)
        """
    )
    leader_code = textwrap.dedent(
        f"""
        import subprocess
        import sys
        child = subprocess.Popen([sys.executable, "-c", {descendant_code!r}])
        print(child.pid, flush=True)
        """
    )
    read_fd, write_fd = os.pipe()
    watchdog = _start_watchdog(read_fd)
    os.close(read_fd)
    leader = subprocess.Popen(
        [sys.executable, "-c", leader_code],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    assert leader.stdout is not None
    descendant_pid = int(leader.stdout.readline().strip())
    os.write(write_fd, f"R\t{leader.pid}\t{leader.pid}\t\n".encode("ascii"))
    leader.wait(timeout=5)
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and not marker.exists():
        time.sleep(0.02)
    assert marker.exists(), "the orphan descendant never ran"

    try:
        os.write(write_fd, f"D\t{leader.pid}\t\n".encode("ascii"))
        os.close(write_fd)
        write_fd = -1
        stdout, stderr = watchdog.communicate(timeout=10)
        time.sleep(0.2)
        settled = marker.read_text(encoding="ascii").count("tick")
        time.sleep(0.2)
    finally:
        if write_fd >= 0:
            os.close(write_fd)
        if watchdog.poll() is None:
            watchdog.kill()
            watchdog.wait(timeout=5)
        try:
            os.killpg(leader.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass

    assert watchdog.returncode == 0, stderr or stdout
    assert marker.read_text(encoding="ascii").count("tick") == settled
    assert descendant_pid > 1


def test_shutdown_can_retry_each_room_cleanup_after_transport_failure():
    from cottage_worker import Lease

    worker = Worker(
        base="https://example.invalid",
        room_id="room-test",
        token="not-a-real-token",
        label="worker-test",
    )
    worker.connection_id = "connection-test"
    worker.lease = Lease(
        task_id="task-test",
        fence=1,
        expires_at=time.time() + 60,
        heartbeat_interval_s=20,
    )
    failures = 2

    def flaky_call(_method, _path, _payload=None):  # type: ignore[no-untyped-def]
        nonlocal failures
        if failures:
            failures -= 1
            raise __import__("urllib.error").error.URLError("offline")
        return {}

    worker.call = flaky_call  # type: ignore[method-assign]
    worker.shutdown()
    assert worker.shutdown_started is False
    assert worker.lease is not None
    assert worker.connection_id

    worker.shutdown()
    assert worker.shutdown_started is True
    assert worker.lease is None
    assert worker.connection_id == ""
