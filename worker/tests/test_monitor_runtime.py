"""Persistent monitor intake, relevance tiers, and bounded continuity."""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cottage_worker import Lease, Worker  # noqa: E402
from executors import EchoExecutor, ReactionResult, StepResult  # noqa: E402


def _event(seq: int, type_: str, *, payload=None, actor="other"):
    return {
        "seq": seq,
        "type": type_,
        "ts": "2026-08-18T00:00:00Z",
        "actor": {"participant_id": actor, "display_name": actor},
        "payload": payload or {},
    }


def _worker() -> Worker:
    worker = Worker(
        base="http://unused",
        room_id="room-test",
        token="unused",
        label="worker-main",
        executor=EchoExecutor(),
    )
    worker.participant_id = "me"
    worker.connection_id = "connection"
    worker.attachment_id = "attachment"
    return worker


def test_page_is_durably_enqueued_before_cursor_advances():
    worker = _worker()
    persisted: list[tuple[int, list[int]]] = []
    # Three arguments since D-089: the reacted set is persisted too, because it was in
    # memory only and a restart therefore answered the same thing twice.
    worker.monitor_state_sink = lambda cursor, pending, reacted=None: persisted.append(
        (cursor, [int(event["seq"]) for event in pending])
    )

    worker._accept_event_page(
        {
            "events": [
                _event(
                    7,
                    "message.posted",
                    payload={"body": "Can you review this?", "to_participant_id": "me"},
                )
            ],
            "cursor": 9,  # includes two privacy-filtered events
        }
    )

    assert worker.cursor == 9
    assert persisted[-1] == (9, [7]), "pending cognition must survive the accepted cursor"
    assert worker.wake_event.is_set()


def test_ambient_is_coalesced_and_routine_noise_never_queues_cognition():
    worker = _worker()
    worker.ambient_debounce_seconds = 5
    worker._accept_event_page(
        {
            "events": [
                _event(1, "presence.changed"),
                _event(2, "activity.noted"),
                _event(3, "message.posted", payload={"body": "Ambient discussion"}),
                _event(4, "task.claim_renewed"),
            ],
            "cursor": 4,
        }
    )

    assert [event["seq"] for event in worker.reaction_queue] == [3]
    assert not worker.wake_event.is_set(), "ambient talk waits for the debounce window"
    assert worker.ambient_due_at is not None and worker.ambient_due_at > time.monotonic()


def test_an_explicit_name_mention_wakes_immediately():
    worker = _worker()
    worker.latest_state = {"you": {"display_name": "Cottage Codex"}}
    worker._accept_event_page(
        {
            "events": [
                _event(
                    5,
                    "message.posted",
                    payload={"body": "@Cottage Codex please inspect the failing check"},
                )
            ],
            "cursor": 5,
        }
    )
    assert worker.reaction_queue[0]["_tier"] == "immediate"
    assert worker.wake_event.is_set()


def test_context_continuity_is_bounded_and_contains_durable_work_facts():
    worker = _worker()
    state = {
        "room": {"charter": "Ship safely and coordinate before touching shared files."},
        "your_work": [{"headline": "Separate monitor from executor"}],
        "checkpoints": {"task": [{"summary": "Added the projected runtime state"}]},
        "blocking_you": [{"type": "conflict", "summary": "Shared transport file"}],
        "recent_relevant_events": [
            _event(
                seq,
                "task.checkpointed" if seq % 2 else "message.posted",
                payload={"summary": f"collaborator output {seq}", "body": f"message {seq}"},
            )
            for seq in range(1, 70)
        ],
    }

    context = worker._continuity(state)

    assert context["room_charter"].startswith("Ship safely")
    assert context["current_work"] == ("Separate monitor from executor",)
    assert context["checkpoints"] == ("Added the projected runtime state",)
    assert len(context["recent_events"]) <= 20
    assert len(context["collaborator_outputs"]) <= 8
    assert context["blockers"]


def test_reconnect_reuses_attachment_label_and_accepted_cursor():
    worker = _worker()
    worker.cursor = 41
    worker.connection_id = "closed-connection"
    requests = []

    def fake_call(method, path, payload=None):
        requests.append((method, path, payload))
        return {
            "connection_id": "new-connection",
            "attachment_id": "same-attachment",
            "may_claim": True,
            "max_lease_seconds": 600,
        }

    worker.call = fake_call  # type: ignore[method-assign]
    worker.reconnect()

    payload = requests[0][2]
    assert payload["attachment_label"] == "worker-main"
    assert payload["since_seq"] == 41
    assert worker.connection_id == "new-connection"
    assert worker.attachment_id == "same-attachment"


