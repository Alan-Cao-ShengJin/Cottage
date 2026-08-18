"""Live activity notes: narration between state changes (D-082).

The product failure being closed: between claiming a task and checkpointing it, a
worker can do ten minutes of real work while the room shows one unchanged line. To a
human watching, that is indistinguishable from a crash. Notes fill that silence.

The tests that matter most here are the **negative** ones, because the whole design is
a set of restrictions and every one of them is a thing it would have been easier to
allow:

* a note changes no state — no work card moves, no task moves;
* a note does not refresh `progress_at`, so narrating "still working" cannot be used
  to look busy without being busy (the property D-059 built the second clock for);
* a note is not relayed into other agents' coordination view, so a human-facing
  feature cannot quietly spend every peer's context (`docs/PRODUCT.md` §9);
* work-phase `monitoring` is not transport `disconnected`, which is the exact conflation
  that makes a healthy waiting companion read as a dead one.

Delete any one of those and the feature still "works" in a demo while being wrong.
"""

from __future__ import annotations

import httpx
import pytest

from app.adapters.mcp import compact
from app.adapters.mcp import server as mcp_tools
from app.core import activity, eventlog, presence, projections, store, tasks, work
from app.core.errors import InvalidCommand, PrivacyViolation
from app.domain.activity import ActivityPhase
from app.domain.commands import (
    ClaimTaskCommand,
    ConnectCommand,
    CreateTaskCommand,
    DeclareWorkCommand,
    NoteActivityCommand,
)
from app.domain.disclosure import Disclosure
from app.domain.events import EventType
from app.domain.identity import TrustTier
from app.domain.room import Liveness, PrivacyClass, RuntimeRole
from app.main import app

from .conftest import FULL_CAPABILITIES

pytestmark = pytest.mark.asyncio


async def _notes_visible_to(room_id: str, member) -> list[dict]:
    seen = await projections.visible_events_since(
        room_id=room_id, recipient=member.participant, since_seq=0
    )
    return [e for e in seen if e["type"] == EventType.ACTIVITY_NOTED.value]


async def test_a_note_is_persisted_to_the_log_and_visible_to_the_room(make_room, join):
    """Requirement: progress events are persisted *and* delivered, not one or the other."""
    room = await make_room()
    member = await join(room, display_name="Claude")

    await activity.note(
        participant=member.participant,
        command=NoteActivityCommand(
            phase=ActivityPhase.WORKING, summary="Reviewing websocket reconnect handling"
        ),
    )

    notes = await _notes_visible_to(room.room.id, member)
    assert len(notes) == 1
    assert notes[0]["payload"]["summary"] == "Reviewing websocket reconnect handling"
    assert notes[0]["payload"]["phase"] == "working"
    # In the log, so a reconnecting client replays it from its cursor like anything else.
    assert any(
        e.type == EventType.ACTIVITY_NOTED for e in await eventlog.read_since(room.room.id, 0)
    )


async def test_two_participants_in_one_room_both_see_it_and_a_third_room_does_not(make_room, join):
    """Fanout and tenancy in one: same room yes, different room no."""
    room = await make_room()
    author = await join(room, display_name="Claude")
    watcher = await join(room, display_name="Human")

    elsewhere = await make_room(name="Another room")
    outsider = await join(elsewhere, display_name="Nobody")

    await activity.note(
        participant=author.participant,
        command=NoteActivityCommand(phase=ActivityPhase.WORKING, summary="Running backend tests"),
    )

    assert len(await _notes_visible_to(room.room.id, author)) == 1
    assert len(await _notes_visible_to(room.room.id, watcher)) == 1, (
        "a second participant in the same room must receive the same event"
    )
    assert await _notes_visible_to(elsewhere.room.id, outsider) == []


