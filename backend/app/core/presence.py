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
    Attachment,
    Connection,
    LeaveReason,
    Liveness,
    Participant,
    PresenceView,
    Room,
    RuntimeDeclaration,
    RuntimeRole,
    RuntimeView,
)
from ..util import from_iso, is_past, utcnow, utcnow_iso
from . import authz, eventlog, store
from .dispatch import CommandOutcome, execute_command, publish_committed
from .errors import (
    AmbiguousExecutor,
    CapabilityUnsupported,
    Forbidden,
    InvalidCommand,
    RateLimited,
    StaleRuntime,
)

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


async def _resolve_attachment_tx(
    tx: db.Tx,
    *,
    room: Room,
    participant: Participant,
    command: ConnectCommand,
) -> tuple[str | None, EventEnvelope | None]:
    """Find or create the durable runtime behind this connection.

    No label means ephemeral, which is the honest default and gets no row — the
    connection itself becomes the executor identity instead (D-034). A label lands
    on the same row every time via `UNIQUE (participant_id, label)`, which is the
    entire mechanism: reattachment is a lookup, not a heuristic.

    A returning attachment may re-declare `is_resumable` and `host_class`, and we
    take the newest declaration. A runtime that has been redeployed with different
    abilities is telling the truth about itself now; refusing the update would pin
    it to a claim it no longer makes.
    """
    if command.attachment_label is None:
        return None, None

    label = command.attachment_label.strip()
    if not label:
        return None, None

    now = utcnow_iso()
    existing = await tx.fetch_one(
        "SELECT id FROM attachments WHERE participant_id = ? AND label = ?",
        (participant.id, label),
    )
    if existing is not None:
        await tx.execute(
            "UPDATE attachments SET last_seen_at = ?, host_class = ?, is_resumable = ?, "
            "runtime_role = ?, executor_kind = ?, executor_model = ? WHERE id = ?",
            (
                now,
                command.host_class.value,
                1 if command.attachment_resumable else 0,
                command.runtime_role.value,
                command.executor_kind,
                command.executor_model,
                existing["id"],
            ),
        )
        return str(existing["id"]), None

    attachment_id = ids.new_id(ids.ATTACHMENT)
    await tx.execute(
        """
        INSERT INTO attachments (
            id, room_id, participant_id, label, host_class, is_resumable,
            runtime_role, executor_kind, executor_model, created_at, last_seen_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            attachment_id,
            room.id,
            participant.id,
            label,
            command.host_class.value,
            1 if command.attachment_resumable else 0,
            command.runtime_role.value,
            command.executor_kind,
            command.executor_model,
            now,
            now,
        ),
    )
    event = await eventlog.append(
        tx,
        room_id=room.id,
        type_=EventType.ATTACHMENT_REGISTERED,
        actor=EventActor(
            participant_id=participant.id,
            display_name=participant.identity.display_name,
            kind=participant.identity.kind,
            org_id=participant.org_id,
        ),
        payload={
            "attachment_id": attachment_id,
            "participant_id": participant.id,
            "label": label,
            "host_class": command.host_class.value,
            "is_resumable": command.attachment_resumable,
            # Grouped, and named for what it is worth. The room recorded these; it
            # verified none of them, and a projection that mixed them in with derived
            # facts would make a self-report look like an observation (D-054).
            "declared": {
                "role": command.runtime_role.value,
                "executor_kind": command.executor_kind,
                "model": command.executor_model,
            },
        },
    )
    return attachment_id, event


async def connect(
    *, participant: Participant, command: ConnectCommand, transport: str
) -> NegotiatedConnection:
    """Open a connection only while the room remains writable.

    Closed-room reads use snapshot, replay, or an existing stream; creating presence
    after the wall-clock deadline is a mutation and is refused transactionally.

    `transport` is what the caller will genuinely use, and negotiation intersects
    against it. An unknown transport name yields the empty capability set rather
    than a permissive default: a typo must cost a client its capabilities, not
    silently grant it someone else's.
    """
    room = await store.load_room(participant.room_id)
    authz.require_active(participant)

    profile, runtime = negotiate(
        declared=command.capabilities,
        host_class=command.host_class,
        transport=transport,
        room=room,
    )
    connection_id = ids.new_id(ids.CONNECTION)
    now = utcnow_iso()

    async def body(tx: db.Tx) -> CommandOutcome:
        live_room = await store.load_room(room.id, tx=tx)
        authz.require_writable(live_room)
        open_count = int(
            await tx.fetch_value(
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
        before = await _grade_participant(tx, participant.id, live_room)
        attachment_id, attachment_event = await _resolve_attachment_tx(
            tx, room=live_room, participant=participant, command=command
        )
        await tx.execute(
            """
            INSERT INTO connections (
                id, room_id, participant_id, attachment_id, host_class, profile,
                delivery_mode, heartbeat_interval_s, opened_at, last_heartbeat_at,
                last_delivered_seq
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                connection_id,
                room.id,
                participant.id,
                attachment_id,
                command.host_class.value,
                db.dumps(profile.model_dump()),
                runtime.delivery_mode.value,
                runtime.heartbeat_interval_s,
                now,
                now,
                command.since_seq,
            ),
        )
        after = await _grade_participant(tx, participant.id, live_room)
        events: list[EventEnvelope] = []
        if attachment_event is not None:
            events.append(attachment_event)
        if after != before:
            changed = await _append_presence_changed(
                tx,
                room=live_room,
                participant=participant,
                liveness=after,
                runtime=runtime,
            )
            if changed is not None:
                events.append(changed)
        return CommandOutcome(
            result={"connection_id": connection_id, "attachment_id": attachment_id},
            events=events,
        )

    outcome = await execute_command(
        command_id=command.command_id,
        command_type="presence.connect",
        room_id=room.id,
        participant_id=participant.id,
        body=body,
    )

    resolved_connection_id = str(outcome.result.get("connection_id", connection_id))
    return NegotiatedConnection(
        connection=await store.load_connection(resolved_connection_id),
        runtime=runtime,
        current_seq=await eventlog.current_seq(room.id),
        since_seq=command.since_seq,
    )


