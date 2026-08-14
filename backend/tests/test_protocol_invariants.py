"""Executable protocol invariants.

These are not ordinary unit tests. Each one pins a guarantee that `docs/PROTOCOL.md`
or `docs/SECURITY.md` makes to participants, and each is written so that the *only*
way to make it pass is to actually hold the guarantee. If one of these fails, the
product is broken in a way a user would notice, not merely a refactor gone wrong.

Invariants covered:
  I1  one active exclusive lease per task
  I2  a stale fence can never mutate protected task state
  I3  a duplicate command_id cannot execute twice
  I4  reconnect from seq=N cannot silently miss authorized events
  I5  unauthorized privacy classes never fan out
  I6  cross-org `org_internal` data is rejected
  I7  disconnected agents eventually lose leases
  I8  conflicting artifact versions never silently overwrite  (contract; M2)
  I9  provider/runtime identity does not determine capabilities
"""

from __future__ import annotations

import asyncio

import pytest

from app.core import eventlog, messages, presence, privacy, projections, store, tasks, work
from app.core.errors import (
    CapabilityUnsupported,
    LeaseConflict,
    PrivacyViolation,
    StaleFence,
)
from app.db import database as db
from app.domain.capabilities import (
    ATTENDED_MAX_LEASE_SECONDS,
    Capability,
    CapabilityProfile,
    DeliveryMode,
    HostClass,
    derive_runtime_policy,
)
from app.domain.commands import (
    ClaimTaskCommand,
    CompleteTaskCommand,
    CreateTaskCommand,
    DeclareWorkCommand,
    PostMessageCommand,
    RenewClaimCommand,
    UpdateTaskCommand,
)
from app.domain.disclosure import Audience, Disclosure
from app.domain.room import (
    ParticipantRole,
    PrivacyClass,
    RoomPolicy,
    RoomVisibility,
    Scope,
)
from app.domain.task import ConflictKind, TaskStatus
from app.util import iso_in

from .conftest import ATTENDED_CAPABILITIES, FULL_CAPABILITIES

pytestmark = pytest.mark.asyncio


async def _open_task(member, *, title="Ship the thing", targets=None) -> str:
    task = await tasks.create(
        participant=member.participant,
        command=CreateTaskCommand(title=title, targets=targets or ["src/api.py"]),
    )
    return task.id


# ===========================================================================
# I1 — one active exclusive lease per task
# ===========================================================================


async def test_i1_only_one_participant_can_hold_a_lease(make_room, join):
    room = await make_room()
    alice = await join(room, display_name="Alice")
    bob = await join(room, display_name="Bob")
    task_id = await _open_task(alice)

    claimed = await tasks.claim(
        participant=alice.participant, command=ClaimTaskCommand(task_id=task_id)
    )
    assert claimed.claim is not None
    assert claimed.claim.participant_id == alice.participant.id

    with pytest.raises(LeaseConflict) as exc:
        await tasks.claim(participant=bob.participant, command=ClaimTaskCommand(task_id=task_id))
    # The error must name the holder: a coordinating agent needs to know who to ask.
    assert exc.value.details["held_by_participant_id"] == alice.participant.id

    after = await store.load_task(task_id)
    assert after.claim is not None and after.claim.participant_id == alice.participant.id


async def test_i1_concurrent_claims_yield_exactly_one_winner(make_room, join):
    """The real race, run concurrently rather than sequentially.

    Correctness here comes from the conditional UPDATE, not from a lock, so this must
    hold without any serialization the domain arranged for itself.
    """
    room = await make_room()
    contenders = [await join(room, display_name=f"Agent{i}") for i in range(4)]
    task_id = await _open_task(contenders[0])

    results = await asyncio.gather(
        *(
            tasks.claim(participant=m.participant, command=ClaimTaskCommand(task_id=task_id))
            for m in contenders
        ),
        return_exceptions=True,
    )

    winners = [r for r in results if not isinstance(r, BaseException)]
    losers = [r for r in results if isinstance(r, LeaseConflict)]
    unexpected = [
        r for r in results if isinstance(r, BaseException) and not isinstance(r, LeaseConflict)
    ]

    assert unexpected == [], f"unexpected failures: {unexpected}"
    assert len(winners) == 1, f"expected exactly one winner, got {len(winners)}"
    assert len(losers) == len(contenders) - 1

    final = await store.load_task(task_id)
    assert final.claim is not None
    assert final.claim.participant_id == winners[0].claim.participant_id