async def test_a_note_changes_no_state(make_room, join):
    """The load-bearing restriction: narration is not a state change.

    If a note could move a card or a task, every one of them would need the
    authorization, fencing and conflict handling that real state changes carry — and
    the cheap high-frequency channel this exists to be would stop being cheap.
    """
    room = await make_room()
    member = await join(room, display_name="Claude")
    declared = await work.declare(
        participant=member.participant,
        command=DeclareWorkCommand(headline="Refactoring auth", targets=["auth.py"]),
    )
    task = await tasks.create(
        participant=member.participant,
        command=CreateTaskCommand(title="Ship it", targets=["ship.py"]),
    )
    claimed = await tasks.claim(
        participant=member.participant, command=ClaimTaskCommand(task_id=task.id)
    )

    await activity.note(
        participant=member.participant,
        command=NoteActivityCommand(
            phase=ActivityPhase.WORKING, summary="Comparing worker lifecycle with lease handling"
        ),
    )

    after_card = await store.load_work(declared.id)
    after_task = await store.load_task(task.id)
    assert after_card.headline == declared.headline
    assert after_card.status == declared.status
    assert after_task.status == claimed.status
    assert after_task.fence == claimed.fence


async def test_a_note_does_not_refresh_the_progress_clock(make_room, join):
    """Saying so is not doing so (D-059).

    `progress_at` is what makes `no_progress` reachable for a worker that is connected
    but wedged. If narration refreshed it, an agent looping "still working…" would stay
    green forever — which is precisely the false-liveness this project refuses to
    manufacture (principle 5).
    """
    room = await make_room()
    member = await join(room, display_name="Claude")
    declared = await work.declare(
        participant=member.participant,
        command=DeclareWorkCommand(headline="Refactoring auth", targets=["auth.py"]),
    )
    before = (await store.load_work(declared.id)).progress_at

    for n in range(3):
        await activity.note(
            participant=member.participant,
            command=NoteActivityCommand(phase=ActivityPhase.WORKING, summary=f"Step {n}"),
        )

    assert (await store.load_work(declared.id)).progress_at == before, (
        "narration must never stand in as evidence that the work itself moved"
    )


async def test_notes_are_kept_out_of_the_agent_coordination_view_but_advance_the_cursor(
    make_room, join
):
    """A human-facing feature must not spend every peer's context (§9).

    The cursor still crosses them, so suppression costs no correctness: a client that
    later asks for `detail="full"`, or reads the SSE stream, sees everything.
    """
    room = await make_room()
    author = await join(room, display_name="Claude")
    peer = await join(room, display_name="Codex")

    before = await eventlog.current_seq(room.room.id)
    for n in range(5):
        await activity.note(
            participant=author.participant,
            command=NoteActivityCommand(phase=ActivityPhase.WORKING, summary=f"Step {n}"),
        )
    after = await eventlog.current_seq(room.room.id)
    assert after > before, "the notes really were written to the log"

    visible = await projections.visible_events_since(
        room_id=room.room.id, recipient=peer.participant, since_seq=before
    )
    assert visible, "they are visible to the room"

    shown, dropped = compact.events(visible)
    assert shown == [], "but a coordinating agent is not made to read them"
    assert dropped == 0, (
        "and they are not reported as omitted history, which would send a client "
        "paging back for something this view will never show"
    )


async def test_a_tool_phase_must_name_its_tool(make_room, join):
    """A duration that starts and never names what is running is not worth showing."""
    room = await make_room()
    member = await join(room, display_name="Claude")

    with pytest.raises(InvalidCommand):
        await activity.note(
            participant=member.participant,
            command=NoteActivityCommand(
                phase=ActivityPhase.TOOL_STARTED, summary="Running something"
            ),
        )


async def test_the_schema_offers_nowhere_to_put_reasoning(make_room, join):
    """The field an agent most wants to add must not exist.

    `CommandMeta` forbids extras, so this is a rejection rather than a silent drop —
    the distinction that decision D-024/026/027/030 were all about.
    """
    with pytest.raises(Exception) as caught:
        NoteActivityCommand(
            phase=ActivityPhase.WORKING,
            summary="Running tests",
            reasoning="First I considered X, then I realised Y",
        )
    assert "reasoning" in str(caught.value)


