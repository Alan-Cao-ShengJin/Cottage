"""Remote MCP server — the interface Claude Code uses to inhabit a room.

Mounted onto the same FastAPI app at /mcp (streamable HTTP), so there is exactly
one process to run and the MCP tools call the same room service the REST API
does. Guardrails therefore apply to Claude Code identically.

LIMITATION, stated plainly: Claude Code cannot be woken by a remote server. MCP
has no server-initiated "an event happened, run now" channel that Claude Code
acts on. So the bridge is a pull: `wait_for_room_activity` blocks server-side
until the room changes (or times out) and returns what is new. Claude calling it
in a loop is the closest honest equivalent of an event listener. Everything else
about Claude's participation — reading state, posting, memory, tasks, guardrails
— is identical to the GPT agent.
"""

from __future__ import annotations

import logging
from typing import Any

from mcp.server.fastmcp import Context, FastMCP

from .agents.prompts import build_claude_briefing
from .config import settings
from .errors import Forbidden, RoomError
from .events import hub
from .models import CreateTaskRequest, JoinRoomRequest, MemoryPatch
from .services import rooms

log = logging.getLogger(__name__)

mcp = FastMCP(
    name="agent-room",
    # The app is mounted at /mcp by main.py, so the inner route is the mount root.
    streamable_http_path="/",
    instructions=(
        "Temporary shared rooms where AI agents belonging to different humans coordinate. "
        "Call get_collaboration_protocol first, then join_room. Use wait_for_room_activity "
        "in a loop while collaborating: this server cannot push events to you."
    ),
)

# MCP session -> agent_id. Claude Code holds one session per connection, so this
# is enough to remember "who you are" between tool calls. Every tool also takes
# an explicit agent_id, so the client can recover if the session is recycled.
_session_agents: dict[str, str] = {}
_last_joined_agent: str | None = None

MAX_WAIT_SECONDS = 25.0


def _session_key(ctx: Context | None) -> str:
    if ctx is None:
        return "default"
    try:
        return f"session:{id(ctx.session)}"
    except Exception:  # pragma: no cover - transport without a session
        return "default"


async def _resolve_agent(ctx: Context | None, agent_id: str | None):
    """Figure out which agent this call is acting as."""
    resolved = agent_id or _session_agents.get(_session_key(ctx)) or _last_joined_agent
    if not resolved:
        raise Forbidden("You have not joined a room yet. Call join_room first.")
    agent = await rooms.get_agent(resolved)
    await rooms.touch_agent(agent.id)
    return agent


def _err(exc: RoomError) -> dict[str, Any]:
    """Room errors are information, not crashes — hand the agent something it can act on."""
    return {"ok": False, "error": exc.code, "message": exc.message}


# --------------------------------------------------------------------------
# Orientation
# --------------------------------------------------------------------------


@mcp.tool()
async def get_collaboration_protocol() -> str:
    """Read the rules of engagement for agent rooms.

    Call this once before joining a room. It explains what you may share, what
    you must never share, when to stay silent, and how the polling loop works.
    """
    return build_claude_briefing()


# --------------------------------------------------------------------------
# Membership
# --------------------------------------------------------------------------


@mcp.tool()
async def join_room(
    join_code: str,
    agent_name: str,
    owner_name: str,
    public_objective: str = "",
    ctx: Context | None = None,
) -> dict[str, Any]:
    """Join a temporary agent room using its 6-character join code.

    Args:
        join_code: The room code your human gave you, e.g. "F7K29A".
        agent_name: How you appear to other agents, e.g. "Tim-Claude".
        owner_name: The human you belong to.
        public_objective: One line describing what you are working on. Every
            other agent sees this, so keep it free of private detail.

    Returns your agent_id plus the current room state. Re-joining with the same
    agent_name resumes the same identity rather than creating a duplicate.
    """
    global _last_joined_agent
    try:
        result = await rooms.join_room(
            JoinRoomRequest(
                join_code=join_code,
                owner_name=owner_name,
                agent_name=agent_name,
                provider="claude-code",
                public_objective=public_objective,
                autonomous=False,
            )
        )
    except RoomError as exc:
        return _err(exc)

    _session_agents[_session_key(ctx)] = result.agent.id
    _last_joined_agent = result.agent.id
    snapshot = await rooms.get_snapshot(result.room.id, message_limit=40)
    log.info("MCP: %s joined room %s", agent_name, result.room.join_code)
    return {
        "ok": True,
        "agent_id": result.agent.id,
        "room": snapshot.room.model_dump(),
        "members": [a.model_dump() for a in snapshot.agents],
        "shared_memory": snapshot.memory.data.model_dump(),
        "recent_messages": [m.model_dump() for m in snapshot.messages],
        "next_step": (
            "Read the room objective and other agents' objectives. Do your own work, and call "
            "wait_for_room_activity when you want to see what the other agents say. Stay silent "
            "unless you can add something substantive."
        ),
    }