async def test_i1_lost_race_is_recorded_as_a_conflict(make_room, join):
    """Losing a race is information the room needs, not just an error for the loser."""
    room = await make_room()
    alice = await join(room, display_name="Alice")
    bob = await join(room, display_name="Bob")
    task_id = await _open_task(alice)

    await tasks.claim(participant=alice.participant, command=ClaimTaskCommand(task_id=task_id))
    with pytest.raises(LeaseConflict):
        await tasks.claim(participant=bob.participant, command=ClaimTaskCommand(task_id=task_id))

    conflicts = await store.list_conflicts(room.room.id)
    races = [c for c in conflicts if c.kind == ConflictKind.CLAIM_RACE]
    assert len(races) == 1
    assert task_id in races[0].subject_refs
    assert set(races[0].participant_ids) == {alice.participant.id, bob.participant.id}


async def test_i1_expired_lease_is_immediately_reclaimable_without_the_reaper(make_room, join):
    """Expiry is enforced on read, so a late reaper cannot park work."""
    room = await make_room()
    alice = await join(room, display_name="Alice")
    bob = await join(room, display_name="Bob")
    task_id = await _open_task(alice)

    await tasks.claim(participant=alice.participant, command=ClaimTaskCommand(task_id=task_id))
    # Force the lease into the past without running the reaper.
    await db.execute("UPDATE tasks SET claim_expires_at = ? WHERE id = ?", (iso_in(-60), task_id))

    assert (await store.load_task(task_id)).claim is None
    assert (await store.load_task(task_id)).status == TaskStatus.OPEN

    reclaimed = await tasks.claim(
        participant=bob.participant, command=ClaimTaskCommand(task_id=task_id)
    )
    assert reclaimed.claim is not None
    assert reclaimed.claim.participant_id == bob.participant.id


# ===========================================================================
# I2 — a stale fence can never mutate protected task state
# ===========================================================================


async def test_i2_stale_fence_cannot_mutate_after_takeover(make_room, join):
    """The zombie-writer case: a claimant that lost its lease and woke up later.

    Its lease id still *looks* valid to it. Only the fence stops it.
    """
    room = await make_room()
    alice = await join(room, display_name="Alice")
    bob = await join(room, display_name="Bob")
    task_id = await _open_task(alice)

    first = await tasks.claim(
        participant=alice.participant, command=ClaimTaskCommand(task_id=task_id)
    )
    alice_fence = first.claim.fence

    await db.execute("UPDATE tasks SET claim_expires_at = ? WHERE id = ?", (iso_in(-60), task_id))
    second = await tasks.claim(
        participant=bob.participant, command=ClaimTaskCommand(task_id=task_id)
    )
    assert second.claim.fence > alice_fence, "a reclaim must allocate a strictly greater fence"

    for command in (UpdateTaskCommand(task_id=task_id, fence=alice_fence, title="Alice was here"),):
        with pytest.raises(StaleFence):
            await tasks.update(participant=alice.participant, command=command)

    with pytest.raises(StaleFence):
        await tasks.complete(
            participant=alice.participant,
            command=CompleteTaskCommand(task_id=task_id, fence=alice_fence, result="done"),
        )

    # And nothing Alice attempted landed.
    final = await store.load_task(task_id)
    assert final.title != "Alice was here"
    assert final.status != TaskStatus.DONE
    assert final.claim.participant_id == bob.participant.id


async def test_i2_missing_fence_is_refused_on_a_held_task(make_room, join):
    room = await make_room()
    alice = await join(room, display_name="Alice")
    task_id = await _open_task(alice)
    await tasks.claim(participant=alice.participant, command=ClaimTaskCommand(task_id=task_id))

    with pytest.raises(StaleFence):
        await tasks.update(
            participant=alice.participant,
            command=UpdateTaskCommand(task_id=task_id, fence=None, title="sneaky"),
        )


async def test_i2_fence_is_never_reused_across_release_and_reclaim(make_room, join):
    """Release must not reset the counter, or an old fence would become valid again."""
    room = await make_room()
    alice = await join(room, display_name="Alice")
    task_id = await _open_task(alice)

    seen = []
    for _ in range(3):
        task = await tasks.claim(
            participant=alice.participant, command=ClaimTaskCommand(task_id=task_id)
        )
        seen.append(task.claim.fence)
        from app.domain.commands import ReleaseClaimCommand

        await tasks.release(
            participant=alice.participant,
            command=ReleaseClaimCommand(task_id=task_id, fence=task.claim.fence),
        )

    assert seen == sorted(set(seen)), f"fences must be strictly increasing, got {seen}"