async def heartbeat(
    *, connection_id: str, participant: Participant, seq: int | None = None
) -> None:
    """Refresh a connection's liveness, and with it the owner's open work.

    Not written to the event log: heartbeats at 20s x N participants would swamp the
    log and drown the events that carry meaning. Only *grade transitions* are
    events (`docs/PROTOCOL.md` §3).

    One beat, both facts (D-059). Requiring a second beat on a second clock to keep a
    work card fresh is how the room came to grade a participant `live_poll` while
    calling its declared work `heartbeat_lapsed`. What the beat does *not* refresh is
    `progress_at` — a live socket is not evidence that anything is moving.
    """
    event: EventEnvelope | None = None
    async with db.transaction() as tx:
        room = await store.load_room(participant.room_id, tx=tx)
        authz.require_writable(room)
        before = await _grade_participant(tx, participant.id, room)
        now = utcnow_iso()
        beat = await tx.execute(
            """
            UPDATE connections
            SET last_heartbeat_at = ?, last_delivered_seq = MAX(last_delivered_seq, ?)
            WHERE id = ? AND participant_id = ? AND closed_at IS NULL
            """,
            (now, seq or 0, connection_id, participant.id),
        )
        if not beat:
            raise InvalidCommand(
                "Connection is closed or does not belong to this participant; reconnect.",
                connection_id=connection_id,
            )
        if beat:
            # One transaction: an expired room cannot accept the connection beat but
            # still refresh the work card (or vice versa).
            await tx.execute(
                "UPDATE work_declarations SET heartbeat_at = ? WHERE participant_id = ? "
                "AND ended_at IS NULL",
                (now, participant.id),
            )

        after = await _grade_participant(tx, participant.id, room)
        if after != before:
            event = await _append_presence_changed(
                tx, room=room, participant=participant, liveness=after, runtime=None
            )
    if event is not None:
        await publish_committed([event])