def test_resume_gap_rebases_from_projection_without_dropping_pending_reactions():
    worker = _worker()
    pending = _event(8, "message.posted", payload={"body": "Please follow up"})
    worker.reaction_queue = [{**pending, "_tier": "ambient"}]
    persisted = []
    worker.monitor_state_sink = lambda cursor, events, reacted=None: persisted.append(
        (cursor, events)
    )
    worker.call = lambda method, path, payload=None: {  # type: ignore[method-assign]
        "cursor": 45,
        "room": {"charter": "Coordinate durably"},
    }

    worker._recover_event_gap()

    assert worker.cursor == 45
    assert worker.latest_state["room"]["charter"] == "Coordinate durably"
    assert [event["seq"] for event in worker.reaction_queue] == [8]
    assert persisted[-1][0] == 45
    assert worker.wake_event.is_set()


class _FailingExecutor:
    name = "failing"

    def cancel(self):
        return None

    def run_step(self, context):
        raise RuntimeError("model transport failed")

    def run_reaction(self, context):
        raise RuntimeError("model transport failed")


class _ConcernExecutor(_FailingExecutor):
    def run_step(self, context):
        return StepResult(summary="Executor exited without a result", concern="non-zero exit")

    def run_reaction(self, context):
        return ReactionResult(summary="Reaction failed", concern="non-zero exit")


def test_failed_task_turn_keeps_companion_working_and_connected():
    worker = _worker()
    worker.executor = _FailingExecutor()
    worker.watch_interval_seconds = 0
    worker.lease = Lease(
        task_id="task-1",
        fence=1,
        expires_at=time.time() + 60,
        heartbeat_interval_s=20,
        title="Inspect reconnect behavior",
    )
    calls = []
    worker.call = (
        lambda method, path, payload=None: calls.append(  # type: ignore[method-assign]
            (method, path, payload)
        )
        or {}
    )

    worker.advance()

    assert worker.lease is not None
    assert not worker.stopping
    assert any(
        path == "/runtime-state" and payload["state"] == "working"
        for _method, path, payload in calls
    )
    assert any(
        path == "/activity" and payload["phase"] == "failed" for _method, path, payload in calls
    )


def test_unsuccessful_executor_result_is_not_misreported_as_task_completion():
    worker = _worker()
    worker.executor = _ConcernExecutor()
    worker.watch_interval_seconds = 0
    worker.lease = Lease(
        task_id="task-1",
        fence=1,
        expires_at=time.time() + 60,
        heartbeat_interval_s=20,
        title="Inspect reconnect behavior",
    )
    calls = []
    worker.call = (
        lambda method, path, payload=None: calls.append(  # type: ignore[method-assign]
            (method, path, payload)
        )
        or {}
    )

    worker.advance()

    assert worker.lease is not None
    assert not any(path == "/tasks/complete" for _method, path, _payload in calls)
    assert "task-1" not in worker.progress


def test_failed_reaction_turn_stays_pending_and_returns_to_monitoring():
    worker = _worker()
    worker.executor = _FailingExecutor()
    direct = {
        **_event(12, "message.posted", payload={"body": "@worker please look"}),
        "_tier": "immediate",
    }
    worker.reaction_queue = [direct]
    calls = []
    worker.call = (
        lambda method, path, payload=None: calls.append(  # type: ignore[method-assign]
            (method, path, payload)
        )
        or {}
    )

    worker.react_to_room_if_needed({})

    assert [event["seq"] for event in worker.reaction_queue] == [12]
    runtime_states = [
        payload["state"] for _method, path, payload in calls if path == "/runtime-state"
    ]
    assert runtime_states == ["working", "monitoring"]
    assert not worker.stopping


def test_unsuccessful_reaction_result_remains_pending_for_a_later_turn():
    worker = _worker()
    worker.executor = _ConcernExecutor()
    worker.reaction_queue = [
        {
            **_event(13, "message.posted", payload={"body": "@worker please retry"}),
            "_tier": "immediate",
        }
    ]
    worker.call = lambda method, path, payload=None: {}  # type: ignore[method-assign]

    worker.react_to_room_if_needed({})

    assert [event["seq"] for event in worker.reaction_queue] == [13]
    assert 13 not in worker.reacted_seqs
