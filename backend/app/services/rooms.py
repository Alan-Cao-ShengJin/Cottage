"""The room service: the single source of truth for room state.

Every entry point (REST, SSE, MCP, the GPT agent loop) goes through this module,
so guardrails and privacy rules are enforced exactly once.
"""

from __future__ import annotations

import json
import logging
from datetime import timedelta
from typing import Any, Iterable

import aiosqlite

from ..config import settings
from ..db import database as db
from ..errors import Forbidden, GuardrailBlocked, NotFound, RoomError, RoomExpired
from ..events import RoomEvent, hub
from ..models import (
    Agent,
    CreateRoomRequest,
    CreateTaskRequest,
    JoinRoomRequest,
    JoinRoomResponse,
    MemoryPatch,
    Message,
    MessageType,
    Room,
    RoomSnapshot,
    SharedMemory,
    SharedMemoryData,
    Task,
)
from ..util import (
    new_id,
    new_join_code,
    new_token,
    normalize_join_code,
    parse_iso,
    utcnow,
    utcnow_iso,
)
from . import guardrails
from .guardrails import RoomTurnState

log = logging.getLogger(__name__)

MAX_JOIN_CODE_ATTEMPTS = 8
DEFAULT_MESSAGE_LIMIT = 100


# --------------------------------------------------------------------------
# Row -> model
# --------------------------------------------------------------------------


def _seconds_remaining(expires_at: str) -> int:
    return max(0, int((parse_iso(expires_at) - utcnow()).total_seconds()))


def _room_from_row(row: aiosqlite.Row) -> Room:
    return Room(
        id=row["id"],
        join_code=row["join_code"],
        title=row["title"],
        objective=row["objective"],
        status=row["status"],
        created_at=row["created_at"],
        expires_at=row["expires_at"],
        seconds_remaining=_seconds_remaining(row["expires_at"]),
        agent_turns_used=row["agent_turns_used"],
        max_agent_turns=settings.max_room_agent_turns,
        autonomy_enabled=bool(row["autonomy_enabled"]),
    )


def _agent_from_row(row: aiosqlite.Row) -> Agent:
    return Agent(
        id=row["id"],
        room_id=row["room_id"],
        owner_name=row["owner_name"],
        agent_name=row["agent_name"],
        provider=row["provider"],
        public_objective=row["public_objective"],
        status=row["status"],
        autonomous=bool(row["autonomous"]),
        joined_at=row["joined_at"],
        last_seen_at=row["last_seen_at"],
    )


def _message_from_row(row: aiosqlite.Row) -> Message:
    return Message(
        id=row["id"],
        room_id=row["room_id"],
        agent_id=row["agent_id"],
        sender_label=row["sender_label"],
        recipient_agent_id=row["recipient_agent_id"],
        content=row["content"],
        message_type=row["message_type"],
        created_at=row["created_at"],
    )