async def disconnect(*, connection_id: str, participant: Participant) -> None:
    """Close one connection. Losing the *last* one releases exclusive claims."""
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
        changed = await _append_presence_changed(
            tx, room=room, participant=participant, liveness=liveness, runtime=None
        )
        if changed is not None:
            events.append(changed)
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
    """A participant lost its last connection: release its exclusive holds.

    Doing this here rather than waiting for lease expiry is what makes a clean
    disconnect fast. The reaper remains the backstop for the ungraceful case, where
    nobody told us anything. A work declaration is not an exclusive hold, however:
    it records the participant's stated intent and survives a transport restart. The
    freshness pass marks it untrusted while the owner is absent; explicit leave and
    actual work/task exits are what end it.
    """
    from . import tasks

    return await tasks.release_all_claims_tx(tx, participant=participant, reason="presence_lost")


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
) -> EventEnvelope | None:
    # Presence is derived rather than stored, so a heartbeat can observe ``idle``
    # immediately before refreshing itself back to ``live_poll`` even though no
    # ``idle`` transition was ever published. Compare with the last public state,
    # not only with that transient pre-beat grade, or every poll produces an
    # identical live event and teaches consumers to ignore the stream.
    previous_row = await tx.fetch_one(
        """
        SELECT payload FROM room_events
        WHERE room_id = ? AND type = ? AND actor_participant_id = ?
        ORDER BY seq DESC LIMIT 1
        """,
        (room.id, EventType.PRESENCE_CHANGED.value, participant.id),
    )
    if previous_row is not None:
        previous_payload = db.loads(previous_row["payload"], {})
        if previous_payload.get("liveness") == liveness.value:
            return None

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
    attachments = await store.list_attachments(room.id)
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
            runtimes=_runtime_views(
                conns,
                {k: v for k, v in attachments.items() if v.participant_id == participant.id},
            ),
        )
    return views


def _runtime_views(
    connections: list[Connection], attachments: dict[str, Attachment]
) -> list[RuntimeView]:
    """One entry per runtime of this seat, graded individually.

    "This seat is live" answers the wrong question once a seat is a chat window plus
    a background worker: a human deciding whether to expect a reply and a worker
    deciding whether a sibling is executing both need to know *which* runtime is
    live, not that one of them is (D-054).

    Connections with no attachment are their own runtime, which is the same rule
    executor affinity already uses — NULL means "no durable runtime", never "no
    runtime" (D-034).
    """
    grouped: dict[str, list[Connection]] = {}
    for conn in connections:
        grouped.setdefault(conn.attachment_id or conn.id, []).append(conn)
    # Durable runtimes remain visible after transport loss. Their liveness is still
    # derived from the empty connection set, so a sibling control surface cannot
    # mask a dead companion attachment.
    for attachment_id in attachments:
        grouped.setdefault(attachment_id, [])

    views: list[RuntimeView] = []
    for ref, conns in grouped.items():
        attachment = attachments.get(ref)
        views.append(
            RuntimeView(
                ref=ref,
                is_attachment=attachment is not None,
                label=attachment.label if attachment else "",
                # Derived here, never read from a column: a runtime that died without
                # saying so must stop being live the moment its heartbeat lapses, and
                # a stored flag would still say otherwise (D-044).
                liveness=grade(conns),
                connection_count=len(conns),
                delivery_modes=sorted({c.delivery_mode for c in conns}, key=lambda m: m.value),
                last_seen_at=max((c.last_heartbeat_at for c in conns), default=None),
                declared=RuntimeDeclaration(
                    role=attachment.runtime_role if attachment else RuntimeRole.UNSPECIFIED,
                    executor_kind=attachment.executor_kind if attachment else "",
                    model=attachment.executor_model if attachment else "",
                    host_class=attachment.host_class if attachment else conns[0].host_class,
                    is_resumable=attachment.is_resumable if attachment else False,
                ),
                operation=attachment.operation if attachment else None,
            )
        )
    return sorted(views, key=lambda v: (-LIVENESS_RANK[v.liveness], v.label, v.ref))


def _transport_for(mode: DeliveryMode) -> str:
    if mode == DeliveryMode.PUSH:
        return "sse"
    if mode == DeliveryMode.LONG_POLL:
        return "long_poll"
    return "sse"


@dataclass(frozen=True)
class Executor:
    """Which runtime of a seat is doing the work, and the connections that are it.

    `attachment_id` when the client declared a resumable durable runtime, otherwise
    the single connection. Never both, and `connections` is what either of them
    resolves to right now — which is how liveness is answered later without storing
    a flag that would be wrong the moment a process died quietly.
    """

    attachment_id: str | None
    connection_id: str | None
    connections: tuple[Connection, ...] = ()

    @property
    def ref(self) -> str | None:
        return self.attachment_id or self.connection_id

    @property
    def is_live(self) -> bool:
        """Live means some connection of this runtime is still heard from.

        Graded rather than merely open: a connection nobody has heard from in three
        intervals is not evidence that anything is executing.
        """
        return any(grade_connection(c) not in _NOT_LIVE for c in self.connections)