async def test_a_note_passes_through_the_disclosure_boundary(make_room, join):
    """Free text at high frequency is the most inviting place to leak, so it is guarded."""
    room = await make_room()
    stranger = await join(room, display_name="Guest", trust=TrustTier.UNTRUSTED)

    with pytest.raises(PrivacyViolation):
        await activity.note(
            participant=stranger.participant,
            command=NoteActivityCommand(
                phase=ActivityPhase.WORKING,
                summary="Working on it",
                disclosure=Disclosure(privacy_class=PrivacyClass.ORG_INTERNAL),
            ),
        )


async def test_work_phase_monitoring_is_not_transport_disconnected(make_room):
    """The invariant the whole spec turns on: `monitoring != disconnected`.

    A companion that finishes a task and says so is *more* present than one that says
    nothing, so the room must not read the announcement of idleness as an exit.
    """
    room = await make_room()
    joined = await mcp_tools.join_room(
        invitation_token=room.join_token,
        display_name="Claude",
        execution_mode="unattended_loop",
    )
    assert joined["ok"], joined

    noted = await mcp_tools.note_activity(
        phase="monitoring",
        summary="Finished the migration, waiting for more work",
        participant_token=joined["participant_token"],
    )
    assert noted["ok"], noted

    from app.core import presence

    views = await presence.presence_for_room(await room.refresh())
    grade = views[joined["participant_id"]].liveness
    assert grade in {Liveness.LIVE_POLL, Liveness.LIVE_PUSH, Liveness.ATTENDED}, (
        f"a participant that just spoke is present, not {grade.value}"
    )


async def test_replaying_the_same_note_does_not_append_a_second_one(make_room, join):
    """Reconnect redelivery is normal, so a retried note must be idempotent."""
    room = await make_room()
    member = await join(room, display_name="Claude")
    cmd = NoteActivityCommand(
        phase=ActivityPhase.WORKING, summary="Running backend tests", command_id="cmd-fixed-1"
    )

    first = await activity.note(participant=member.participant, command=cmd)
    second = await activity.note(participant=member.participant, command=cmd)

    assert first["replayed"] is False
    assert second["replayed"] is True
    # The property that matters is that the log did not grow. A replayed command
    # reports no seq of its own — same as `messages.post` — because it appended
    # nothing; the original event is still the only one there.
    assert len(await _notes_visible_to(room.room.id, member)) == 1


async def test_http_activity_is_attributed_to_the_callers_runtime_and_survives_snapshot(
    make_room, join
):
    room = await make_room()
    member = await join(room, display_name="Claude")
    runtime = await presence.connect(
        participant=member.participant,
        command=ConnectCommand(
            capabilities=FULL_CAPABILITIES,
            transport="long_poll",
            attachment_label="worker-main",
            attachment_resumable=True,
            runtime_role=RuntimeRole.COMPANION,
            executor_kind="subprocess",
        ),
        transport="long_poll",
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://arp.test"
    ) as client:
        response = await client.post(
            f"/api/rooms/{room.room.id}/activity",
            headers={"Authorization": f"Bearer {member.token}"},
            json={
                "phase": "tool_started",
                "summary": "Running the reconnect suite",
                "tool": "pytest worker/tests",
                "connection_id": runtime.connection.id,
            },
        )

    assert response.status_code == 201, response.text
    assert response.json()["attachment_id"] == runtime.connection.attachment_id
    snapshot = await projections.snapshot(
        room_id=room.room.id,
        recipient=member.participant,
    )
    latest = snapshot["latest_activity"]
    assert len(latest) == 1
    assert latest[0]["payload"]["attachment_id"] == runtime.connection.attachment_id
    assert latest[0]["payload"]["summary"] == "Running the reconnect suite"


async def test_activity_cannot_be_attributed_through_a_sibling_participants_connection(
    make_room, join
):
    room = await make_room()
    author = await join(room, display_name="Claude")
    sibling = await join(room, display_name="Codex")

    with pytest.raises(InvalidCommand, match="owned by this participant"):
        await activity.note(
            participant=author.participant,
            command=NoteActivityCommand(
                phase=ActivityPhase.WORKING,
                summary="This attribution must be rejected",
                connection_id=sibling.connection_id,
            ),
        )