def _task_from_row(row: aiosqlite.Row) -> Task:
    return Task(
        id=row["id"],
        room_id=row["room_id"],
        title=row["title"],
        description=row["description"],
        assigned_agent_id=row["assigned_agent_id"],
        status=row["status"],
        result=row["result"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


# --------------------------------------------------------------------------
# Rooms
# --------------------------------------------------------------------------


async def create_room(req: CreateRoomRequest) -> Room:
    now = utcnow()
    ttl = req.ttl_seconds or settings.room_ttl_seconds
    expires_at = now + timedelta(seconds=ttl)
    room_id = new_id("room")
    for attempt in range(MAX_JOIN_CODE_ATTEMPTS):
        code = new_join_code()
        try:
            await db.execute(
                """INSERT INTO rooms (id, join_code, title, objective, status, created_at, expires_at)
                   VALUES (?, ?, ?, ?, 'active', ?, ?)""",
                (
                    room_id,
                    code,
                    req.title.strip(),
                    req.objective.strip(),
                    now.isoformat(),
                    expires_at.isoformat(),
                ),
            )
            break
        except aiosqlite.IntegrityError:
            if attempt == MAX_JOIN_CODE_ATTEMPTS - 1:
                raise RoomError("Could not allocate a unique join code; try again.")
            continue

    memory = SharedMemoryData(objective=req.objective.strip())
    await db.execute(
        "INSERT INTO shared_memory (room_id, data, updated_at, updated_by) VALUES (?, ?, ?, ?)",
        (room_id, memory.model_dump_json(), utcnow_iso(), "system"),
    )

    room = await get_room(room_id)
    log.info("room created id=%s code=%s ttl=%ss", room.id, room.join_code, ttl)
    await _system_message(room_id, f'Room opened. Objective: "{room.objective}"')
    return room


async def _fetch_room_row(room_id_or_code: str) -> aiosqlite.Row:
    row = await db.fetch_one("SELECT * FROM rooms WHERE id = ?", (room_id_or_code,))
    if row is None:
        row = await db.fetch_one(
            "SELECT * FROM rooms WHERE join_code = ?", (normalize_join_code(room_id_or_code),)
        )
    if row is None:
        raise NotFound(f"No room found for '{room_id_or_code}'.")
    return row


async def get_room(room_id_or_code: str) -> Room:
    """Look a room up by id or join code, expiring it lazily if its TTL passed."""
    row = await _fetch_room_row(room_id_or_code)
    room = _room_from_row(row)
    if room.status == "active" and room.seconds_remaining <= 0:
        room = await _mark_expired(room)
    return room


async def _mark_expired(room: Room) -> Room:
    await db.execute("UPDATE rooms SET status = 'expired' WHERE id = ?", (room.id,))
    log.info("room %s (%s) expired", room.join_code, room.id)
    await _system_message(room.id, "Room expired. No new messages will be accepted.")
    await hub.publish(RoomEvent(room_id=room.id, type="room_expired", payload={}))
    return room.model_copy(update={"status": "expired"})


async def require_active_room(room_id_or_code: str) -> Room:
    room = await get_room(room_id_or_code)
    if room.status != "active":
        raise RoomExpired(f"Room {room.join_code} is {room.status}; it no longer accepts changes.")
    return room


async def set_autonomy(room_id_or_code: str, enabled: bool) -> Room:
    room = await get_room(room_id_or_code)
    await db.execute("UPDATE rooms SET autonomy_enabled = ? WHERE id = ?", (1 if enabled else 0, room.id))
    await _system_message(
        room.id,
        f"Autonomous collaboration {'started' if enabled else 'paused'} by a human.",
    )
    updated = await get_room(room.id)
    await hub.publish(RoomEvent(room_id=room.id, type="room_updated", payload={"room": updated.model_dump()}))
    return updated


async def reset_turn_budget(room_id_or_code: str) -> Room:
    """Human escape hatch once the room's agent-turn budget is spent."""
    room = await get_room(room_id_or_code)
    await db.execute(
        "UPDATE rooms SET agent_turns_used = 0, consecutive_turns = 0, last_speaker_id = NULL WHERE id = ?",
        (room.id,),
    )
    await _system_message(room.id, "A human reset the agent turn budget.")
    updated = await get_room(room.id)
    await hub.publish(RoomEvent(room_id=room.id, type="room_updated", payload={"room": updated.model_dump()}))
    return updated


async def set_expiry_now(room_id_or_code: str) -> Room:
    """Bring a room's TTL forward to now. Lets a demo show expiry without waiting."""
    room = await get_room(room_id_or_code)
    await db.execute("UPDATE rooms SET expires_at = ? WHERE id = ?", (utcnow_iso(), room.id))
    return await get_room(room.id)


async def list_rooms(limit: int = 25) -> list[Room]:
    rows = await db.fetch_all("SELECT * FROM rooms ORDER BY created_at DESC LIMIT ?", (limit,))
    return [_room_from_row(r) for r in rows]


async def expire_due_rooms() -> list[str]:
    """Sweep every active room whose TTL has passed. Used by the background janitor."""
    rows = await db.fetch_all("SELECT * FROM rooms WHERE status = 'active'")
    expired: list[str] = []
    for row in rows:
        room = _room_from_row(row)
        if room.seconds_remaining <= 0:
            await _mark_expired(room)
            expired.append(room.id)
    return expired


async def purge_room(room_id_or_code: str) -> None:
    """Hard delete. V0 keeps expired rooms around for debugging; this is the
    cleanup operation a real TTL job would call."""
    room = await get_room(room_id_or_code)
    await db.execute("DELETE FROM rooms WHERE id = ?", (room.id,))
    log.info("room %s purged", room.join_code)


# --------------------------------------------------------------------------
# Agents
# --------------------------------------------------------------------------


async def join_room(req: JoinRoomRequest) -> JoinRoomResponse:
    room = await require_active_room(req.join_code)

    existing = await db.fetch_one(
        "SELECT * FROM agents WHERE room_id = ? AND agent_name = ?",
        (room.id, req.agent_name.strip()),
    )
    now = utcnow_iso()

    if existing is not None:
        # Re-joining under the same name resumes the same identity. Claude Code
        # restarts often; issuing a brand-new agent every time would litter the
        # room with ghosts.
        await db.execute(
            """UPDATE agents
               SET status = 'active', last_seen_at = ?, public_objective = ?,
                   owner_name = ?, provider = ?, autonomous = ?
               WHERE id = ?""",
            (
                now,
                req.public_objective.strip() or existing["public_objective"],
                req.owner_name.strip(),
                req.provider,
                1 if req.autonomous else 0,
                existing["id"],
            ),
        )
        agent = await get_agent(existing["id"])
        token = existing["token"]
        await _system_message(room.id, f"{agent.agent_name} rejoined the room.")
    else:
        agent_id = new_id("agent")
        token = new_token()
        await db.execute(
            """INSERT INTO agents
               (id, room_id, owner_name, agent_name, provider, public_objective,
                status, autonomous, joined_at, last_seen_at, token)
               VALUES (?, ?, ?, ?, ?, ?, 'active', ?, ?, ?, ?)""",
            (
                agent_id,
                room.id,
                req.owner_name.strip(),
                req.agent_name.strip(),
                req.provider,
                req.public_objective.strip(),
                1 if req.autonomous else 0,
                now,
                now,
                token,
            ),
        )
        agent = await get_agent(agent_id)
        await _post_event_message(
            room.id,
            agent,
            f"{agent.agent_name} (owner: {agent.owner_name}) joined. "
            f"Objective: {agent.public_objective or 'not stated'}",
            "join",
        )

    log.info("agent %s joined room %s", agent.agent_name, room.join_code)
    await hub.publish(
        RoomEvent(room_id=room.id, type="agent_joined", payload={"agent": agent.model_dump()})
    )
    return JoinRoomResponse(agent=agent, agent_token=token, room=await get_room(room.id))


async def leave_room(agent_id: str, reason: str = "") -> Agent:
    agent = await get_agent(agent_id)
    await db.execute(
        "UPDATE agents SET status = 'left', last_seen_at = ? WHERE id = ?", (utcnow_iso(), agent_id)
    )
    guardrails.clear_agent(agent_id)
    note = f"{agent.agent_name} left the room."
    if reason:
        note += f" Reason: {reason}"
    await _post_event_message(agent.room_id, agent, note, "leave")
    updated = await get_agent(agent_id)
    await hub.publish(
        RoomEvent(room_id=agent.room_id, type="agent_left", payload={"agent": updated.model_dump()})
    )
    return updated


async def get_agent(agent_id: str) -> Agent:
    row = await db.fetch_one("SELECT * FROM agents WHERE id = ?", (agent_id,))
    if row is None:
        raise NotFound(f"No agent '{agent_id}'.")
    return _agent_from_row(row)


async def authenticate_agent(token: str) -> Agent:
    """Resolve an agent token. V0 auth is a bearer capability per agent - enough
    to stop one agent impersonating another, and no more."""
    row = await db.fetch_one("SELECT * FROM agents WHERE token = ?", (token,))
    if row is None:
        raise Forbidden("Unknown agent token. Join the room again to get a fresh one.")
    await db.execute("UPDATE agents SET last_seen_at = ? WHERE id = ?", (utcnow_iso(), row["id"]))
    return _agent_from_row(row)


async def get_members(room_id_or_code: str, include_left: bool = True) -> list[Agent]:
    room = await get_room(room_id_or_code)
    sql = "SELECT * FROM agents WHERE room_id = ?"
    params: list[Any] = [room.id]
    if not include_left:
        sql += " AND status = 'active'"
    sql += " ORDER BY joined_at ASC"
    rows = await db.fetch_all(sql, params)
    return [_agent_from_row(r) for r in rows]


async def touch_agent(agent_id: str) -> None:
    await db.execute("UPDATE agents SET last_seen_at = ? WHERE id = ?", (utcnow_iso(), agent_id))


async def find_agent_by_name(room_id: str, name: str) -> Agent | None:
    row = await db.fetch_one(
        "SELECT * FROM agents WHERE room_id = ? AND lower(agent_name) = lower(?)",
        (room_id, name.strip()),
    )
    return _agent_from_row(row) if row else None


# --------------------------------------------------------------------------
# Messages
# --------------------------------------------------------------------------


async def read_messages(
    room_id_or_code: str, since_id: int = 0, limit: int = DEFAULT_MESSAGE_LIMIT
) -> list[Message]:
    room = await get_room(room_id_or_code)
    rows = await db.fetch_all(
        "SELECT * FROM messages WHERE room_id = ? AND id > ? ORDER BY id ASC LIMIT ?",
        (room.id, since_id, limit),
    )
    return [_message_from_row(r) for r in rows]


async def recent_messages(room_id: str, limit: int) -> list[Message]:
    rows = await db.fetch_all(
        "SELECT * FROM messages WHERE room_id = ? ORDER BY id DESC LIMIT ?", (room_id, limit)
    )
    return [_message_from_row(r) for r in reversed(rows)]


async def _insert_message(
    room_id: str,
    agent_id: str | None,
    sender_label: str,
    content: str,
    message_type: MessageType,
    recipient_agent_id: str | None = None,
) -> Message:
    message_id = await db.execute(
        """INSERT INTO messages
           (room_id, agent_id, sender_label, recipient_agent_id, content, message_type, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (room_id, agent_id, sender_label, recipient_agent_id, content.strip(), message_type, utcnow_iso()),
    )
    row = await db.fetch_one("SELECT * FROM messages WHERE id = ?", (message_id,))
    assert row is not None
    message = _message_from_row(row)
    await hub.publish(
        RoomEvent(room_id=room_id, type="message", payload={"message": message.model_dump()})
    )
    return message


async def _system_message(room_id: str, content: str) -> Message:
    return await _insert_message(room_id, None, "system", content, "system")


async def _post_event_message(room_id: str, agent: Agent, content: str, kind: MessageType) -> Message:
    return await _insert_message(room_id, agent.id, agent.agent_name, content, kind)


async def post_human_message(
    room_id_or_code: str, content: str, sender_label: str = "Human", recipient_agent_id: str | None = None
) -> Message:
    room = await require_active_room(room_id_or_code)
    return await _insert_message(room.id, None, sender_label, content, "human", recipient_agent_id)


async def post_message(
    *,
    agent: Agent,
    content: str,
    recipient_agent_id: str | None = None,
    message_type: MessageType = "chat",
    autonomous: bool = False,
) -> Message:
    """Post as an agent. This is the guardrail choke point.

    Counts toward the room turn budget only for `chat` messages - bookkeeping
    updates (memory/tasks) should never cost an agent its right to speak.
    """
    room = await get_room(agent.room_id)
    if room.status != "active":
        raise RoomExpired(f"Room {room.join_code} is {room.status}; no new messages accepted.")

    if agent.status != "active":
        raise Forbidden(f"{agent.agent_name} has left the room; rejoin before posting.")

    if recipient_agent_id:
        recipient = await db.fetch_one(
            "SELECT id FROM agents WHERE id = ? AND room_id = ?", (recipient_agent_id, room.id)
        )
        if recipient is None:
            raise NotFound(f"No agent '{recipient_agent_id}' in this room.")

    counts_as_turn = message_type == "chat"
    if counts_as_turn:
        state = await _turn_state(room)
        verdict = guardrails.check_turn(state, agent.id, autonomous=autonomous)
        if not verdict.allowed:
            log.info("guardrail blocked %s in %s: %s", agent.agent_name, room.join_code, verdict.reason)
            raise GuardrailBlocked(verdict.reason)

    message = await _insert_message(
        room.id, agent.id, agent.agent_name, content, message_type, recipient_agent_id
    )

    if counts_as_turn:
        await _record_turn(room, agent.id)
    await touch_agent(agent.id)
    return message


async def _turn_state(room: Room) -> RoomTurnState:
    row = await _fetch_room_row(room.id)
    return RoomTurnState(
        status=room.status,
        autonomy_enabled=bool(row["autonomy_enabled"]),
        agent_turns_used=row["agent_turns_used"],
        last_speaker_id=row["last_speaker_id"],
        consecutive_turns=row["consecutive_turns"],
        seconds_remaining=room.seconds_remaining,
    )


async def _record_turn(room: Room, agent_id: str) -> None:
    row = await _fetch_room_row(room.id)
    consecutive = row["consecutive_turns"] + 1 if row["last_speaker_id"] == agent_id else 1
    await db.execute(
        """UPDATE rooms
           SET agent_turns_used = agent_turns_used + 1, last_speaker_id = ?, consecutive_turns = ?
           WHERE id = ?""",
        (agent_id, consecutive, room.id),
    )
    guardrails.record_turn(agent_id)
    updated = await get_room(room.id)
    await hub.publish(
        RoomEvent(room_id=room.id, type="room_updated", payload={"room": updated.model_dump()})
    )
    if updated.agent_turns_used >= settings.max_room_agent_turns:
        await _system_message(
            room.id,
            f"Agent turn budget reached ({settings.max_room_agent_turns}). "
            "Autonomous exchange halted until a human resets it.",
        )


async def can_agent_speak(agent: Agent, *, autonomous: bool = True) -> tuple[bool, str]:
    """Non-mutating guardrail probe, exposed to agents so they can self-check."""
    room = await get_room(agent.room_id)
    verdict = guardrails.check_turn(await _turn_state(room), agent.id, autonomous=autonomous)
    return verdict.allowed, verdict.reason


# --------------------------------------------------------------------------
# Shared memory
# --------------------------------------------------------------------------


async def get_shared_memory(room_id_or_code: str) -> SharedMemory:
    room = await get_room(room_id_or_code)
    row = await db.fetch_one("SELECT * FROM shared_memory WHERE room_id = ?", (room.id,))
    if row is None:
        data = SharedMemoryData(objective=room.objective)
        now = utcnow_iso()
        await db.execute(
            "INSERT INTO shared_memory (room_id, data, updated_at, updated_by) VALUES (?, ?, ?, ?)",
            (room.id, data.model_dump_json(), now, "system"),
        )
        return SharedMemory(room_id=room.id, data=data, updated_at=now, updated_by="system")
    return SharedMemory(
        room_id=room.id,
        data=SharedMemoryData.model_validate(json.loads(row["data"])),
        updated_at=row["updated_at"],
        updated_by=row["updated_by"],
    )


def _merge(existing: list[str], additions: Iterable[str]) -> list[str]:
    seen = {item.strip().lower() for item in existing}
    merged = list(existing)
    for item in additions:
        clean = item.strip()
        if clean and clean.lower() not in seen:
            merged.append(clean)
            seen.add(clean.lower())
    return merged


async def update_shared_memory(
    room_id_or_code: str,
    patch: MemoryPatch,
    updated_by: str | None = None,
    actor_agent_id: str | None = None,
) -> SharedMemory:
    room = await require_active_room(room_id_or_code)
    current = await get_shared_memory(room.id)
    data = current.data.model_copy(deep=True)

    if patch.objective is not None:
        data.objective = patch.objective.strip()

    data.decisions = _merge(
        patch.replace_decisions if patch.replace_decisions is not None else data.decisions,
        patch.add_decisions,
    )
    data.facts = _merge(
        patch.replace_facts if patch.replace_facts is not None else data.facts, patch.add_facts
    )
    data.assumptions = _merge(
        patch.replace_assumptions if patch.replace_assumptions is not None else data.assumptions,
        patch.add_assumptions,
    )
    data.open_questions = _merge(
        patch.replace_open_questions
        if patch.replace_open_questions is not None
        else data.open_questions,
        patch.add_open_questions,
    )
    data.disagreements = _merge(
        patch.replace_disagreements
        if patch.replace_disagreements is not None
        else data.disagreements,
        patch.add_disagreements,
    )

    if patch.resolve_open_questions:
        resolved = {q.strip().lower() for q in patch.resolve_open_questions}
        data.open_questions = [q for q in data.open_questions if q.strip().lower() not in resolved]

    now = utcnow_iso()
    await db.execute(
        """INSERT INTO shared_memory (room_id, data, updated_at, updated_by)
           VALUES (?, ?, ?, ?)
           ON CONFLICT(room_id) DO UPDATE SET data = excluded.data,
                                              updated_at = excluded.updated_at,
                                              updated_by = excluded.updated_by""",
        (room.id, data.model_dump_json(), now, updated_by),
    )
    memory = SharedMemory(room_id=room.id, data=data, updated_at=now, updated_by=updated_by)
    await hub.publish(
        RoomEvent(
            room_id=room.id,
            type="memory_updated",
            payload={"memory": memory.model_dump(), "actor_agent_id": actor_agent_id},
        )
    )
    return memory


# --------------------------------------------------------------------------
# Tasks
# --------------------------------------------------------------------------


async def list_tasks(room_id_or_code: str) -> list[Task]:
    room = await get_room(room_id_or_code)
    rows = await db.fetch_all("SELECT * FROM tasks WHERE room_id = ? ORDER BY created_at ASC", (room.id,))
    return [_task_from_row(r) for r in rows]


async def get_task(task_id: str) -> Task:
    row = await db.fetch_one("SELECT * FROM tasks WHERE id = ?", (task_id,))
    if row is None:
        raise NotFound(f"No task '{task_id}'.")
    return _task_from_row(row)


async def create_task(
    room_id_or_code: str, req: CreateTaskRequest, created_by: Agent | None = None
) -> Task:
    room = await require_active_room(room_id_or_code)
    task_id = new_id("task")
    now = utcnow_iso()
    assignee = created_by.id if (req.assign_to_self and created_by) else None
    await db.execute(
        """INSERT INTO tasks (id, room_id, title, description, assigned_agent_id, status, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            task_id,
            room.id,
            req.title.strip(),
            req.description.strip(),
            assignee,
            "claimed" if assignee else "open",
            now,
            now,
        ),
    )
    task = await get_task(task_id)
    label = created_by.agent_name if created_by else "system"
    await _insert_message(
        room.id,
        created_by.id if created_by else None,
        label,
        f"Task created: {task.title}" + (f" (claimed by {label})" if assignee else ""),
        "task_update",
    )
    await hub.publish(RoomEvent(room_id=room.id, type="task_updated", payload={"task": task.model_dump(), "actor_agent_id": created_by.id if created_by else None}))
    return task


async def claim_task(task_id: str, agent: Agent) -> Task:
    task = await get_task(task_id)
    if task.room_id != agent.room_id:
        raise Forbidden("That task belongs to a different room.")
    if task.status in ("done", "cancelled"):
        raise RoomError(f"Task is already {task.status}.")
    if task.assigned_agent_id and task.assigned_agent_id != agent.id:
        owner = await get_agent(task.assigned_agent_id)
        raise RoomError(f"Task already claimed by {owner.agent_name}.")

    await db.execute(
        "UPDATE tasks SET assigned_agent_id = ?, status = 'claimed', updated_at = ? WHERE id = ?",
        (agent.id, utcnow_iso(), task_id),
    )
    updated = await get_task(task_id)
    await _insert_message(
        agent.room_id, agent.id, agent.agent_name, f"Claimed task: {updated.title}", "task_update"
    )
    await hub.publish(
        RoomEvent(room_id=agent.room_id, type="task_updated", payload={"task": updated.model_dump(), "actor_agent_id": agent.id})
    )
    return updated


async def complete_task(task_id: str, agent: Agent, result: str = "") -> Task:
    task = await get_task(task_id)
    if task.room_id != agent.room_id:
        raise Forbidden("That task belongs to a different room.")

    await db.execute(
        "UPDATE tasks SET status = 'done', result = ?, assigned_agent_id = ?, updated_at = ? WHERE id = ?",
        (result.strip(), task.assigned_agent_id or agent.id, utcnow_iso(), task_id),
    )
    updated = await get_task(task_id)
    summary = f"Completed task: {updated.title}"
    if result.strip():
        summary += f" - {result.strip()}"
    await _insert_message(agent.room_id, agent.id, agent.agent_name, summary, "task_update")
    await hub.publish(
        RoomEvent(room_id=agent.room_id, type="task_updated", payload={"task": updated.model_dump(), "actor_agent_id": agent.id})
    )
    return updated


# --------------------------------------------------------------------------
# Snapshot
# --------------------------------------------------------------------------


async def get_snapshot(room_id_or_code: str, message_limit: int = DEFAULT_MESSAGE_LIMIT) -> RoomSnapshot:
    room = await get_room(room_id_or_code)
    return RoomSnapshot(
        room=room,
        agents=await get_members(room.id),
        messages=await recent_messages(room.id, message_limit),
        memory=await get_shared_memory(room.id),
        tasks=await list_tasks(room.id),
    )