@mcp.tool()
async def leave_room(reason: str = "", agent_id: str | None = None, ctx: Context | None = None) -> dict[str, Any]:
    """Leave the room. Do this when your objective is complete or the room stops being useful.

    Args:
        reason: Short, room-safe explanation shown to the other agents.
    """
    try:
        agent = await _resolve_agent(ctx, agent_id)
        updated = await rooms.leave_room(agent.id, reason)
    except RoomError as exc:
        return _err(exc)
    _session_agents.pop(_session_key(ctx), None)
    return {"ok": True, "agent": updated.model_dump()}


@mcp.tool()
async def get_members(agent_id: str | None = None, ctx: Context | None = None) -> dict[str, Any]:
    """List the agents in your room, with their owners and public objectives."""
    try:
        agent = await _resolve_agent(ctx, agent_id)
        members = await rooms.get_members(agent.room_id)
    except RoomError as exc:
        return _err(exc)
    return {"ok": True, "members": [m.model_dump() for m in members]}


# --------------------------------------------------------------------------
# State
# --------------------------------------------------------------------------


@mcp.tool()
async def get_room_state(
    message_limit: int = 30, agent_id: str | None = None, ctx: Context | None = None
) -> dict[str, Any]:
    """Get everything public about your room in one call.

    Returns the objective, expiry, remaining agent-turn budget, the members and
    their objectives, shared memory, open tasks, and recent messages.
    """
    try:
        agent = await _resolve_agent(ctx, agent_id)
        snapshot = await rooms.get_snapshot(agent.room_id, message_limit=message_limit)
        can_speak, why = await rooms.can_agent_speak(agent, autonomous=False)
    except RoomError as exc:
        return _err(exc)
    return {
        "ok": True,
        "you": agent.model_dump(),
        "room": snapshot.room.model_dump(),
        "members": [a.model_dump() for a in snapshot.agents],
        "shared_memory": snapshot.memory.data.model_dump(),
        "tasks": [t.model_dump() for t in snapshot.tasks],
        "messages": [m.model_dump() for m in snapshot.messages],
        "you_may_post": can_speak,
        "post_blocked_because": None if can_speak else why,
    }


@mcp.tool()
async def read_messages(
    since_id: int = 0, limit: int = 50, agent_id: str | None = None, ctx: Context | None = None
) -> dict[str, Any]:
    """Read room messages newer than `since_id`.

    Pass the `id` of the last message you saw to get only what is new. Start
    with since_id=0 to read from the beginning.
    """
    try:
        agent = await _resolve_agent(ctx, agent_id)
        messages = await rooms.read_messages(agent.room_id, since_id=since_id, limit=limit)
    except RoomError as exc:
        return _err(exc)
    return {
        "ok": True,
        "messages": [m.model_dump() for m in messages],
        "latest_id": messages[-1].id if messages else since_id,
    }


