"""Which runtime of a seat is doing what, said honestly (D-054, gate 5).

A seat may be a chat window *and* a background worker. "This participant is live"
then answers the wrong question: whether to expect a prompt reply depends on which
runtime is live, not on whether one of them is.

The harder half is honesty about *kind*. A runtime's role, executor and model are
things it says about itself and the room cannot check any of them — so they are
grouped under `declared` rather than mixed in with liveness, which the room derives
and stands behind. And nothing in the server may branch on them: behaviour comes
from negotiated capabilities alone (principle 4), so a companion worker is described
differently and treated identically.
"""

from __future__ import annotations

import pytest

from app.adapters.mcp import compact
from app.core import presence, projections, store
from app.domain.capabilities import Capability, HostClass
from app.domain.commands import ConnectCommand
from app.domain.room import Liveness, RuntimeRole

pytestmark = pytest.mark.asyncio

WORKER_CAPS = [
    Capability.CAN_RECEIVE_EVENTS,
    Capability.SUPPORTS_POLL,
    Capability.SUPPORTS_RESUME,
    Capability.CAN_INITIATE_FOLLOWUP,
    Capability.CAN_EXECUTE_BACKGROUND,
    Capability.SUPPORTS_TOOLS,
]

SURFACE_CAPS = [
    Capability.CAN_RECEIVE_EVENTS,
    Capability.SUPPORTS_POLL,
    Capability.REQUIRES_HUMAN_PRESENCE,
    Capability.SUPPORTS_TOOLS,
]


async def _attach(member, *, label, role, kind, model="", capabilities, transport="long_poll"):
    return await presence.connect(
        participant=member.participant,
        command=ConnectCommand(
            capabilities=capabilities,
            host_class=HostClass.PERSISTENT_LOCAL,
            attachment_label=label,
            attachment_resumable=True,
            runtime_role=role,
            executor_kind=kind,
            executor_model=model,
        ),
        transport=transport,
    )


async def test_one_seat_two_runtimes_are_described_separately(make_room, join):
    """The state this whole feature exists for.

    Before it, a seat with a companion attached reported one liveness, and a human
    reading the rail could not tell whether the thing that was live was the surface
    they could talk to or the process they could not.
    """
    room = await make_room()
    member = await join(room, display_name="Alan's agent")
    await _attach(
        member,
        label="chat",
        role=RuntimeRole.CONTROL_SURFACE,
        kind="human",
        capabilities=SURFACE_CAPS,
    )
    await _attach(
        member,
        label="worker-main",
        role=RuntimeRole.COMPANION,
        kind="subprocess",
        capabilities=WORKER_CAPS,
    )

    views = await presence.presence_for_room(await room.refresh())
    runtimes = {r.label: r for r in views[member.participant.id].runtimes}
    assert set(runtimes) >= {"chat", "worker-main"}
    assert runtimes["chat"].declared.role is RuntimeRole.CONTROL_SURFACE
    assert runtimes["worker-main"].declared.role is RuntimeRole.COMPANION
    assert runtimes["worker-main"].declared.executor_kind == "subprocess"


async def test_liveness_is_per_runtime_and_derived(make_room, join):
    """A runtime that stopped must stop being live on its own, not with its seat.

    Derived from open connections on every read rather than stored, so a process
    that died without saying anything is not live the moment its heartbeat lapses —
    with no flag anyone has to remember to clear (D-044).
    """
    room = await make_room()
    member = await join(room, display_name="Alan's agent")
    surface = await _attach(
        member,
        label="chat",
        role=RuntimeRole.CONTROL_SURFACE,
        kind="human",
        capabilities=SURFACE_CAPS,
    )
    await _attach(
        member,
        label="worker-main",
        role=RuntimeRole.COMPANION,
        kind="echo",
        capabilities=WORKER_CAPS,
    )
    await presence.disconnect(participant=member.participant, connection_id=surface.connection.id)

    views = await presence.presence_for_room(await room.refresh())
    runtimes = {r.label: r for r in views[member.participant.id].runtimes}
    assert "chat" not in runtimes, "a closed runtime is not a live one"
    assert runtimes["worker-main"].liveness is not Liveness.DISCONNECTED
    assert views[member.participant.id].liveness is not Liveness.DISCONNECTED


async def test_a_declaration_is_recorded_and_never_verified(make_room, join):
    """The room takes a runtime's word for what it is, and says that it did.

    `declared` is a nested object rather than flattened fields precisely so a reader
    cannot mistake a self-report for an observation. This is the same rule that makes
    an invitation-chosen display name carry `name_is_self_asserted` (D-025).
    """
    room = await make_room()
    member = await join(room, display_name="Honest worker")
    await _attach(
        member,
        label="worker-main",
        role=RuntimeRole.COMPANION,
        kind="subprocess",
        model="something-it-cannot-prove",
        capabilities=WORKER_CAPS,
    )

    views = await presence.presence_for_room(await room.refresh())
    runtime = next(r for r in views[member.participant.id].runtimes if r.label == "worker-main")
    assert runtime.declared.model == "something-it-cannot-prove"
    dumped = runtime.model_dump(mode="json")
    assert "model" not in dumped, "never at the top level, where it would read as fact"
    assert dumped["declared"]["model"] == "something-it-cannot-prove"


