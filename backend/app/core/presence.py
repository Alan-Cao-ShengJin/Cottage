"""Connections, capability negotiation, and derived presence.

The rule this module enforces: **behavior comes from negotiated capabilities, never
from a provider label.** `negotiate` intersects what the client declared with what
the chosen transport can actually honor, then `derive_runtime_policy` turns that
into delivery mode, lease eligibility, and lease ceiling. `HostClass` is recorded
for display and used only to supply defaults when a client declares nothing.

The practical consequence: an "interactive client" that declares `supports_push`
and `can_initiate_followup` gets pushed to and may hold a full-length lease, while a
"persistent local" agent that declares neither does not. Vendors ship features; the
derivation must not need editing when they do.

Presence itself is derived from open connections and heartbeat age (`docs/PROTOCOL.md`
§3), never stored as a flag — a stored flag is wrong the instant a process dies
without saying goodbye.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from ..config import settings
from ..db import database as db
from ..domain import ids
from ..domain.capabilities import (
    SUGGESTED_CAPABILITIES,
    Capability,
    CapabilityProfile,
    DeliveryMode,
    HostClass,
    RuntimePolicy,
    derive_runtime_policy,
)
from ..domain.commands import ConnectCommand
from ..domain.events import EventActor, EventEnvelope, EventType
from ..domain.room import (
    DELIVERY_MODE_LIVENESS,
    IDLE_AFTER_INTERVALS,
    LIVENESS_RANK,
    STALE_AFTER_INTERVALS,
    Connection,
    LeaveReason,
    Liveness,
    Participant,
    PresenceView,
    Room,
)
from ..util import from_iso, is_past, utcnow, utcnow_iso
from . import authz, eventlog, store
from .dispatch import CommandOutcome, execute_command, publish_committed
from .errors import CapabilityUnsupported, RateLimited

log = logging.getLogger(__name__)


#: What each transport can genuinely deliver. A client claiming `supports_push` over
#: a long-poll connection does not get push — negotiation is an intersection, so a
#: client cannot talk itself into a capability the wire cannot provide.
TRANSPORT_CAPABILITIES: dict[str, frozenset[Capability]] = {
    "sse": frozenset(
        {
            Capability.SUPPORTS_PUSH,
            Capability.CAN_RECEIVE_EVENTS,
            Capability.SUPPORTS_RESUME,
            Capability.CAN_INITIATE_FOLLOWUP,
            Capability.CAN_EXECUTE_BACKGROUND,
            Capability.REQUIRES_HUMAN_PRESENCE,
            Capability.SUPPORTS_TOOLS,
            Capability.SUPPORTS_ARTIFACTS,
        }
    ),
    "long_poll": frozenset(
        {
            Capability.SUPPORTS_POLL,
            Capability.CAN_RECEIVE_EVENTS,
            Capability.SUPPORTS_RESUME,
            Capability.CAN_INITIATE_FOLLOWUP,
            Capability.CAN_EXECUTE_BACKGROUND,
            Capability.REQUIRES_HUMAN_PRESENCE,
            Capability.SUPPORTS_TOOLS,
            Capability.SUPPORTS_ARTIFACTS,
        }
    ),
    "a2a_webhook": frozenset(
        {
            Capability.SUPPORTS_PUSH,
            Capability.CAN_RECEIVE_EVENTS,
            Capability.SUPPORTS_RESUME,
            Capability.CAN_INITIATE_FOLLOWUP,
            Capability.CAN_EXECUTE_BACKGROUND,
            Capability.SUPPORTS_TOOLS,
            Capability.SUPPORTS_ARTIFACTS,
        }
    ),
}


@dataclass
class NegotiatedConnection:
    connection: Connection
    runtime: RuntimePolicy
    #: Room seq at the moment of connect, so the client knows where it starts.
    current_seq: int
    since_seq: int


def negotiate(
    *,
    declared: list[Capability] | None,
    host_class: HostClass,
    transport: str,
    room: Room,
) -> tuple[CapabilityProfile, RuntimePolicy]:
    """Intersect declared capabilities with transport reality, then derive policy.

    Unknown declared capabilities are dropped rather than rejected: a newer client
    talking to an older server should degrade, not fail (`docs/PROTOCOL.md` §3).
    """
    wanted = set(declared) if declared is not None else set(SUGGESTED_CAPABILITIES[host_class])
    supported = TRANSPORT_CAPABILITIES.get(transport, frozenset())
    profile = CapabilityProfile.from_capabilities(wanted & supported)

    runtime = derive_runtime_policy(
        profile,
        default_lease_seconds=room.policy.default_lease_seconds,
        max_lease_seconds=room.policy.max_lease_seconds,
        allow_attended_claims=room.policy.allow_attended_claims,
        heartbeat_interval_s=room.policy.heartbeat_interval_s
        or settings.heartbeat_interval_seconds,
    )
    return profile, runtime


async def connect(
    *, participant: Participant, command: ConnectCommand, transport: str
) -> NegotiatedConnection:
    """Open a connection. Reads survive a closed room, so this does not require
    writability — a participant may still attach to a closed room to read history."""
    room = await store.load_room(participant.room_id)
    authz.require_active(participant)

    open_count = int(
        await db.fetch_value(
            "SELECT COUNT(*) FROM connections WHERE participant_id = ? AND closed_at IS NULL",
            (participant.id,),
        )
        or 0
    )
    if open_count >= settings.max_connections_per_participant:
        raise RateLimited(
            "Too many open connections for this participant.",
            limit=settings.max_connections_per_participant,
        )

    profile, runtime = negotiate(
        declared=command.capabilities,
        host_class=command.host_class,
        transport=transport,
        room=room,
    )
    connection_id = ids.new_id(ids.CONNECTION)
    now = utcnow_iso()

    async def body(tx: db.Tx) -> CommandOutcome:
        before = await _grade_participant(tx, participant.id, room)
        await tx.execute(
            """
            INSERT INTO connections (
                id, room_id, participant_id, host_class, profile, delivery_mode,
                heartbeat_interval_s, opened_at, last_heartbeat_at, last_delivered_seq
            ) VALUES (?,?,?,?,?,?,?,?,?,?)
            """,
            (
                connection_id,
                room.id,
                participant.id,
                command.host_class.value,
                db.dumps(profile.model_dump()),
                runtime.delivery_mode.value,
                runtime.heartbeat_interval_s,
                now,
                now,
                command.since_seq,
            ),
        )
        after = await _grade_participant(tx, participant.id, room)
        events: list[EventEnvelope] = []
        if after != before:
            events.append(
                await _append_presence_changed(
                    tx, room=room, participant=participant, liveness=after, runtime=runtime
                )
            )
        return CommandOutcome(result={"connection_id": connection_id}, events=events)

    await execute_command(
        command_id=command.command_id,
        command_type="presence.connect",
        room_id=room.id,
        participant_id=participant.id,
        body=body,
    )

    return NegotiatedConnection(
        connection=await store.load_connection(connection_id),
        runtime=runtime,
        current_seq=await eventlog.current_seq(room.id),
        since_seq=command.since_seq,
    )


async def heartbeat(
    *, connection_id: str, participant: Participant, seq: int | None = None
) -> None:
    """Refresh a connection's liveness.

    Not written to the event log: heartbeats at 20s x N participants would swamp the
    log and drown the events that carry meaning. Only *grade transitions* are
    events (`docs/PROTOCOL.md` §3).
    """
    room = await store.load_room(participant.room_id)
    before = await _grade_participant(None, participant.id, room)

    await db.execute(
        """
        UPDATE connections
        SET last_heartbeat_at = ?, last_delivered_seq = MAX(last_delivered_seq, ?)
        WHERE id = ? AND participant_id = ? AND closed_at IS NULL
        """,
        (utcnow_iso(), seq or 0, connection_id, participant.id),
    )

    after = await _grade_participant(None, participant.id, room)
    if after != before:
        async with db.transaction() as tx:
            event = await _append_presence_changed(
                tx, room=room, participant=participant, liveness=after, runtime=None
            )
        await publish_committed([event])


async def disconnect(*, connection_id: str, participant: Participant) -> None:
    """Close one connection. Losing the *last* one ends work and releases claims."""
    room = await store.load_room(participant.room_id)

    async def body(tx: db.Tx) -> CommandOutcome:
        affected = await tx.execute(
            "UPDATE connections SET closed_at = ? WHERE id = ? AND participant_id = ? "
            "AND closed_at IS NULL",
            (utcnow_iso(), connection_id, participant.id),
        )
        if affected == 0:
            return CommandOutcome()

        events: list[EventEnvelope] = []
        liveness = await _grade_participant(tx, participant.id, room)
        if liveness == Liveness.DISCONNECTED:
            events += await _on_disconnected_tx(tx, participant=participant, room=room)
        events.append(
            await _append_presence_changed(
                tx, room=room, participant=participant, liveness=liveness, runtime=None
            )
        )
        return CommandOutcome(events=events)

    await execute_command(
        command_id=None,
        command_type="presence.disconnect",
        room_id=room.id,
        participant_id=participant.id,
        body=body,
    )


async def close_all_connections_tx(tx: db.Tx, *, participant: Participant) -> list[EventEnvelope]:
    """Used by graceful leave, inside the caller's transaction."""
    await tx.execute(
        "UPDATE connections SET closed_at = ? WHERE participant_id = ? AND closed_at IS NULL",
        (utcnow_iso(), participant.id),
    )
    return []


