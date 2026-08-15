"""A turn-based client must not flap between live and gone on every turn (D-060).

Reported by the Codex participant at seq 96 and confirmed at seq 111: one-shot MCP
supervisor calls repeatedly ended an active declaration as `owner_presence_lost`, even
holding a valid `participant_token`, while rebinding the same seat to a persistent poll
loop held it at `live_poll`. Distinct from D-059 despite the identical complaint from
outside: that was a *busy* worker's card going stale on the work clock; this is a
*turn-based* participant's presence going to zero on the transport clock.

**The argument these tests encode.** Nothing closes the connection when a one-shot call
ends — the adapter calls `presence.connect` and never `presence.disconnect`, and each
tool call refreshes the row through `_touch`. What ended the participant was the decay
ladder running on the transport cadence: `idle` at 20s, `stale` plus
`work.stale reason=owner_presence_lost` at 60s, reaped at 80s. A human takes longer than
80 seconds to read a reply and type the next prompt, so an attended participant was
graded absent at *every* turn boundary, forever, with no behaviour on its side able to
prevent it — the only thing such a client can do is act, and between turns it is by
definition not acting. That silently punishes the hosts that declare least, which is the
failure mode `CLAUDE.md` names.

**And the line these tests hold.** The fix is not to hold an attended client live.
Principle 5 stands: nothing here promotes anything to `live_poll`, an attended
connection is still capped at `attended` however fresh it is, and it still walks the
whole ladder to `disconnected` — on its own clock. What is asserted is narrower and, on
this evidence, true: between turns an attended client is honestly `attended` (per
`docs/PRODUCT.md` §5, "healthy, but reachable only while a human is engaged with it"),
because a human could prompt it and it would answer. `disconnected` asserts strictly
more than that, and asserts it falsely.

Adapter level on purpose: every client-visible defect in this project has been in an
adapter or a projection and none in core, and the reported symptom is a property of what
the MCP tool path leaves behind when a call returns.
"""

from __future__ import annotations

import pytest

from app.adapters.mcp import server as mcp_tools
from app.core import presence, projections, store
from app.core import work as work_service
from app.db import database as db
from app.domain.capabilities import (
    ATTENDED_HEARTBEAT_INTERVAL_SECONDS,
    ATTENDED_MAX_LEASE_SECONDS,
    Capability,
    HostClass,
)
from app.domain.commands import ConnectCommand
from app.domain.room import Liveness, RoomPolicy
from app.util import iso_in

pytestmark = pytest.mark.asyncio

#: A second runtime for a seat that already has an attended one: beats on the transport
#: cadence, needs no human, and therefore grades above `attended` while it is fresh.
UNATTENDED_CAPABILITIES = [
    Capability.CAN_RECEIVE_EVENTS,
    Capability.SUPPORTS_PUSH,
    Capability.CAN_INITIATE_FOLLOWUP,
    Capability.CAN_EXECUTE_BACKGROUND,
    Capability.SUPPORTS_TOOLS,
]


async def _card(room, *, recipient_id: str, work_id: str) -> dict:
    """The work card as the board renders it, for whoever is reading."""
    frame = await projections.snapshot(
        room_id=room.room.id, recipient=await store.load_participant(recipient_id)
    )
    return next(w for w in frame["work"] if w["id"] == work_id)


async def _turn_ended(participant_id: str, *, seconds_ago: int) -> None:
    """Stand in for the wall-clock gap between two one-shot calls.

    Backdates every clock a returned call left behind — the connection's beat and the
    declaration's — because the point at issue is precisely that nobody touches either
    of them while a human is reading and typing. Nothing is closed here: the real
    teardown closes nothing either, and pretending otherwise would test a different bug.
    """
    await db.execute(
        "UPDATE connections SET last_heartbeat_at = ? WHERE participant_id = ? "
        "AND closed_at IS NULL",
        (iso_in(-seconds_ago), participant_id),
    )
    await db.execute(
        "UPDATE work_declarations SET heartbeat_at = ?, progress_at = ? "
        "WHERE participant_id = ? AND ended_at IS NULL",
        (iso_in(-seconds_ago), iso_in(-seconds_ago), participant_id),
    )