async def test_an_undeclared_runtime_is_described_as_nothing(make_room, join):
    """Silence is not evidence, and guessing from `host_class` would be the
    vendor-label error in a new costume (principle 4)."""
    room = await make_room()
    member = await join(room, display_name="Says nothing")

    views = await presence.presence_for_room(await room.refresh())
    runtime = views[member.participant.id].runtimes[0]
    assert runtime.declared.role is RuntimeRole.UNSPECIFIED
    assert runtime.declared.executor_kind == ""
    assert runtime.declared.model == ""


async def test_an_ephemeral_connection_is_still_a_runtime(make_room, join):
    """NULL means "no durable runtime", never "no runtime" (D-034).

    A client that declares no label still gets an entry, because a seat with one
    anonymous connection is a seat somebody may be trying to reach.
    """
    room = await make_room()
    member = await join(room, display_name="Ephemeral")
    views = await presence.presence_for_room(await room.refresh())
    runtime = views[member.participant.id].runtimes[0]
    assert runtime.is_attachment is False
    assert runtime.ref.startswith("con_")


async def test_the_compact_view_shows_runtimes_only_when_there_is_a_choice(make_room, join):
    """Context is the caller's money on a metered host (`docs/INTEROP.md` §4).

    A seat with one runtime is fully described by its own liveness; repeating that
    per runtime spends the reader's context to say nothing.
    """
    room = await make_room()
    solo = await join(room, display_name="One runtime")
    both = await join(room, display_name="Two runtimes")
    await _attach(
        both,
        label="chat",
        role=RuntimeRole.CONTROL_SURFACE,
        kind="human",
        capabilities=SURFACE_CAPS,
    )
    await _attach(
        both,
        label="worker-main",
        role=RuntimeRole.COMPANION,
        kind="subprocess",
        capabilities=WORKER_CAPS,
    )

    snapshot = await projections.snapshot(room_id=room.room.id, recipient=room.owner)
    view = compact.room_state(snapshot)
    by_id = {p["participant_id"]: p for p in view["participants"]}
    assert "runtimes" not in by_id[solo.participant.id]
    shown = by_id[both.participant.id]["runtimes"]
    roles = {r["declared"]["role"] for r in shown if "declared" in r}
    assert roles == {"control_surface", "companion"}
    # The seat's own anonymous connection from joining is listed too, and carries no
    # `declared` block at all — silence stays silence rather than becoming a default.
    assert any("declared" not in r for r in shown)


async def test_the_role_changes_nothing_about_what_a_seat_may_do(make_room, join):
    """Descriptive, never a permission.

    A room that started routing work by declared role would have reinvented vendor
    labels with extra steps — and a worker could then widen its own treatment by
    editing one string.
    """
    room = await make_room()
    honest = await join(room, display_name="Honest")
    liar = await join(room, display_name="Claims to be a person")
    await _attach(
        honest,
        label="w",
        role=RuntimeRole.COMPANION,
        kind="subprocess",
        capabilities=WORKER_CAPS,
    )
    await _attach(
        liar,
        label="w",
        role=RuntimeRole.CONTROL_SURFACE,
        kind="human",
        capabilities=WORKER_CAPS,
    )

    views = await presence.presence_for_room(await room.refresh())
    a = views[honest.participant.id]
    b = views[liar.participant.id]
    assert a.runtime is not None and b.runtime is not None
    assert a.runtime.may_claim == b.runtime.may_claim
    assert a.runtime.max_lease_seconds == b.runtime.max_lease_seconds
    assert a.runtime.delivery_mode == b.runtime.delivery_mode


async def test_a_returning_runtime_may_correct_what_it_said(make_room, join):
    """A redeployed process is telling the truth about itself now.

    Pinning it to an earlier claim would make the record less accurate over time,
    which is the same reasoning that already lets `host_class` and `is_resumable` be
    re-declared on reattach.
    """
    room = await make_room()
    member = await join(room, display_name="Upgraded")
    await _attach(
        member, label="w", role=RuntimeRole.COMPANION, kind="echo", capabilities=WORKER_CAPS
    )
    await _attach(
        member,
        label="w",
        role=RuntimeRole.COMPANION,
        kind="subprocess",
        model="an-agent-cli",
        capabilities=WORKER_CAPS,
    )

    attachments = await store.list_attachments(room.room.id)
    mine = [a for a in attachments.values() if a.participant_id == member.participant.id]
    assert len(mine) == 1, "one label, one runtime, however many times it reconnects"
    assert mine[0].executor_kind == "subprocess"
    assert mine[0].executor_model == "an-agent-cli"