async def test_i2_renewal_after_expiry_is_refused(make_room, join):
    """A lapsed lease cannot be quietly resurrected — that would reintroduce zombies."""
    room = await make_room()
    alice = await join(room, display_name="Alice")
    task_id = await _open_task(alice)
    claimed = await tasks.claim(
        participant=alice.participant, command=ClaimTaskCommand(task_id=task_id)
    )
    await db.execute("UPDATE tasks SET claim_expires_at = ? WHERE id = ?", (iso_in(-1), task_id))

    with pytest.raises(LeaseConflict):
        await tasks.renew(
            participant=alice.participant,
            command=RenewClaimCommand(task_id=task_id, fence=claimed.claim.fence),
        )


# ===========================================================================
# I3 — a duplicate command_id cannot execute twice
# ===========================================================================


async def test_i3_duplicate_command_id_does_not_execute_twice(make_room, join):
    room = await make_room()
    alice = await join(room, display_name="Alice")

    command = CreateTaskCommand(command_id="cmd-fixed-1", title="Only once", targets=["a.py"])
    first = await tasks.create(participant=alice.participant, command=command)
    second = await tasks.create(participant=alice.participant, command=command)

    rows = await db.fetch_all(
        "SELECT id FROM tasks WHERE room_id = ? AND title = 'Only once'", (room.room.id,)
    )
    assert len(rows) == 1, "a replayed command_id must not create a second task"
    assert first.id == second.id


async def test_i3_duplicate_command_id_appends_no_second_event(make_room, join):
    room = await make_room()
    alice = await join(room, display_name="Alice")

    before = await eventlog.current_seq(room.room.id)
    command = PostMessageCommand(command_id="cmd-msg-1", body="hello room")
    await messages.post(participant=alice.participant, command=command)
    after_first = await eventlog.current_seq(room.room.id)
    await messages.post(participant=alice.participant, command=command)
    after_second = await eventlog.current_seq(room.room.id)

    assert after_first > before
    assert after_second == after_first, "replay must append no new event"

    rows = await db.fetch_all(
        "SELECT id FROM messages WHERE room_id = ? AND body = 'hello room'", (room.room.id,)
    )
    assert len(rows) == 1


async def test_i3_concurrent_duplicate_command_ids_execute_once(make_room, join):
    """The UNIQUE reservation, not a check-then-act read, must be the arbiter."""
    room = await make_room()
    alice = await join(room, display_name="Alice")
    command = CreateTaskCommand(command_id="cmd-race-1", title="Race once", targets=["b.py"])

    results = await asyncio.gather(
        *(tasks.create(participant=alice.participant, command=command) for _ in range(5)),
        return_exceptions=True,
    )
    failures = [r for r in results if isinstance(r, BaseException)]
    assert failures == [], f"idempotent replay should not error: {failures}"

    rows = await db.fetch_all(
        "SELECT id FROM tasks WHERE room_id = ? AND title = 'Race once'", (room.room.id,)
    )
    assert len(rows) == 1


# ===========================================================================
# I4 — reconnect from seq=N cannot silently miss authorized events
# ===========================================================================


async def test_i4_replay_from_cursor_is_gapless_and_ordered(make_room, join):
    room = await make_room()
    alice = await join(room, display_name="Alice")

    cursor = await eventlog.current_seq(room.room.id)
    for i in range(12):
        await messages.post(
            participant=alice.participant, command=PostMessageCommand(body=f"msg {i}")
        )

    events = await eventlog.read_since(room.room.id, cursor)
    seqs = [e.seq for e in events]
    assert seqs == sorted(seqs), "events must be returned in seq order"
    assert seqs == list(range(cursor + 1, cursor + 1 + len(seqs))), "seq must be gapless"


