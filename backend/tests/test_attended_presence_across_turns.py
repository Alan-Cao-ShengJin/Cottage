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
from app.core import presence, store
from app.core import work as work_service
from app.db import database as db
from app.domain.capabilities import ATTENDED_HEARTBEAT_INTERVAL_SECONDS
from app.domain.room import Liveness
from app.util import iso_in

pytestmark = pytest.mark.asyncio


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


async def _one_shot_turn(room, *, declare: str | None = None) -> dict:
    """One MCP call from a turn-based host, from join through to the call returning."""
    joined = await mcp_tools.join_room(
        invitation_token=room.join_token,
        display_name="ChatGPT (connector)",
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