_NOT_LIVE: frozenset[Liveness] = frozenset({Liveness.DISCONNECTED, Liveness.STALE})


async def _fetch_all(tx: db.Tx | None, sql: str, params: tuple) -> list[Any]:
    """Read inside the caller's transaction when there is one.

    An affinity decision made from a read outside the transaction that acts on it
    is a decision about a slightly older world, which is the shape of every
    check-then-act race.
    """
    return await (tx.fetch_all(sql, params) if tx is not None else db.fetch_all(sql, params))


async def open_connections(participant_id: str, *, tx: db.Tx | None = None) -> list[Connection]:
    rows = await _fetch_all(
        tx,
        "SELECT * FROM connections WHERE participant_id = ? AND closed_at IS NULL",
        (participant_id,),
    )
    return [store.to_connection(r) for r in rows]


async def resolve_executor(
    *, participant: Participant, connection_id: str | None = None, tx: db.Tx | None = None
) -> Executor:
    """Decide which runtime of this seat is about to execute.

    Order, from D-034: a named connection wins; otherwise a single open connection
    is unambiguous; otherwise all open connections belonging to one attachment are
    unambiguous because they *are* one runtime. Anything else is genuinely unknown
    and is refused rather than guessed.
    """
    conns = await open_connections(participant.id, tx=tx)
    if not conns:
        raise CapabilityUnsupported(
            "You have no open connection to this room, so nothing can be recorded "
            "as executing. Connect first.",
            participant_id=participant.id,
        )

    if connection_id is not None:
        named = next((c for c in conns if c.id == connection_id), None)
        if named is None:
            raise InvalidCommand(
                "That connection is not open for this participant.",
                connection_id=connection_id,
            )
        chosen = [named]
    else:
        attachments = {c.attachment_id for c in conns}
        # Unambiguous two ways: there is only one connection, or every connection
        # belongs to one durable runtime and therefore *is* one executor.
        if len(conns) == 1 or (len(attachments) == 1 and None not in attachments):
            chosen = conns
        else:
            raise AmbiguousExecutor(
                "This participant has several open connections from different "
                "runtimes, so which one is executing cannot be inferred. Send "
                "connection_id, or reconnect with an attachment_label so your "
                "connections are recognised as one runtime.",
                connection_ids=sorted(c.id for c in conns),
                participant_id=participant.id,
            )

    attachment_id = chosen[0].attachment_id
    if attachment_id is not None:
        # Refused here rather than at each call site, because this is the one place
        # every command passes through on its way to becoming somebody's executor.
        # A drained runtime is therefore unable to claim, renew, checkpoint, complete
        # or release anything, without a single one of those handlers knowing the
        # concept exists (D-062).
        await assert_not_drained(attachment_id, tx=tx)
        # Every open connection of that attachment is the same runtime, so affinity
        # survives any one of them dying — and returns to nothing when they all do,
        # because `is_live` asks about connections rather than about a stored flag.
        #
        # `is_resumable` deliberately does *not* switch this. Making it select
        # connection-scoping instead would reintroduce the guess this function
        # exists to refuse: with several connections of one non-resumable runtime,
        # there is no principled way to pick which connection is "the" executor.
        # What the declaration is actually for is recovery (D-036/D-038), where
        # "the same attachment came back" is only evidence if the client promised
        # the label would mean that.
        siblings = tuple(c for c in conns if c.attachment_id == attachment_id)
        return Executor(attachment_id=attachment_id, connection_id=None, connections=siblings)

    return Executor(attachment_id=None, connection_id=chosen[0].id, connections=(chosen[0],))