async def test_i4_reconnect_sees_every_authorized_event_it_missed(make_room, join):
    """The union of what a participant received before and after a reconnect must be
    everything it was authorized to see — no gap at the boundary."""
    room = await make_room()
    alice = await join(room, display_name="Alice")
    bob = await join(room, display_name="Bob")

    # Bob snapshots, then goes away while things happen.
    snapshot = await projections.snapshot(room_id=room.room.id, recipient=bob.participant)
    cursor = snapshot["snapshot_seq"]

    await messages.post(participant=alice.participant, command=PostMessageCommand(body="one"))
    task_id = await _open_task(alice, title="While Bob was away")
    await tasks.claim(participant=alice.participant, command=ClaimTaskCommand(task_id=task_id))
    await messages.post(participant=alice.participant, command=PostMessageCommand(body="two"))

    replayed = await projections.visible_events_since(
        room_id=room.room.id, recipient=bob.participant, since_seq=cursor
    )
    types = [e["type"] for e in replayed]
    assert "message.posted" in types
    assert "task.created" in types
    assert "task.claimed" in types

    bodies = [e["payload"].get("body") for e in replayed if e["type"] == "message.posted"]
    assert bodies == ["one", "two"], "both messages must survive the reconnect boundary"

    # And the replay is contiguous with the snapshot: nothing at or below the cursor.
    assert all(e["seq"] > cursor for e in replayed)


async def test_i4_snapshot_seq_is_consistent_with_snapshot_content(make_room, join):
    """`snapshot_seq` must be read in the same transaction as the content, or an
    event landing in between would be missed or double-applied."""
    room = await make_room()
    alice = await join(room, display_name="Alice")
    task_id = await _open_task(alice, title="Visible in snapshot")

    snapshot = await projections.snapshot(room_id=room.room.id, recipient=alice.participant)
    titles = [t["title"] for t in snapshot["tasks"]]
    assert "Visible in snapshot" in titles

    # Every event that produced the snapshot content is at or below snapshot_seq.
    events = await eventlog.read_since(room.room.id, 0)
    creating = next(e for e in events if e.payload.get("task_id") == task_id)
    assert creating.seq <= snapshot["snapshot_seq"]


async def test_i4_cursor_ahead_of_room_is_a_client_error_not_a_silent_empty(make_room, join):
    from app.core.errors import InvalidCursor

    room = await make_room()
    await join(room, display_name="Alice")
    current = await eventlog.current_seq(room.room.id)

    with pytest.raises(InvalidCursor):
        await eventlog.validate_cursor(room.room.id, current + 50)


async def test_i4_truncated_history_reports_resume_gap(make_room, join):
    """Truncation is not implemented yet, but the signal must already work — a client
    that cannot tell it lost history would coordinate on stale state."""
    from app.core.errors import ResumeGap

    room = await make_room()
    alice = await join(room, display_name="Alice")
    for i in range(5):
        await messages.post(participant=alice.participant, command=PostMessageCommand(body=f"m{i}"))
    await db.execute("UPDATE rooms SET retained_from_seq = ? WHERE id = ?", (4, room.room.id))

    with pytest.raises(ResumeGap):
        await eventlog.validate_cursor(room.room.id, 1)


# ===========================================================================
# I5 — unauthorized privacy classes never fan out
# ===========================================================================


async def test_i5_directed_message_is_not_visible_to_a_third_participant(make_room, join):
    room = await make_room()
    alice = await join(room, display_name="Alice")
    bob = await join(room, display_name="Bob")
    carol = await join(room, display_name="Carol", role=ParticipantRole.COLLABORATOR)

    cursor = await eventlog.current_seq(room.room.id)
    await messages.post(
        participant=alice.participant,
        command=PostMessageCommand(
            body="just between us",
            disclosure=Disclosure(
                audience=Audience.PARTICIPANT, to_participant_id=bob.participant.id
            ),
        ),
    )

    for member, should_see in ((alice, True), (bob, True), (carol, False)):
        visible = await projections.visible_events_since(
            room_id=room.room.id, recipient=member.participant, since_seq=cursor
        )
        bodies = [e["payload"].get("body") for e in visible]
        assert ("just between us" in bodies) is should_see, (
            f"{member.participant.identity.display_name} visibility wrong"
        )