async def _on_disconnected_tx(
    tx: db.Tx, *, participant: Participant, room: Room
) -> list[EventEnvelope]:
    """A participant lost its last connection: release its holds.

    Doing this here rather than waiting for lease expiry is what makes a clean
    disconnect fast. The reaper remains the backstop for the ungraceful case, where
    nobody told us anything.
    """
    from . import tasks, work

    events = await tasks.release_all_claims_tx(tx, participant=participant, reason="presence_lost")
    events += await work.end_all_open_tx(tx, participant=participant, reason="presence_lost")
    return events


# ---------------------------------------------------------------------------
# Grading
# ---------------------------------------------------------------------------


def grade_connection(connection: Connection, *, now=None) -> Liveness:
    """Grade one connection from its delivery mechanism, attendedness, and heartbeat age.

    Three separate facts, deliberately not collapsed:

    * **Mechanism** (`delivery_mode`) — how we get bytes to it. A connector that can
      poll is genuinely poll-capable, and we do not downgrade the mechanism.
    * **Attendedness** (`requires_human_presence`) — whether it acts on *our* clock. A
      client that can poll but only when its human prompts it is **not** `live_poll`;
      grading it so would tell everyone else to expect prompt responses it cannot give.
      So this caps the grade at `attended`, regardless of mechanism.
    * **Heartbeat age** — dominates both. "We could push to it" says nothing about
      whether anyone is listening, so a silent pushable connection is `stale`.
    """
    if connection.closed_at:
        return Liveness.DISCONNECTED

    now = now or utcnow()
    age = (now - from_iso(connection.last_heartbeat_at)).total_seconds()
    interval = max(connection.heartbeat_interval_s, 1)

    if age > interval * STALE_AFTER_INTERVALS:
        return Liveness.STALE
    if age > interval * IDLE_AFTER_INTERVALS:
        return Liveness.IDLE

    grade = DELIVERY_MODE_LIVENESS.get(connection.delivery_mode, Liveness.IDLE)
    if connection.profile.requires_human_presence and (
        LIVENESS_RANK[grade] > LIVENESS_RANK[Liveness.ATTENDED]
    ):
        return Liveness.ATTENDED
    return grade