async def _one_shot_turn(
    room, *, declare: str | None = None, display_name: str = "ChatGPT (connector)"
) -> dict:
    """One MCP call from a turn-based host, from join through to the call returning.

    The name matters when a test wants two seats: an identity is resolved by display
    name, so two turns under one name are one participant re-entering, not two.
    """
    joined = await mcp_tools.join_room(
        invitation_token=room.join_token,
        display_name=display_name,
        execution_mode="human_turn_only",
    )
    assert joined["ok"], joined
    if declare is not None:
        declared = await mcp_tools.declare_current_work(
            headline=declare,
            targets=["docs/PROTOCOL.md"],
            participant_token=joined["participant_token"],
        )
        assert declared["ok"], declared
        joined["work_id"] = declared["work"]["id"]
    return joined


async def test_a_returned_one_shot_call_leaves_the_participant_attended(make_room):
    """The reported failure, at the boundary it was reported at.

    Three minutes of human — past the old 60s stale rung and the old 80s reap, and past
    the flat 120s work window that would have blocked the card under the other reason —
    with the participant doing exactly what a turn-based host does between turns:
    nothing. It must still be present, its declaration must still be active, and the
    next turn must land on the same open connection rather than a closed one.
    """
    room = await make_room()
    joined = await _one_shot_turn(room, declare="Reviewing the protocol doc")
    participant_id = joined["participant_id"]

    await _turn_ended(participant_id, seconds_ago=180)

    reaped = await presence.reap_dead_connections()
    stale = await work_service.mark_stale_declarations(await room.refresh())
    views = await presence.presence_for_room(await room.refresh())

    assert reaped == [], "a human reading a reply is not a dead connection"
    assert stale == [], "nor is it a lapsed work declaration"
    assert views[participant_id].liveness == Liveness.ATTENDED
    assert views[participant_id].connection_count == 1

    # And the board must say the same thing the log does. The sweeper above emitted no
    # `work.stale` and flipped no status, so a card rendered stale here would be a
    # projection asserting what the source of truth denies — one rule, two readers,
    # disagreeing between the flat 120s window and this owner's 900s floor.
    card = await _card(room, recipient_id=participant_id, work_id=joined["work_id"])
    assert card["stale"] is False, "the board and the event log must tell one story"

    # The next turn arrives and is not starting over: same seat, same open connection,
    # and the declaration it made three minutes ago is still the room's answer for it.
    updated = await mcp_tools.update_current_work(
        work_id=joined["work_id"],
        note="picked the thread back up",
        participant_token=joined["participant_token"],
    )
    assert updated["ok"], updated
    open_work = await store.list_open_work(room.room.id)
    assert [w.headline for w in open_work] == ["Reviewing the protocol doc"]
    assert open_work[0].status.value == "active"


async def test_attended_is_the_ceiling_and_not_a_promotion(make_room):
    """Honest capabilities, asserted as the thing that must *not* have happened.

    The failure mode adjacent to this fix is buying presence with a lie. A client that
    only acts when prompted is not pollable on our clock no matter how recently it was
    prompted, so even one second after a call it must grade `attended` and never
    `live_poll` — and the ladder it walks must be its own declared one, not a borrowed
    interval that happens to be long.
    """
    room = await make_room()
    joined = await _one_shot_turn(room)

    views = await presence.presence_for_room(await room.refresh())
    view = views[joined["participant_id"]]

    assert view.liveness == Liveness.ATTENDED, "fresh, and still not live_poll"
    assert view.runtime is not None
    assert view.runtime.heartbeat_interval_s == ATTENDED_HEARTBEAT_INTERVAL_SECONDS
    assert joined["heartbeat_interval_s"] == ATTENDED_HEARTBEAT_INTERVAL_SECONDS, (
        "the client is told the clock it is actually being graded against"
    )


async def test_an_abandoned_attended_client_still_decays_all_the_way(make_room):
    """A browser tab closed yesterday is genuinely gone, and must still say so.

    This is what keeps the fix from being "attended clients never leave". The rungs are
    unchanged — `stale` at 3x the interval, closed at 4x — only the interval is the
    participant's own. So a seat nobody has prompted in half an hour loses its work with
    the honest reason and then its connection, exactly as before.
    """
    room = await make_room()
    joined = await _one_shot_turn(room, declare="Left in a browser tab")
    participant_id = joined["participant_id"]

    # Past 3x300s: the human has plainly gone, and now the room may say so.
    await _turn_ended(participant_id, seconds_ago=1000)
    stale = await work_service.mark_stale_declarations(await room.refresh())

    assert [e.payload["reason"] for e in stale] == ["owner_presence_lost"]
    views = await presence.presence_for_room(await room.refresh())
    assert views[participant_id].liveness == Liveness.STALE

    # And past 4x, the backstop closes it rather than leaving a seat open forever.
    await _turn_ended(participant_id, seconds_ago=1400)
    assert await presence.reap_dead_connections() != []
    views = await presence.presence_for_room(await room.refresh())
    assert views[participant_id].liveness == Liveness.DISCONNECTED