async def test_i5_participant_private_event_stays_with_its_author(make_room, join):
    room = await make_room()
    alice = await join(room, display_name="Alice")
    bob = await join(room, display_name="Bob", role=ParticipantRole.COLLABORATOR)

    cursor = await eventlog.current_seq(room.room.id)
    await work.declare(
        participant=alice.participant,
        command=DeclareWorkCommand(
            headline="private note to self",
            targets=["notes.md"],
            disclosure=Disclosure(privacy_class=PrivacyClass.PARTICIPANT_PRIVATE),
        ),
    )

    alice_sees = await projections.visible_events_since(
        room_id=room.room.id, recipient=alice.participant, since_seq=cursor
    )
    bob_sees = await projections.visible_events_since(
        room_id=room.room.id, recipient=bob.participant, since_seq=cursor
    )
    assert any(e["type"] == "work.declared" for e in alice_sees)
    assert not any(e["type"] == "work.declared" for e in bob_sees)


async def test_i5_private_records_are_filtered_from_the_snapshot_too(make_room, join):
    """Fanout filtering alone is not enough: the snapshot is a second read path, and a
    filter applied in one place and forgotten in the other is the usual leak."""
    room = await make_room()
    alice = await join(room, display_name="Alice")
    bob = await join(room, display_name="Bob")

    await work.declare(
        participant=alice.participant,
        command=DeclareWorkCommand(
            headline="alice private work",
            targets=["secret.md"],
            disclosure=Disclosure(privacy_class=PrivacyClass.PARTICIPANT_PRIVATE),
        ),
    )
    await messages.post(
        participant=alice.participant,
        command=PostMessageCommand(
            body="dm for nobody else",
            disclosure=Disclosure(
                audience=Audience.PARTICIPANT, to_participant_id=alice.participant.id
            ),
        ),
    )

    bob_snapshot = await projections.snapshot(room_id=room.room.id, recipient=bob.participant)
    assert all(w["headline"] != "alice private work" for w in bob_snapshot["work"])
    assert all(m["body"] != "dm for nobody else" for m in bob_snapshot["messages"])

    alice_snapshot = await projections.snapshot(room_id=room.room.id, recipient=alice.participant)
    assert any(w["headline"] == "alice private work" for w in alice_snapshot["work"])


async def test_i5_org_internal_is_invisible_to_a_foreign_org_participant(make_room, join):
    room = await make_room(visibility=RoomVisibility.CROSS_ORG)
    alice = await join(room, display_name="Alice")

    from app.core import rooms as room_service

    other_org_id, other_user_id = await room_service.ensure_org_and_user(
        org_name="Beta Corp", org_slug="beta", email="b@beta.test", display_name="Beta"
    )
    outsider = await join(room, display_name="Outsider", org_id=other_org_id)

    cursor = await eventlog.current_seq(room.room.id)
    # Post an org_internal event directly, bypassing the write gate — this test is
    # about the *read* gate, and §6 must hold independently of §2.
    async with db.transaction() as tx:
        await eventlog.append(
            tx,
            room_id=room.room.id,
            type_=__import__("app.domain.events", fromlist=["EventType"]).EventType.MESSAGE_POSTED,
            actor=__import__("app.core.actors", fromlist=["actor_for"]).actor_for(
                alice.participant
            ),
            payload={"body": "internal only"},
            disclosure=__import__(
                "app.domain.disclosure", fromlist=["DisclosureDecision"]
            ).DisclosureDecision(privacy_class=PrivacyClass.ORG_INTERNAL, audience=Audience.ORG),
        )

    insider_sees = await projections.visible_events_since(
        room_id=room.room.id, recipient=alice.participant, since_seq=cursor
    )
    outsider_sees = await projections.visible_events_since(
        room_id=room.room.id, recipient=outsider.participant, since_seq=cursor
    )
    assert any(e["payload"].get("body") == "internal only" for e in insider_sees)
    assert not any(e["payload"].get("body") == "internal only" for e in outsider_sees)


# ===========================================================================
# I6 — cross-org `org_internal` data is rejected
# ===========================================================================


async def test_i6_org_internal_write_into_cross_org_room_is_rejected(make_room, join):
    """Rejected, never downgraded: a downgrade performs the disclosure it prevents."""
    room = await make_room(visibility=RoomVisibility.CROSS_ORG)
    alice = await join(room, display_name="Alice")

    with pytest.raises(PrivacyViolation) as exc:
        await messages.post(
            participant=alice.participant,
            command=PostMessageCommand(
                body="internal roadmap",
                disclosure=Disclosure(privacy_class=PrivacyClass.ORG_INTERNAL),
            ),
        )
    assert exc.value.details["room_visibility"] == "cross_org"

    rows = await db.fetch_all(
        "SELECT id, privacy_class FROM messages WHERE room_id = ?", (room.room.id,)
    )
    assert rows == [], "nothing may be stored, at any class, when the check fails"