def grade(connections: list[Connection], *, now=None) -> Liveness:
    """Best connection wins. No connections at all is `disconnected`."""
    if not connections:
        return Liveness.DISCONNECTED
    grades = [grade_connection(c, now=now) for c in connections if not c.closed_at]
    if not grades:
        return Liveness.DISCONNECTED
    return max(grades, key=lambda g: LIVENESS_RANK[g])


async def _grade_participant(tx: db.Tx | None, participant_id: str, room: Room) -> Liveness:
    sql = "SELECT * FROM connections WHERE participant_id = ? AND closed_at IS NULL"
    rows = await (
        tx.fetch_all(sql, (participant_id,)) if tx else db.fetch_all(sql, (participant_id,))
    )
    return grade([store.to_connection(r) for r in rows])


async def _append_presence_changed(
    tx: db.Tx,
    *,
    room: Room,
    participant: Participant,
    liveness: Liveness,
    runtime: RuntimePolicy | None,
) -> EventEnvelope:
    rows = await tx.fetch_all(
        "SELECT * FROM connections WHERE participant_id = ? AND closed_at IS NULL",
        (participant.id,),
    )
    connections = [store.to_connection(r) for r in rows]
    payload: dict[str, Any] = {
        "participant_id": participant.id,
        "liveness": liveness.value,
        "connection_count": len(connections),
        "delivery_modes": sorted({c.delivery_mode.value for c in connections}),
        "negotiated_capabilities": sorted(
            {cap.value for c in connections for cap in c.negotiated_capabilities}
        ),
    }
    if runtime is not None:
        payload["runtime"] = runtime.model_dump(mode="json")
    return await eventlog.append(
        tx,
        room_id=room.id,
        type_=EventType.PRESENCE_CHANGED,
        actor=EventActor(
            participant_id=participant.id,
            display_name=participant.identity.display_name,
            kind=participant.identity.kind,
            org_id=participant.org_id,
        ),
        payload=payload,
    )


