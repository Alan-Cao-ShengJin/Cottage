"""The `RoomAgent` abstraction.

A RoomAgent is a server-side participant that can be woken by a room event and
decide, on its own, whether to act. `GptRoomAgent` is the only concrete
implementation in V0; Claude Code participates through MCP instead (it cannot be
woken remotely — see docs/CLAUDE_CODE.md).

The context an agent receives is assembled by `build_context` and contains
public room state only. Private owner instructions live on the agent object and
never enter the room.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from ..config import settings
from ..errors import GuardrailBlocked, RoomError
from ..events import RoomEvent
from ..models import (
    Agent,
    AgentAction,
    CreateTaskRequest,
    MemoryPatch,
    Message,
    Room,
    SharedMemoryData,
    Task,
)
from ..services import rooms

log = logging.getLogger(__name__)


@dataclass
class RoomContext:
    """Everything an agent is allowed to see. Public state only."""

    room: Room
    self_agent: Agent
    others: list[Agent]
    messages: list[Message]
    memory: SharedMemoryData
    tasks: list[Task]
    turns_remaining: int


@dataclass
class Decision:
    action: AgentAction = "IGNORE"
    relevance: float = 0.0
    message: str = ""
    recipient_agent_name: str | None = None
    memory: MemoryPatch = field(default_factory=MemoryPatch)
    task_title: str = ""
    task_description: str = ""
    note_to_human: str = ""
    reason: str = ""  # short, room-safe rationale for logs/UI. Not chain-of-thought.


async def build_context(agent: Agent) -> RoomContext:
    room = await rooms.get_room(agent.room_id)
    members = await rooms.get_members(room.id)
    memory = await rooms.get_shared_memory(room.id)
    return RoomContext(
        room=room,
        self_agent=agent,
        others=[m for m in members if m.id != agent.id],
        messages=await rooms.recent_messages(room.id, settings.agent_context_messages),
        memory=memory.data,
        tasks=await rooms.list_tasks(room.id),
        turns_remaining=max(0, room.max_agent_turns - room.agent_turns_used),
    )


class RoomAgent(ABC):
    """Base class for a server-driven room participant."""

    def __init__(self, agent: Agent, private_instructions: str = "") -> None:
        self.agent = agent
        self.private_instructions = private_instructions

    @property
    def agent_id(self) -> str:
        return self.agent.id

    @property
    def room_id(self) -> str:
        return self.agent.room_id

    @property
    def public_objective(self) -> str:
        return self.agent.public_objective

    # -- to implement ---------------------------------------------------
    @abstractmethod
    async def decide_action(self, context: RoomContext, trigger: RoomEvent) -> Decision:
        """Choose one action given public room context. Must not leak private state."""

    # -- shared machinery -----------------------------------------------
    async def handle_event(self, trigger: RoomEvent) -> Decision:
        """Wake, decide, and carry out the decision. Returns what was decided."""
        self.agent = await rooms.get_agent(self.agent.id)
        if self.agent.status != "active":
            return Decision(action="IGNORE", reason="agent has left the room")

        context = await build_context(self.agent)
        if context.room.status != "active":
            return Decision(action="IGNORE", reason="room is not active")

        allowed, reason = await rooms.can_agent_speak(self.agent, autonomous=True)
        speaking_actions = {"RESPOND", "ASK_AGENT"}

        decision = await self.decide_action(context, trigger)
        log.info(
            "agent=%s action=%s relevance=%.2f reason=%s",
            self.agent.agent_name,
            decision.action,
            decision.relevance,
            decision.reason[:120],
        )

        if decision.action in speaking_actions and not allowed:
            log.info("agent=%s wanted to speak but guardrail said: %s", self.agent.agent_name, reason)
            return Decision(action="IGNORE", relevance=decision.relevance, reason=f"blocked: {reason}")

        await self._apply(decision, context)
        return decision

    async def _apply(self, decision: Decision, context: RoomContext) -> None:
        from ..services import guardrails

        try:
            if decision.action in ("RESPOND", "ASK_AGENT"):
                verdict = guardrails.check_relevance(decision.relevance)
                if not verdict.allowed:
                    log.info("agent=%s stayed silent: %s", self.agent.agent_name, verdict.reason)
                    return
                if not decision.message.strip():
                    return
                await self.post(
                    decision.message,
                    recipient_agent_name=decision.recipient_agent_name,
                )

            elif decision.action == "UPDATE_MEMORY":
                await self.update_memory(decision.memory)
                if decision.message.strip() and decision.relevance >= settings.min_response_relevance:
                    await self.post(decision.message)

            elif decision.action == "CREATE_TASK":
                if decision.task_title.strip():
                    await rooms.create_task(
                        self.room_id,
                        CreateTaskRequest(
                            title=decision.task_title, description=decision.task_description
                        ),
                        created_by=self.agent,
                    )

            elif decision.action == "ASK_HUMAN":
                note = decision.note_to_human.strip() or decision.message.strip()
                if note:
                    await rooms.post_message(
                        agent=self.agent,
                        content=f"[for {self.agent.owner_name}] {note}",
                        message_type="ask_human",
                    )

            elif decision.action == "LEAVE":
                await rooms.leave_room(self.agent.id, decision.reason or "objective complete")

        except GuardrailBlocked as exc:
            log.info("guardrail stopped %s: %s", self.agent.agent_name, exc.message)
        except RoomError as exc:
            log.warning("room rejected action from %s: %s", self.agent.agent_name, exc.message)

    async def post(
        self, content: str, recipient_agent_name: str | None = None, message_type: str = "chat"
    ) -> Message | None:
        recipient_id: str | None = None
        if recipient_agent_name:
            target = await rooms.find_agent_by_name(self.room_id, recipient_agent_name)
            recipient_id = target.id if target else None
        return await rooms.post_message(
            agent=self.agent,
            content=content,
            recipient_agent_id=recipient_id,
            message_type=message_type,  # type: ignore[arg-type]
            autonomous=True,
        )

    async def update_memory(self, patch: MemoryPatch) -> None:
        if not patch.model_dump(exclude_defaults=True):
            return
        await rooms.update_shared_memory(self.room_id, patch, updated_by=self.agent.agent_name)

    def describe(self) -> dict[str, Any]:
        return {
            "agent_id": self.agent_id,
            "agent_name": self.agent.agent_name,
            "room_id": self.room_id,
            "public_objective": self.public_objective,
        }