async def test_i6_foreign_org_participant_cannot_assert_org_internal(make_room, join):
    room = await make_room(visibility=RoomVisibility.INTERNAL)
    from app.core import rooms as room_service

    other_org_id, _ = await room_service.ensure_org_and_user(
        org_name="Gamma", org_slug="gamma", email="g@gamma.test", display_name="G"
    )
    # An internal room refuses a foreign identity outright, which is the stronger
    # guarantee; assert that first.
    from app.core.errors import Forbidden

    with pytest.raises(Forbidden):
        await join(room, display_name="Outsider", org_id=other_org_id)


async def test_i6_untrusted_participant_is_confined_to_room_public(make_room, join):
    from app.domain.identity import TrustTier

    room = await make_room(visibility=RoomVisibility.CROSS_ORG)
    from app.core import rooms as room_service

    other_org_id, _ = await room_service.ensure_org_and_user(
        org_name="Delta", org_slug="delta", email="d@delta.test", display_name="D"
    )
    outsider = await join(
        room, display_name="Untrusted", org_id=other_org_id, trust=TrustTier.UNTRUSTED
    )

    with pytest.raises(PrivacyViolation):
        await messages.post(
            participant=outsider.participant,
            command=PostMessageCommand(
                body="anything",
                disclosure=Disclosure(privacy_class=PrivacyClass.PARTICIPANT_PRIVATE),
            ),
        )


async def test_i6_untrusted_participant_cannot_hold_scopes_that_write_state(make_room, join):
    from app.domain.identity import TrustTier

    room = await make_room(visibility=RoomVisibility.CROSS_ORG)
    from app.core import rooms as room_service

    other_org_id, _ = await room_service.ensure_org_and_user(
        org_name="Eps", org_slug="eps", email="e@eps.test", display_name="E"
    )
    outsider = await join(
        room,
        display_name="Untrusted",
        role=ParticipantRole.COLLABORATOR,
        org_id=other_org_id,
        trust=TrustTier.UNTRUSTED,
    )
    scopes = set(outsider.participant.scopes)
    assert Scope.TASK_CLAIM not in scopes
    assert Scope.STATE_WRITE not in scopes
    assert Scope.ARTIFACT_WRITE not in scopes
    assert Scope.ROOM_ADMIN not in scopes
    # It can still participate in coordination — untrusted is not muted.
    assert Scope.MESSAGE_POST in scopes


# ===========================================================================
# I7 — disconnected agents eventually lose leases
# ===========================================================================


async def test_i7_reaper_expires_a_lease_and_reopens_the_task(make_room, join):
    room = await make_room()
    alice = await join(room, display_name="Alice")
    task_id = await _open_task(alice)
    claimed = await tasks.claim(
        participant=alice.participant, command=ClaimTaskCommand(task_id=task_id)
    )
    await db.execute("UPDATE tasks SET claim_expires_at = ? WHERE id = ?", (iso_in(-5), task_id))

    events = await tasks.reap_expired_leases()
    assert any(e.type.value == "task.claim_expired" for e in events)

    reopened = await store.load_task(task_id)
    assert reopened.status == TaskStatus.OPEN
    assert reopened.claim is None
    # The event must explain *why* the claim vanished, or the room cannot tell a
    # crash from a deliberate release.
    expired = next(e for e in events if e.type.value == "task.claim_expired")
    assert expired.payload["reason"] == "lease_expired"
    assert expired.payload["fence"] == claimed.claim.fence
    assert expired.actor.participant_id is None, "the room expired it, not a participant"


async def test_i7_reaper_is_idempotent(make_room, join):
    room = await make_room()
    alice = await join(room, display_name="Alice")
    task_id = await _open_task(alice)
    await tasks.claim(participant=alice.participant, command=ClaimTaskCommand(task_id=task_id))
    await db.execute("UPDATE tasks SET claim_expires_at = ? WHERE id = ?", (iso_in(-5), task_id))

    first = await tasks.reap_expired_leases()
    second = await tasks.reap_expired_leases()
    assert len(first) == 1
    assert second == [], "a second pass must not emit a duplicate expiry event"


