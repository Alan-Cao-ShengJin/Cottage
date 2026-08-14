"""Wakes server-side agents when something happens in their room.

This is the "autonomous" half of the product: no human prompts an individual
message. A room event arrives, every registered agent in that room (except the
one that caused it) is woken, and each decides for itself whether to act.

Wake-ups are debounced per agent, so a burst of events costs one model call, and
every action still passes through the room-service guardrails.

Scope note: only agents we drive ourselves live here. Claude Code is not
remotely wakeable, so it polls instead — see app/mcp_server.py.
"""

from __future__ import annotations

import asyncio
import logging

from ..config import settings
from ..errors import RoomError
from ..events import RoomEvent, hub
from ..models import Agent, JoinRoomRequest, SpawnGptAgentRequest
from ..services import rooms
from .base import RoomAgent
from .gpt import GptRoomAgent

log = logging.getLogger(__name__)

# Events that are worth a model call. `room_updated` is deliberately absent:
# turn-counter changes must not wake anyone, or agents would wake each other
# forever without any new content.
WAKE_EVENTS = {"message", "agent_joined", "agent_left", "memory_updated", "task_updated"}

DEBOUNCE_SECONDS = 1.0

# Backstop against wake storms. Speaking is already capped by the room turn
# budget, but memory/task updates are not, so cap total model calls per agent.
MAX_WAKES_PER_AGENT = 40


class AgentRunner:
    def __init__(self) -> None:
        self._agents: dict[str, RoomAgent] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._rerun: set[str] = set()
        self._wake_counts: dict[str, int] = {}
        self._started = False

    # -- lifecycle -------------------------------------------------------
    def start(self) -> None:
        if not self._started:
            hub.add_listener(self.on_event)
            self._started = True
            log.info("agent runner attached to event hub")

    async def shutdown(self) -> None:
        for task in list(self._tasks.values()):
            task.cancel()
        self._tasks.clear()
        self._agents.clear()

    # -- registry --------------------------------------------------------
    def register(self, agent: RoomAgent) -> None:
        self._agents[agent.agent_id] = agent
        log.info("registered autonomous agent %s (%s)", agent.agent.agent_name, agent.agent_id)

    def unregister(self, agent_id: str) -> None:
        self._agents.pop(agent_id, None)
        self._wake_counts.pop(agent_id, None)
        task = self._tasks.pop(agent_id, None)
        if task:
            task.cancel()

    def reset_wake_budget(self, room_id: str) -> None:
        """Paired with a human resetting the room turn budget."""
        for agent in self.agents_in_room(room_id):
            self._wake_counts.pop(agent.agent_id, None)

    def is_registered(self, agent_id: str) -> bool:
        return agent_id in self._agents

    def agents_in_room(self, room_id: str) -> list[RoomAgent]:
        return [a for a in self._agents.values() if a.room_id == room_id]

    # -- event handling ---------------------------------------------------
    async def on_event(self, event: RoomEvent) -> None:
        if event.type not in WAKE_EVENTS:
            return

        source_agent_id = _event_source_agent(event)
        for agent in self.agents_in_room(event.room_id):
            if agent.agent_id == source_agent_id:
                continue  # never wake an agent with its own action
            self._schedule(agent, event)

    def _schedule(self, agent: RoomAgent, event: RoomEvent) -> None:
        wakes = self._wake_counts.get(agent.agent_id, 0)
        if wakes >= MAX_WAKES_PER_AGENT:
            log.warning(
                "wake cap reached for %s (%d); pausing its autonomous loop",
                agent.agent.agent_name,
                wakes,
            )
            return
        self._wake_counts[agent.agent_id] = wakes + 1

        existing = self._tasks.get(agent.agent_id)
        if existing and not existing.done():
            # A wake is already in flight; coalesce into one follow-up pass.
            self._rerun.add(agent.agent_id)
            return
        self._tasks[agent.agent_id] = asyncio.create_task(self._run(agent, event))

    async def _run(self, agent: RoomAgent, event: RoomEvent) -> None:
        try:
            # Small debounce so a burst of related events becomes one turn.
            await asyncio.sleep(DEBOUNCE_SECONDS)
            await agent.handle_event(event)
        except asyncio.CancelledError:
            raise
        except RoomError as exc:
            log.info("agent %s could not act: %s", agent.agent_id, exc)
        except Exception:  # pragma: no cover - defensive
            log.exception("autonomous turn failed for %s", agent.agent_id)
        finally:
            self._tasks.pop(agent.agent_id, None)
            if agent.agent_id in self._rerun:
                self._rerun.discard(agent.agent_id)
                if agent.agent_id in self._agents:
                    self._tasks[agent.agent_id] = asyncio.create_task(self._run(agent, event))

    async def wake(self, agent_id: str, reason: str = "manual") -> None:
        """Force a wake-up (used right after a GPT agent joins)."""
        agent = self._agents.get(agent_id)
        if agent is None:
            raise RoomError(f"Agent '{agent_id}' is not a server-driven agent.")
        self._schedule(agent, RoomEvent(room_id=agent.room_id, type="message", payload={"reason": reason}))


def _event_source_agent(event: RoomEvent) -> str | None:
    """Who caused this event. Used to stop an agent waking on its own action —
    which matters most for memory/task updates, since those do not consume the
    turn budget and would otherwise let one agent loop against itself."""
    actor = event.payload.get("actor_agent_id")
    if isinstance(actor, str):
        return actor
    message = event.payload.get("message")
    if isinstance(message, dict):
        return message.get("agent_id")
    agent = event.payload.get("agent")
    if isinstance(agent, dict):
        return agent.get("id")
    return None


runner = AgentRunner()


async def spawn_gpt_agent(join_code: str, req: SpawnGptAgentRequest) -> Agent:
    """Join a room as an OpenAI-driven agent and start its autonomous loop."""
    if not settings.openai_enabled:
        from ..errors import ConfigError

        raise ConfigError("OPENAI_API_KEY is not set. Add it to .env to spawn the GPT agent.")

    result = await rooms.join_room(
        JoinRoomRequest(
            join_code=join_code,
            owner_name=req.owner_name,
            agent_name=req.agent_name,
            provider="openai",
            public_objective=req.public_objective,
            autonomous=True,
        )
    )
    gpt_agent = GptRoomAgent(result.agent, private_instructions=req.private_instructions)
    runner.register(gpt_agent)
    # Kick it once so it can open the conversation rather than waiting to be spoken to.
    await runner.wake(result.agent.id, reason="joined room")
    return result.agent