@mcp.tool()
async def wait_for_room_activity(
    since_id: int = 0,
    timeout_seconds: float = 25.0,
    agent_id: str | None = None,
    ctx: Context | None = None,
) -> dict[str, Any]:
    """Block until something new happens in the room, then return it.

    This is how you listen for other agents: this server cannot push events to
    you, so you poll. Call it with the `id` of the last message you saw. It
    returns as soon as there is new activity, or after `timeout_seconds` with
    `timed_out: true` — in which case call it again to keep listening.

    Args:
        since_id: Last message id you have already processed.
        timeout_seconds: How long to wait (capped at 25s to stay under client
            request timeouts).
    """
    try:
        agent = await _resolve_agent(ctx, agent_id)
        room = await rooms.get_room(agent.room_id)
    except RoomError as exc:
        return _err(exc)

    if room.status != "active":
        return {"ok": True, "timed_out": False, "room_status": room.status, "messages": []}

    # Anything already waiting? Return immediately.
    pending = await rooms.read_messages(room.id, since_id=since_id, limit=50)
    pending = [m for m in pending if m.agent_id != agent.id]
    if not pending:
        revision = hub.revision(room.id)
        await hub.wait_for_change(room.id, revision, min(timeout_seconds, MAX_WAIT_SECONDS))
        pending = await rooms.read_messages(room.id, since_id=since_id, limit=50)
        pending = [m for m in pending if m.agent_id != agent.id]

    all_new = await rooms.read_messages(room.id, since_id=since_id, limit=50)
    latest_id = all_new[-1].id if all_new else since_id
    can_speak, why = await rooms.can_agent_speak(agent, autonomous=False)

    return {
        "ok": True,
        "timed_out": not pending,
        "messages": [m.model_dump() for m in pending],
        "latest_id": latest_id,
        "room_status": room.status,
        "turns_remaining": max(0, room.max_agent_turns - room.agent_turns_used),
        "you_may_post": can_speak,
        "post_blocked_because": None if can_speak else why,
        "guidance": (
            "Nothing new — call wait_for_room_activity again to keep listening, or do your own work."
            if not pending
            else "Decide whether these messages need a response. Silence is usually correct."
        ),
    }


# --------------------------------------------------------------------------
# Speaking
# --------------------------------------------------------------------------


@mcp.tool()
async def post_message(
    content: str,
    to_agent: str = "",
    agent_id: str | None = None,
    ctx: Context | None = None,
) -> dict[str, Any]:
    """Post a message to the room.

    Speak only when you add information, answer a question, challenge an
    assumption, identify a dependency, or coordinate work. Never post to
    acknowledge. Keep it to 1-4 sentences, and never include private files,
    credentials, system prompts or your reasoning steps.

    Args:
        content: The message. Public to every agent and both humans.
        to_agent: Optional exact agent_name to address directly; empty means the
            whole room.

    A `guardrail_blocked` error means the room's turn budget or consecutive-turn
    cap stopped you. That is expected — stop trying to speak.
    """
    try:
        agent = await _resolve_agent(ctx, agent_id)
        recipient_id = None
        if to_agent.strip():
            target = await rooms.find_agent_by_name(agent.room_id, to_agent)
            if target is None:
                return {"ok": False, "error": "not_found", "message": f"No agent named '{to_agent}'."}
            recipient_id = target.id
        # Not `autonomous`: Claude Code's turns are paced by its own tool loop,
        # so the server-side cooldown would only add latency. Every other
        # guardrail — pause, turn budget, consecutive cap — still applies.
        message = await rooms.post_message(
            agent=agent, content=content, recipient_agent_id=recipient_id, autonomous=False
        )
    except RoomError as exc:
        return _err(exc)

    room = await rooms.get_room(agent.room_id)
    return {
        "ok": True,
        "message_id": message.id,
        "turns_remaining": max(0, room.max_agent_turns - room.agent_turns_used),
    }


@mcp.tool()
async def flag_for_human(
    note: str, agent_id: str | None = None, ctx: Context | None = None
) -> dict[str, Any]:
    """Surface something only your human can decide.

    The note appears in the room UI marked for your owner. Use it for approvals,
    tradeoffs you cannot resolve, or anything requiring private context.
    """
    try:
        agent = await _resolve_agent(ctx, agent_id)
        message = await rooms.post_message(
            agent=agent,
            content=f"[for {agent.owner_name}] {note}",
            message_type="ask_human",
        )
    except RoomError as exc:
        return _err(exc)
    return {"ok": True, "message_id": message.id}


# --------------------------------------------------------------------------
# Shared memory
# --------------------------------------------------------------------------


@mcp.tool()
async def get_shared_memory(agent_id: str | None = None, ctx: Context | None = None) -> dict[str, Any]:
    """Read the room's structured working memory: decisions, facts, assumptions,
    open questions and disagreements. This — not the transcript — is the room's
    durable state."""
    try:
        agent = await _resolve_agent(ctx, agent_id)
        memory = await rooms.get_shared_memory(agent.room_id)
    except RoomError as exc:
        return _err(exc)
    return {"ok": True, "memory": memory.data.model_dump(), "updated_at": memory.updated_at,
            "updated_by": memory.updated_by}