async def test_an_unattended_worker_keeps_the_short_clock(make_room):
    """The blast radius, pinned: this must not become a relaxation for everyone.

    A participant that declared it runs its own loop has promised to beat on the
    transport cadence, so three minutes of silence from it is still evidence of a dead
    runtime and must still be graded as one. If this test ever passes for the same
    reason the first one does, the fix has stopped being about attendedness.
    """
    room = await make_room()
    joined = await mcp_tools.join_room(
        invitation_token=room.join_token,
        display_name="Claude Code",
        execution_mode="unattended_loop",
    )
    await mcp_tools.declare_current_work(
        headline="Migrating the schema", participant_token=joined["participant_token"]
    )

    await _turn_ended(joined["participant_id"], seconds_ago=180)

    stale = await work_service.mark_stale_declarations(await room.refresh())
    assert [e.payload["reason"] for e in stale] == ["owner_presence_lost"]
    assert await presence.reap_dead_connections() != []


async def test_a_room_that_set_a_slower_clock_than_the_attended_default_keeps_it(make_room):
    """The floor is the owner's interval, not the constant that usually produces it.

    `derive_runtime_policy` raises an attended client to 300s with `max`, so a room that
    deliberately configured something longer keeps its own number — and the staleness
    floor must follow the interval rather than the constant. At 600s the floor is 1800s,
    and a card 1000s silent is fine; a reader that had hardcoded the 300s default would
    put the floor at 900s and call it stale, which is the same class of second-reader
    drift as D-061 itself, one indirection further in.

    The progress window is widened alongside it because it is a *different* clock with a
    different meaning (D-059): leaving it at 900s would trip `no_progress` at 1000s and
    the test would pass for a reason that has nothing to do with the heartbeat floor.
    """
    room = await make_room(
        policy=RoomPolicy(heartbeat_interval_s=600, work_progress_stale_after_seconds=3600)
    )
    joined = await _one_shot_turn(room, declare="Reading a long document")
    participant_id = joined["participant_id"]

    assert joined["heartbeat_interval_s"] == 600, "the room's slower clock, not the default"

    await _turn_ended(participant_id, seconds_ago=1000)

    stale = await work_service.mark_stale_declarations(await room.refresh())
    views = await presence.presence_for_room(await room.refresh())

    assert stale == []
    assert views[participant_id].liveness == Liveness.IDLE, "quiet on a 600s clock, not gone"
    assert work_service.heartbeat_cutoff_for(await room.refresh(), views[participant_id]) == 1800, (
        "3x the owner's interval, and the owner's interval came from the room"
    )
    card = await _card(room, recipient_id=participant_id, work_id=joined["work_id"])
    assert card["stale"] is False