async def presence_for_room(room: Room) -> dict[str, PresenceView]:
    """Presence for every participant, derived fresh. Drives the presence rail."""
    connections = await store.list_open_connections(room.id)
    by_participant: dict[str, list[Connection]] = {}
    for conn in connections:
        by_participant.setdefault(conn.participant_id, []).append(conn)

    views: dict[str, PresenceView] = {}
    for participant in await store.list_participants(room.id):
        conns = by_participant.get(participant.id, [])
        liveness = grade(conns)
        runtime: RuntimePolicy | None = None
        if conns:
            # The best-graded connection is the one others should coordinate
            # against, so its runtime policy is the one to publish.
            best = max(conns, key=lambda c: LIVENESS_RANK[grade_connection(c)])
            _, runtime = negotiate(
                declared=best.negotiated_capabilities,
                host_class=best.host_class,
                transport=_transport_for(best.delivery_mode),
                room=room,
            )
        views[participant.id] = PresenceView(
            participant_id=participant.id,
            liveness=liveness,
            connection_count=len(conns),
            delivery_modes=sorted({c.delivery_mode for c in conns}, key=lambda m: m.value),
            negotiated_capabilities=sorted(
                {cap for c in conns for cap in c.negotiated_capabilities}, key=lambda c: c.value
            ),
            runtime=runtime,
            last_seen_at=max((c.last_heartbeat_at for c in conns), default=None),
        )
    return views


def _transport_for(mode: DeliveryMode) -> str:
    if mode == DeliveryMode.PUSH:
        return "sse"
    if mode == DeliveryMode.LONG_POLL:
        return "long_poll"
    return "sse"


async def runtime_policy_for(participant: Participant, room: Room) -> RuntimePolicy:
    """The policy in force for a participant right now.

    Derived from its *live* connections, so a participant that reattached with
    different capabilities is judged on what it can do now, not what it once
    claimed. With no live connection there is nothing to derive from, and claiming
    is refused — an unreachable participant cannot be handed exclusive work.
    """
    rows = await db.fetch_all(
        "SELECT * FROM connections WHERE participant_id = ? AND closed_at IS NULL",
        (participant.id,),
    )
    conns = [store.to_connection(r) for r in rows]
    if not conns:
        raise CapabilityUnsupported(
            "You have no open connection to this room, so no capabilities are "
            "negotiated. Connect before claiming work.",
            participant_id=participant.id,
        )
    best = max(conns, key=lambda c: LIVENESS_RANK[grade_connection(c)])
    _, runtime = negotiate(
        declared=best.negotiated_capabilities,
        host_class=best.host_class,
        transport=_transport_for(best.delivery_mode),
        room=room,
    )
    return runtime


# ---------------------------------------------------------------------------
# Reaper
# ---------------------------------------------------------------------------


async def reap_dead_connections() -> list[EventEnvelope]:
    """Close connections whose heartbeat lapsed well past stale, and cascade.

    The threshold is deliberately beyond `stale`: a stale connection is still shown
    (with a warning) because a slow agent is not a gone agent. Only once it is well
    past stale do we treat it as gone and release its holds — which is the backstop
    that makes "disconnected agents eventually lose leases" true even when nobody
    said goodbye.
    """
    now = utcnow()
    rows = await db.fetch_all(
        "SELECT * FROM connections WHERE closed_at IS NULL",
    )
    dead: dict[str, list[str]] = {}
    for row in rows:
        conn = store.to_connection(row)
        age = (now - from_iso(conn.last_heartbeat_at)).total_seconds()
        if age > conn.heartbeat_interval_s * (STALE_AFTER_INTERVALS + 1):
            dead.setdefault(conn.room_id, []).append(conn.id)

    events: list[EventEnvelope] = []
    for room_id, connection_ids in dead.items():
        room = await store.load_room(room_id)
        for connection_id in connection_ids:
            conn = await store.load_connection(connection_id)
            participant = await store.load_participant(conn.participant_id)
            async with db.transaction() as tx:
                affected = await tx.execute(
                    "UPDATE connections SET closed_at = ? WHERE id = ? AND closed_at IS NULL",
                    (utcnow_iso(), connection_id),
                )
                if affected == 0:
                    continue
                liveness = await _grade_participant(tx, participant.id, room)
                batch: list[EventEnvelope] = []
                if liveness == Liveness.DISCONNECTED:
                    batch += await _on_disconnected_tx(tx, participant=participant, room=room)
                batch.append(
                    await _append_presence_changed(
                        tx, room=room, participant=participant, liveness=liveness, runtime=None
                    )
                )
            events += batch
            log.info(
                "reaped connection %s for participant %s (%s)",
                connection_id,
                participant.id,
                LeaveReason.TIMEOUT.value,
            )

    await publish_committed(events)
    return events


def is_stale_heartbeat(connection: Connection, *, now=None) -> bool:
    return grade_connection(connection, now=now) in {Liveness.STALE, Liveness.DISCONNECTED}


def room_expired(room: Room) -> bool:
    return is_past(room.expires_at)