async def test_i7_dead_connection_reaper_releases_claims_and_ends_work(make_room, join):
    """The ungraceful case: nobody said goodbye, the heartbeat just stopped."""
    room = await make_room()
    alice = await join(room, display_name="Alice")
    task_id = await _open_task(alice)
    await tasks.claim(participant=alice.participant, command=ClaimTaskCommand(task_id=task_id))
    declaration = await work.declare(
        participant=alice.participant,
        command=DeclareWorkCommand(headline="mid-flight", targets=["src/api.py"]),
    )

    # Age the heartbeat well past stale, as a crashed process would.
    await db.execute(
        "UPDATE connections SET last_heartbeat_at = ? WHERE participant_id = ?",
        (iso_in(-3600), alice.participant.id),
    )

    events = await presence.reap_dead_connections()
    types = {e.type.value for e in events}
    assert "task.claim_released" in types
    assert "work.ended" in types
    assert "presence.changed" in types

    assert (await store.load_task(task_id)).claim is None
    assert (await store.load_task(task_id)).status == TaskStatus.OPEN
    ended = await store.load_work(declaration.id)
    assert ended.ended_at is not None
    assert ended.end_reason.value == "presence_lost"


async def test_i7_graceful_leave_releases_immediately(make_room, join):
    from app.core import rooms as room_service
    from app.domain.commands import LeaveRoomCommand

    room = await make_room()
    alice = await join(room, display_name="Alice")
    task_id = await _open_task(alice)
    await tasks.claim(participant=alice.participant, command=ClaimTaskCommand(task_id=task_id))

    await room_service.leave_room(
        participant=alice.participant, command=LeaveRoomCommand(note="done for today")
    )

    assert (await store.load_task(task_id)).claim is None
    left = await store.load_participant(alice.participant.id)
    assert left.state.value == "left"
    # The token is revoked on leave, so it cannot be replayed.
    rows = await db.fetch_all(
        "SELECT token_hash FROM participants WHERE id = ?", (alice.participant.id,)
    )
    assert rows[0]["token_hash"] is None


async def test_i7_participant_with_no_connection_cannot_claim(make_room, join):
    """An unreachable participant must not be handed exclusive work."""
    room = await make_room()
    alice = await join(room, display_name="Alice")
    bob = await join(room, display_name="Bob", connect=False)
    task_id = await _open_task(alice)

    with pytest.raises(CapabilityUnsupported):
        await tasks.claim(participant=bob.participant, command=ClaimTaskCommand(task_id=task_id))


# ===========================================================================
# I8 — conflicting artifact versions never silently overwrite
# ===========================================================================


async def test_i8_artifact_divergence_contract_is_specified_and_not_yet_implemented():
    """Artifacts land in M2. This test pins the *contract* so the implementation
    cannot arrive with silent-overwrite semantics.

    It asserts the error code, event type, and conflict kind already exist, which is
    what a divergent publish must produce. When `core/artifacts.py` lands, this test
    is replaced by a behavioral one (see docs/ROADMAP.md M2).
    """
    from app.core.errors import ArtifactDivergence
    from app.domain.events import EventType

    assert ArtifactDivergence.code == "artifact_divergence"
    assert EventType.ARTIFACT_DIVERGENCE_DETECTED.value == "artifact.divergence_detected"
    assert EventType.ARTIFACT_VERSION_PUBLISHED.value == "artifact.version_published"
    assert ConflictKind.ARTIFACT_DIVERGENCE.value == "artifact_divergence"

    import app.core as core_pkg

    assert not hasattr(core_pkg, "artifacts"), (
        "core/artifacts.py has landed — replace this contract test with a behavioral "
        "one that proves a divergent publish is accepted, does not move head, and "
        "raises artifact.divergence_detected"
    )


# ===========================================================================
# I9 — provider/runtime identity does not determine capabilities
# ===========================================================================


def test_i9_runtime_policy_derivation_takes_no_host_class():
    """Structural guarantee: the derivation function cannot branch on a label
    because it is not given one."""
    import inspect

    params = set(inspect.signature(derive_runtime_policy).parameters)
    assert "host_class" not in params
    assert "provider" not in params
    assert "profile" in params