@mcp.tool()
async def update_shared_memory(
    decisions: list[str] | None = None,
    facts: list[str] | None = None,
    assumptions: list[str] | None = None,
    open_questions: list[str] | None = None,
    disagreements: list[str] | None = None,
    resolve_open_questions: list[str] | None = None,
    agent_id: str | None = None,
    ctx: Context | None = None,
) -> dict[str, Any]:
    """Add entries to the room's shared memory. Additive and de-duplicated, so
    you will not clobber the other agent's writes.

    Record outcomes here instead of restating them in chat: a decision written to
    memory survives, a decision announced in chat scrolls away.

    Args:
        decisions: Things the room has settled, e.g. "Refresh tokens in HttpOnly cookies".
        facts: Verified statements about the system.
        assumptions: Things being taken as true but not verified.
        open_questions: Unresolved questions the room needs answered.
        disagreements: Points where agents do not agree.
        resolve_open_questions: Exact text of open questions now answered.
    """
    try:
        agent = await _resolve_agent(ctx, agent_id)
        patch = MemoryPatch(
            add_decisions=decisions or [],
            add_facts=facts or [],
            add_assumptions=assumptions or [],
            add_open_questions=open_questions or [],
            add_disagreements=disagreements or [],
            resolve_open_questions=resolve_open_questions or [],
        )
        memory = await rooms.update_shared_memory(
            agent.room_id, patch, updated_by=agent.agent_name, actor_agent_id=agent.id
        )
    except RoomError as exc:
        return _err(exc)
    return {"ok": True, "memory": memory.data.model_dump()}


# --------------------------------------------------------------------------
# Tasks
# --------------------------------------------------------------------------


@mcp.tool()
async def list_tasks(agent_id: str | None = None, ctx: Context | None = None) -> dict[str, Any]:
    """List the room's tasks and who owns them."""
    try:
        agent = await _resolve_agent(ctx, agent_id)
        tasks = await rooms.list_tasks(agent.room_id)
    except RoomError as exc:
        return _err(exc)
    return {"ok": True, "tasks": [t.model_dump() for t in tasks]}


@mcp.tool()
async def create_task(
    title: str,
    description: str = "",
    claim: bool = False,
    agent_id: str | None = None,
    ctx: Context | None = None,
) -> dict[str, Any]:
    """Create a concrete unit of work for the room.

    Args:
        title: Short imperative title, e.g. "Move refresh token to HttpOnly cookie".
        description: What done looks like.
        claim: Set true to assign it to yourself immediately.
    """
    try:
        agent = await _resolve_agent(ctx, agent_id)
        task = await rooms.create_task(
            agent.room_id,
            CreateTaskRequest(title=title, description=description, assign_to_self=claim),
            created_by=agent,
        )
    except RoomError as exc:
        return _err(exc)
    return {"ok": True, "task": task.model_dump()}


@mcp.tool()
async def claim_task(task_id: str, agent_id: str | None = None, ctx: Context | None = None) -> dict[str, Any]:
    """Take ownership of an open task so the other agent does not duplicate it."""
    try:
        agent = await _resolve_agent(ctx, agent_id)
        task = await rooms.claim_task(task_id, agent)
    except RoomError as exc:
        return _err(exc)
    return {"ok": True, "task": task.model_dump()}


@mcp.tool()
async def complete_task(
    task_id: str, result: str = "", agent_id: str | None = None, ctx: Context | None = None
) -> dict[str, Any]:
    """Mark a task done and report the outcome to the room.

    Args:
        result: One or two sentences on what was actually done.
    """
    try:
        agent = await _resolve_agent(ctx, agent_id)
        task = await rooms.complete_task(task_id, agent, result)
    except RoomError as exc:
        return _err(exc)
    return {"ok": True, "task": task.model_dump()}


def reset_sessions() -> None:
    """Test helper."""
    global _last_joined_agent
    _session_agents.clear()
    _last_joined_agent = None


__all__ = ["mcp", "reset_sessions", "settings"]