async def test_the_best_graded_connection_decides_which_clock_the_card_is_judged_on(make_room):
    """Two seats, same shape, same silence — and the board must grade them differently.

    A seat can hold more than one runtime, and the presence view publishes the *best
    graded* one because that is who the room should coordinate against. The staleness
    floor rides on that choice, so this is where a reader that quietly picked "any
    attended connection means the long clock" diverges from the rule.

    Both participants are attended clients with a second, unattended runtime attached,
    and both have been silent for 200 seconds:

    * `busy` still has that runtime beating, so it grades `live_push`, the published
      interval is the 20s transport cadence, the floor is the room's flat 120s, and 200s
      of silence from something that promised to beat every 20s is real evidence. Stale
      — and the sweeper says so too, with an event behind it.
    * `left` has the same second runtime gone quiet, so the attended connection is the
      best one left, the interval is 300s and the floor 900s. Not stale.

    Nothing here is about who the participants are; it is entirely about which runtime
    is currently answering for the seat.
    """
    room = await make_room()
    busy = await _one_shot_turn(
        room, declare="Reviewing while my worker runs", display_name="ChatGPT (worker up)"
    )
    left = await _one_shot_turn(
        room, declare="Reviewing after my worker stopped", display_name="ChatGPT (worker down)"
    )
    assert busy["participant_id"] != left["participant_id"], "two seats, not one rejoining"

    for seat in (busy, left):
        negotiated = await presence.connect(
            participant=await store.load_participant(seat["participant_id"]),
            command=ConnectCommand(
                capabilities=UNATTENDED_CAPABILITIES, host_class=HostClass.PERSISTENT_LOCAL
            ),
            transport="sse",
        )
        seat["worker_connection_id"] = negotiated.connection.id

    # Both seats: attended connection and declaration 200s cold — inside the 300s
    # attended clock, well outside the 20s transport one.
    await _turn_ended(busy["participant_id"], seconds_ago=200)
    await _turn_ended(left["participant_id"], seconds_ago=200)
    # `busy`'s worker is still beating; `left`'s stopped when the attended turn ended.
    await db.execute(
        "UPDATE connections SET last_heartbeat_at = ? WHERE id = ?",
        (iso_in(0), busy["worker_connection_id"]),
    )

    stale = await work_service.mark_stale_declarations(await room.refresh())
    views = await presence.presence_for_room(await room.refresh())

    assert views[busy["participant_id"]].liveness == Liveness.LIVE_PUSH
    assert views[busy["participant_id"]].runtime.heartbeat_interval_s == 20
    assert views[left["participant_id"]].liveness == Liveness.ATTENDED
    assert (
        views[left["participant_id"]].runtime.heartbeat_interval_s
        == ATTENDED_HEARTBEAT_INTERVAL_SECONDS
    )

    assert [(e.payload["participant_id"], e.payload["reason"]) for e in stale] == [
        (busy["participant_id"], "heartbeat_lapsed")
    ]
    busy_card = await _card(room, recipient_id=left["participant_id"], work_id=busy["work_id"])
    left_card = await _card(room, recipient_id=left["participant_id"], work_id=left["work_id"])
    assert busy_card["stale"] is True, "the board agrees with the event the sweeper emitted"
    assert left_card["stale"] is False, "and with the event it did not emit"


async def test_a_long_clock_buys_freshness_and_never_lease_eligibility(make_room):
    """The floor must not leak into claim policy — they answer different questions.

    A 20-minute room clock puts the staleness floor at an hour, so a quarter-hour-old
    declaration from an attended participant is genuinely current and the board says so.
    That is a statement about *evidence*: nothing has happened that contradicts the card.

    It is not a statement about what the participant may be trusted to hold. Lease
    eligibility asks whether anyone can renew if the human walks away mid-task, and the
    answer is still no however long the room's clock is — so the claim is refused and
    the lease ceiling stays at `ATTENDED_MAX_LEASE_SECONDS`. If a future change derives
    lease length from the heartbeat interval, this fails, and it should.
    """
    room = await make_room(
        policy=RoomPolicy(heartbeat_interval_s=1200, work_progress_stale_after_seconds=3600)
    )
    joined = await _one_shot_turn(room, declare="Fifteen minutes with a document")
    participant_id = joined["participant_id"]

    await _turn_ended(participant_id, seconds_ago=900)

    stale = await work_service.mark_stale_declarations(await room.refresh())
    views = await presence.presence_for_room(await room.refresh())
    runtime = views[participant_id].runtime

    assert stale == []
    assert runtime is not None
    assert runtime.heartbeat_interval_s == 1200
    assert work_service.heartbeat_cutoff_for(await room.refresh(), views[participant_id]) == 3600
    card = await _card(room, recipient_id=participant_id, work_id=joined["work_id"])
    assert card["stale"] is False, "fifteen minutes is not silence on a twenty-minute clock"

    # And none of that made it claimable.
    assert runtime.may_claim is False
    assert runtime.max_lease_seconds == ATTENDED_MAX_LEASE_SECONDS
    task = await mcp_tools.create_task(
        title="Something exclusive", participant_token=room.owner_token
    )
    assert task["ok"], task
    claimed = await mcp_tools.claim_task(
        task_id=task["task"]["id"], participant_token=joined["participant_token"]
    )
    assert claimed["ok"] is False, claimed
    assert "human presence" in claimed["message"], claimed