@pytest.mark.parametrize(
    "host_class",
    [
        HostClass.INTERACTIVE_CLIENT,
        HostClass.PERSISTENT_LOCAL,
        HostClass.NATIVE_REMOTE_A2A,
        HostClass.BROWSER_HUMAN,
        HostClass.UNKNOWN,
    ],
)
def test_i9_identical_capabilities_yield_identical_policy_across_labels(host_class):
    """Same declaration, different label → identical behavior. This is the invariant
    that stops "ChatGPT cannot be woken" from being baked in as architecture."""
    profile = CapabilityProfile.from_capabilities(FULL_CAPABILITIES)
    policy = derive_runtime_policy(
        profile,
        default_lease_seconds=900,
        max_lease_seconds=3600,
        allow_attended_claims=False,
        heartbeat_interval_s=20,
    )
    assert policy.delivery_mode == DeliveryMode.PUSH
    assert policy.may_claim is True
    assert policy.max_lease_seconds == 900
    assert policy.lease_renewable_unattended is True
    del host_class  # the label is deliberately unused


def test_i9_an_interactive_label_that_declares_push_gets_push():
    """If a vendor ships a push channel tomorrow, no code change is needed."""
    profile = CapabilityProfile.from_capabilities(
        [
            Capability.CAN_RECEIVE_EVENTS,
            Capability.SUPPORTS_PUSH,
            Capability.CAN_INITIATE_FOLLOWUP,
            Capability.CAN_EXECUTE_BACKGROUND,
            Capability.SUPPORTS_TOOLS,
        ]
    )
    policy = derive_runtime_policy(
        profile,
        default_lease_seconds=900,
        max_lease_seconds=3600,
        allow_attended_claims=False,
        heartbeat_interval_s=20,
    )
    assert policy.delivery_mode == DeliveryMode.PUSH
    assert policy.may_claim is True


def test_i9_a_persistent_local_label_that_declares_attended_gets_short_leases():
    """And the reverse: a "persistent" label buys nothing without the capability."""
    profile = CapabilityProfile.from_capabilities(ATTENDED_CAPABILITIES)
    policy = derive_runtime_policy(
        profile,
        default_lease_seconds=900,
        max_lease_seconds=3600,
        allow_attended_claims=True,
        heartbeat_interval_s=20,
    )
    assert policy.delivery_mode == DeliveryMode.LONG_POLL
    assert policy.lease_renewable_unattended is False
    assert policy.max_lease_seconds == ATTENDED_MAX_LEASE_SECONDS
    assert policy.may_claim is True


def test_i9_attended_claims_are_refused_when_the_room_says_so():
    profile = CapabilityProfile.from_capabilities(ATTENDED_CAPABILITIES)
    policy = derive_runtime_policy(
        profile,
        default_lease_seconds=900,
        max_lease_seconds=3600,
        allow_attended_claims=False,
        heartbeat_interval_s=20,
    )
    assert policy.may_claim is False
    assert policy.claim_denied_reason and "human presence" in policy.claim_denied_reason


async def test_i9_negotiation_intersects_declaration_with_transport_reality(make_room):
    """A client cannot talk itself into a capability the wire cannot provide."""
    room = await make_room()
    profile, policy = presence.negotiate(
        declared=[Capability.SUPPORTS_PUSH, Capability.CAN_RECEIVE_EVENTS],
        host_class=HostClass.PERSISTENT_LOCAL,
        transport="long_poll",
        room=room.room,
    )
    assert profile.supports_push is False, "long-poll transport cannot honor push"
    assert (
        policy.delivery_mode == DeliveryMode.NONE
        or policy.delivery_mode == DeliveryMode.ATTENDED_PULL
    )


async def test_i9_lease_length_follows_capabilities_end_to_end(make_room, join):
    """The whole path: declaration → negotiation → actual lease granted."""
    room = await make_room(policy=RoomPolicy(allow_attended_claims=True))
    attended = await join(
        room,
        display_name="Attended agent",
        # A label that suggests full autonomy...
        host_class=HostClass.NATIVE_REMOTE_A2A,
        # ...but a declaration that says otherwise. The declaration must win.
        capabilities=ATTENDED_CAPABILITIES,
        transport="long_poll",
    )
    task_id = await _open_task(attended)
    claimed = await tasks.claim(
        participant=attended.participant, command=ClaimTaskCommand(task_id=task_id)
    )
    from app.util import seconds_until

    remaining = seconds_until(claimed.claim.expires_at)
    assert remaining <= ATTENDED_MAX_LEASE_SECONDS + 5, (
        f"an attended participant must get a short lease, got {remaining}s"
    )