async def assert_not_drained(attachment_id: str, *, tx: db.Tx | None = None) -> int:
    """Refuse a runtime that was told to stop. Returns the current epoch.

    An attachment row that has vanished is not treated as drained: the runtime is
    then unknown rather than stopped, and inventing a refusal out of an absence
    would fail every ephemeral connection that never had a row (D-034).
    """
    rows = await _fetch_all(
        tx,
        "SELECT epoch, drained_at, drained_reason FROM attachments WHERE id = ?",
        (attachment_id,),
    )
    row = rows[0] if rows else None
    if row is None or row["drained_at"] is None:
        return int(row["epoch"]) if row is not None else 1
    raise StaleRuntime(
        "This runtime was drained and may no longer act in the room. It was not "
        "killed — the room cannot kill a process it does not own — so if it is "
        "still running, stop it. Reconnecting will not clear this: a drained "
        "runtime that comes back is the same drained runtime. Resume it explicitly "
        "once you know the old process is gone.",
        attachment_id=attachment_id,
        epoch=int(row["epoch"]),
        drained_at=row["drained_at"],
        reason=row["drained_reason"],
    )


async def _load_attachment_for(
    tx: db.Tx, room: Room, participant: Participant, attachment_id: str
) -> Any:
    rows = await tx.fetch_all(
        "SELECT * FROM attachments WHERE id = ? AND room_id = ?",
        (attachment_id, room.id),
    )
    if not rows:
        raise InvalidCommand("No such runtime in this room.", attachment_id=attachment_id)
    row = rows[0]
    # Scoped to the seat, not to the room. `room.admin` is not a licence to act as
    # another participant, and draining someone else's runtime is exactly that:
    # it stops their work, under their name, on their machine (see docs/SECURITY.md).
    if row["participant_id"] != participant.id:
        raise Forbidden(
            "That runtime belongs to another participant. Steer them with a directive "
            "instead — stopping another seat's worker is their decision to carry out, "
            "and a room admin is not an owner of other people's processes.",
            attachment_id=attachment_id,
        )
    return row


async def drain_runtime(
    *,
    room: Room,
    participant: Participant,
    attachment_id: str,
    reason: str = "",
    command_id: str | None = None,
) -> CommandOutcome:
    """Stop accepting this runtime's work, and say so in the log.

    This is the whole containment story for a process the server does not own. It
    does not signal, kill, or wait: it revokes permission, which is the one lever
    that works when the runtime is on someone else's machine — the normal case for
    this product, not the exception.

    Idempotent. Draining twice is a supervisor being careful about a process it
    cannot see, which is the situation this exists for, so the second call is a
    no-op that still answers with the epoch rather than an error.
    """

    async def body(tx: db.Tx) -> CommandOutcome:
        row = await _load_attachment_for(tx, room, participant, attachment_id)
        if row["drained_at"] is not None:
            return CommandOutcome(
                result={
                    "ok": True,
                    "attachment_id": attachment_id,
                    "epoch": int(row["epoch"]),
                    "drained_at": row["drained_at"],
                    "already_drained": True,
                },
                events=[],
            )

        now = utcnow_iso()
        epoch = int(row["epoch"]) + 1
        await tx.execute(
            "UPDATE attachments SET epoch = ?, drained_at = ?, drained_reason = ? "
            "WHERE id = ? AND drained_at IS NULL",
            (epoch, now, reason, attachment_id),
        )
        # Closing the connections is housekeeping, not the control. A survivor can
        # open a new one whenever it likes; what it cannot do is get past
        # `assert_not_drained`, because that reads the attachment rather than the
        # connection. Closing them simply stops the room showing a runtime as live
        # when it has been told to stop.
        await tx.execute(
            "UPDATE connections SET closed_at = ? WHERE attachment_id = ? AND closed_at IS NULL",
            (now, attachment_id),
        )
        event = await eventlog.append(
            tx,
            room_id=room.id,
            type_=EventType.RUNTIME_DRAINED,
            actor=EventActor(
                participant_id=participant.id,
                display_name=participant.identity.display_name,
                kind=participant.identity.kind,
                org_id=participant.org_id,
            ),
            payload={
                "attachment_id": attachment_id,
                "participant_id": participant.id,
                "label": row["label"],
                "epoch": epoch,
                "reason": reason,
            },
        )
        return CommandOutcome(
            result={
                "ok": True,
                "attachment_id": attachment_id,
                "epoch": epoch,
                "drained_at": now,
                "already_drained": False,
            },
            events=[event],
        )

    return await execute_command(
        command_id=command_id,
        command_type="runtime.drain",
        room_id=room.id,
        participant_id=participant.id,
        body=body,
    )


