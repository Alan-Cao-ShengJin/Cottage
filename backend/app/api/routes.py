"""REST + SSE surface.

Two audiences:
  /api/rooms/*   -> the browser (human-facing operations)
  /api/agent/*   -> any HTTP agent, authenticated with the token it got on join

Claude Code does not use these routes; it talks MCP (app/mcp_server.py), which
calls the same room service underneath.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, Query, Request
from fastapi.responses import StreamingResponse

from ..agents import runner as agent_runner
from ..config import settings
from ..errors import Forbidden
from ..events import hub
from ..models import (
    Agent,
    AutonomyRequest,
    CompleteTaskRequest,
    CreateRoomRequest,
    CreateTaskRequest,
    HumanMessageRequest,
    JoinRoomRequest,
    JoinRoomResponse,
    MemoryPatch,
    Message,
    PostMessageRequest,
    Room,
    RoomSnapshot,
    SharedMemory,
    SpawnGptAgentRequest,
    Task,
)
from ..services import rooms

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api")

SSE_HEARTBEAT_SECONDS = 15.0


# --------------------------------------------------------------------------
# Meta
# --------------------------------------------------------------------------


@router.get("/health")
async def health() -> dict[str, Any]:
    return {"status": "ok"}


@router.get("/config")
async def get_config() -> dict[str, Any]:
    """Limits the UI needs to render, plus whether the GPT agent is available."""
    return {
        "openai_enabled": settings.openai_enabled,
        "openai_model": settings.openai_model if settings.openai_enabled else None,
        "room_ttl_seconds": settings.room_ttl_seconds,
        "max_room_agent_turns": settings.max_room_agent_turns,
        "max_consecutive_turns_per_agent": settings.max_consecutive_turns_per_agent,
        "min_response_relevance": settings.min_response_relevance,
        "mcp_url": f"{settings.public_base_url.rstrip('/')}/mcp",
    }


# --------------------------------------------------------------------------
# Rooms (browser)
# --------------------------------------------------------------------------


@router.post("/rooms", response_model=Room, status_code=201)
async def create_room(req: CreateRoomRequest) -> Room:
    return await rooms.create_room(req)


@router.get("/rooms", response_model=list[Room])
async def list_rooms(limit: int = Query(default=25, ge=1, le=100)) -> list[Room]:
    return await rooms.list_rooms(limit)


@router.get("/rooms/{code}", response_model=RoomSnapshot)
async def get_room(code: str) -> RoomSnapshot:
    return await rooms.get_snapshot(code)


@router.post("/rooms/{code}/join", response_model=JoinRoomResponse, status_code=201)
async def join_room(code: str, req: JoinRoomRequest) -> JoinRoomResponse:
    return await rooms.join_room(req.model_copy(update={"join_code": code}))


@router.post("/rooms/{code}/messages", response_model=Message, status_code=201)
async def post_human_message(code: str, req: HumanMessageRequest) -> Message:
    return await rooms.post_human_message(
        code, req.content, sender_label=req.sender_label, recipient_agent_id=req.recipient_agent_id
    )


@router.post("/rooms/{code}/autonomy", response_model=Room)
async def set_autonomy(code: str, req: AutonomyRequest) -> Room:
    room = await rooms.set_autonomy(code, req.enabled)
    if req.enabled:
        # Give the server-side agents one nudge so collaboration actually starts.
        for agent in agent_runner.runner.agents_in_room(room.id):
            await agent_runner.runner.wake(agent.agent_id, reason="autonomy enabled")
    return room


@router.post("/rooms/{code}/reset-turns", response_model=Room)
async def reset_turns(code: str) -> Room:
    room = await rooms.reset_turn_budget(code)
    agent_runner.runner.reset_wake_budget(room.id)
    return room


@router.post("/rooms/{code}/expire", response_model=Room)
async def expire_room(code: str) -> Room:
    """Force expiry now. Useful for demoing the temporary-room behaviour."""
    room = await rooms.get_room(code)
    await rooms.set_expiry_now(room.id)
    return await rooms.get_room(room.id)


@router.delete("/rooms/{code}")
async def purge_room(code: str) -> dict[str, Any]:
    """Hard-delete a room and everything in it."""
    await rooms.purge_room(code)
    return {"ok": True}


@router.post("/rooms/{code}/gpt-agent", response_model=Agent, status_code=201)
async def spawn_gpt_agent(code: str, req: SpawnGptAgentRequest) -> Agent:
    return await agent_runner.spawn_gpt_agent(code, req)


@router.get("/rooms/{code}/tasks", response_model=list[Task])
async def list_tasks(code: str) -> list[Task]:
    return await rooms.list_tasks(code)


@router.get("/rooms/{code}/memory", response_model=SharedMemory)
async def get_memory(code: str) -> SharedMemory:
    return await rooms.get_shared_memory(code)


@router.get("/rooms/{code}/messages", response_model=list[Message])
async def list_messages(
    code: str,
    since_id: int = Query(default=0, ge=0),
    limit: int = Query(default=100, ge=1, le=500),
) -> list[Message]:
    return await rooms.read_messages(code, since_id=since_id, limit=limit)


# --------------------------------------------------------------------------
# Live stream
# --------------------------------------------------------------------------


@router.get("/rooms/{code}/events")
async def stream_events(code: str, request: Request) -> StreamingResponse:
    """SSE stream: one `snapshot` frame, then incremental room events."""
    room = await rooms.get_room(code)

    async def event_source():
        snapshot = await rooms.get_snapshot(room.id)
        yield _sse("snapshot", snapshot.model_dump())

        async with hub.subscription(room.id) as queue:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=SSE_HEARTBEAT_SECONDS)
                except asyncio.TimeoutError:
                    # Comment frame keeps proxies and idle connections alive.
                    yield ": keepalive\n\n"
                    continue
                yield _sse(event.type, event.as_sse())

    return StreamingResponse(
        event_source(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


def _sse(event_type: str, data: dict[str, Any]) -> str:
    return f"event: {event_type}\ndata: {json.dumps(data, default=str)}\n\n"


# --------------------------------------------------------------------------
# Agent-token surface
# --------------------------------------------------------------------------


async def current_agent(
    authorization: Annotated[str | None, Header()] = None,
    x_agent_token: Annotated[str | None, Header()] = None,
) -> Agent:
    token = x_agent_token
    if not token and authorization and authorization.lower().startswith("bearer "):
        token = authorization[7:].strip()
    if not token:
        raise Forbidden("Missing agent token. Send it as `Authorization: Bearer <token>`.")
    return await rooms.authenticate_agent(token)


AgentDep = Annotated[Agent, Depends(current_agent)]


@router.get("/agent/me", response_model=Agent)
async def agent_me(agent: AgentDep) -> Agent:
    return agent


@router.get("/agent/room", response_model=RoomSnapshot)
async def agent_room(agent: AgentDep) -> RoomSnapshot:
    return await rooms.get_snapshot(agent.room_id)


@router.post("/agent/messages", response_model=Message, status_code=201)
async def agent_post_message(agent: AgentDep, req: PostMessageRequest) -> Message:
    return await rooms.post_message(
        agent=agent,
        content=req.content,
        recipient_agent_id=req.recipient_agent_id,
        message_type=req.message_type,
    )


@router.post("/agent/leave", response_model=Agent)
async def agent_leave(agent: AgentDep) -> Agent:
    agent_runner.runner.unregister(agent.id)
    return await rooms.leave_room(agent.id)


@router.post("/agent/memory", response_model=SharedMemory)
async def agent_update_memory(agent: AgentDep, patch: MemoryPatch) -> SharedMemory:
    return await rooms.update_shared_memory(
        agent.room_id, patch, updated_by=agent.agent_name, actor_agent_id=agent.id
    )


@router.post("/agent/tasks", response_model=Task, status_code=201)
async def agent_create_task(agent: AgentDep, req: CreateTaskRequest) -> Task:
    return await rooms.create_task(agent.room_id, req, created_by=agent)


@router.post("/agent/tasks/{task_id}/claim", response_model=Task)
async def agent_claim_task(agent: AgentDep, task_id: str) -> Task:
    return await rooms.claim_task(task_id, agent)


@router.post("/agent/tasks/{task_id}/complete", response_model=Task)
async def agent_complete_task(agent: AgentDep, task_id: str, req: CompleteTaskRequest) -> Task:
    return await rooms.complete_task(task_id, agent, req.result)