async def resume_runtime(
    *,
    room: Room,
    participant: Participant,
    attachment_id: str,
    note: str = "",
    command_id: str | None = None,
) -> CommandOutcome:
    """Let a drained runtime act again, as a deliberate and visible act.

    Separated from draining rather than folded into reconnect, because the question
    it answers is one only a human or supervisor can answer: *is the old process
    actually gone?* The room cannot see that. Making it a command means the claim
    is attributed to whoever made it and sits in the log next to the drain.

    The epoch is not rolled back. It counts runs, and a resumed runtime is a new
    run — so an in-flight message from before the drain is still recognisably from
    an older one.
    """

    async def body(tx: db.Tx) -> CommandOutcome:
        row = await _load_attachment_for(tx, room, participant, attachment_id)
        if row["drained_at"] is None:
            return CommandOutcome(
                result={
                    "ok": True,
                    "attachment_id": attachment_id,
                    "epoch": int(row["epoch"]),
                    "was_drained": False,
                },
                events=[],
            )

        await tx.execute(
            "UPDATE attachments SET drained_at = NULL, drained_reason = '' "
            "WHERE id = ? AND drained_at IS NOT NULL",
            (attachment_id,),
        )
        event = await eventlog.append(
            tx,
            room_id=room.id,
            type_=EventType.RUNTIME_RESUMED,
            actor=EventActor(
                participant_id=participant.id,
                display_name=participant.identity.display_name,
                kind=participant.identity.kind,
                org_id=participant.org_id,
            ),
            payload={
                "attachment_id": attachment_id,
                "participant_id": participant.id,
                "label": row["label"],
                "epoch": int(row["epoch"]),
                "note": note,
            },
        )
        return CommandOutcome(
            result={
                "ok": True,
                "attachment_id": attachment_id,
                "epoch": int(row["epoch"]),
                "was_drained": True,
            },
            events=[event],
        )

    return await execute_command(
        command_id=command_id,
        command_type="runtime.resume",
        room_id=room.id,
        participant_id=participant.id,
        body=body,
    )


async def executor_of(task_row: Any, *, tx: db.Tx | None = None) -> Executor:
    """Rebuild the executor recorded on a lease, with its connections as they are now.

    Liveness is never stored on the lease. A worker that dies without saying so
    would leave a stored flag reading `live` forever, and the whole point of asking
    at enforcement time is that nobody has to remember to clear it.
    """
    attachment_id = task_row["executor_attachment_id"]
    connection_id = task_row["executor_connection_id"]
    if attachment_id is None and connection_id is None:
        return Executor(attachment_id=None, connection_id=None, connections=())
    if attachment_id is not None:
        rows = await _fetch_all(
            tx,
            "SELECT * FROM connections WHERE attachment_id = ? AND closed_at IS NULL",
            (attachment_id,),
        )
    else:
        rows = await _fetch_all(
            tx,
            "SELECT * FROM connections WHERE id = ? AND closed_at IS NULL",
            (connection_id,),
        )
    return Executor(
        attachment_id=attachment_id,
        connection_id=connection_id,
        connections=tuple(store.to_connection(r) for r in rows),
    )


async def runtime_policy_for(
    participant: Participant, room: Room, *, executor: Executor | None = None
) -> RuntimePolicy:
    """The policy in force for a participant right now.

    Derived from its *live* connections, so a participant that reattached with
    different capabilities is judged on what it can do now, not what it once
    claimed. With no live connection there is nothing to derive from, and claiming
    is refused — an unreachable participant cannot be handed exclusive work.

    When an executor is given, the derivation narrows to that runtime's own
    connections. Without that narrowing a seat with a background worker attached
    would lend the worker's unattended standing to its chat surface, which is the
    honest-capabilities rule broken by an accident of sharing a seat.
    """
    conns = list(executor.connections) if executor is not None else []
    if not conns:
        conns = await open_connections(participant.id)
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
                changed = await _append_presence_changed(
                    tx, room=room, participant=participant, liveness=liveness, runtime=None
                )
                if changed is not None:
                    batch.append(changed)
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
