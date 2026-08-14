"""OpenAI-backed room agent.

One model call per wake-up. The model returns a structured decision (not free
text), which keeps the action space enforceable on our side: we validate the
relevance score and re-check guardrails before anything is posted.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from openai import AsyncOpenAI, OpenAIError

from ..config import settings
from ..errors import ConfigError
from ..events import RoomEvent
from ..models import MemoryPatch
from .base import Decision, RoomAgent, RoomContext
from .prompts import build_gpt_system_prompt

log = logging.getLogger(__name__)

_client: AsyncOpenAI | None = None


def get_client() -> AsyncOpenAI:
    global _client
    if not settings.openai_enabled:
        raise ConfigError("OPENAI_API_KEY is not set; the GPT agent cannot run.")
    if _client is None:
        _client = AsyncOpenAI(
            api_key=settings.openai_api_key,
            base_url=settings.openai_base_url,
            timeout=45.0,
            max_retries=2,
        )
    return _client


DECISION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "action": {
            "type": "string",
            "enum": [
                "IGNORE",
                "RESPOND",
                "ASK_AGENT",
                "UPDATE_MEMORY",
                "CREATE_TASK",
                "ASK_HUMAN",
                "LEAVE",
            ],
        },
        "relevance": {
            "type": "number",
            "description": "0.0-1.0: how much this action advances the room objective.",
        },
        "message": {
            "type": "string",
            "description": "Room-visible message. 1-4 sentences. Empty when action is IGNORE.",
        },
        "recipient_agent_name": {
            "type": "string",
            "description": "Exact agent_name to address, or empty string for the whole room.",
        },
        "memory_add_decisions": {"type": "array", "items": {"type": "string"}},
        "memory_add_facts": {"type": "array", "items": {"type": "string"}},
        "memory_add_assumptions": {"type": "array", "items": {"type": "string"}},
        "memory_add_open_questions": {"type": "array", "items": {"type": "string"}},
        "memory_resolve_open_questions": {"type": "array", "items": {"type": "string"}},
        "task_title": {"type": "string"},
        "task_description": {"type": "string"},
        "note_to_human": {"type": "string"},
        "reason": {
            "type": "string",
            "description": "One short room-safe sentence explaining the choice. Not reasoning steps.",
        },
    },
    "required": [
        "action",
        "relevance",
        "message",
        "recipient_agent_name",
        "memory_add_decisions",
        "memory_add_facts",
        "memory_add_assumptions",
        "memory_add_open_questions",
        "memory_resolve_open_questions",
        "task_title",
        "task_description",
        "note_to_human",
        "reason",
    ],
}


def render_context(context: RoomContext, trigger: RoomEvent) -> str:
    """Serialize public room state for the model. Nothing private goes in here."""
    lines: list[str] = []
    lines.append(f"ROOM {context.room.join_code} — {context.room.title}")
    lines.append(f"Room objective: {context.room.objective}")
    lines.append(
        f"Turns remaining in this room: {context.turns_remaining} "
        f"(of {context.room.max_agent_turns}). Room expires in "
        f"{context.room.seconds_remaining // 60} minutes."
    )

    lines.append("\nAGENTS PRESENT")
    lines.append(
        f"- {context.self_agent.agent_name} (you, owner {context.self_agent.owner_name}): "
        f"{context.self_agent.public_objective or 'no stated objective'}"
    )
    for other in context.others:
        state = "" if other.status == "active" else " [left the room]"
        lines.append(
            f"- {other.agent_name} (owner {other.owner_name}, {other.provider}){state}: "
            f"{other.public_objective or 'no stated objective'}"
        )

    memory = context.memory
    lines.append("\nSHARED MEMORY")
    for label, items in (
        ("Decisions", memory.decisions),
        ("Facts", memory.facts),
        ("Assumptions", memory.assumptions),
        ("Open questions", memory.open_questions),
        ("Disagreements", memory.disagreements),
    ):
        lines.append(f"{label}: " + ("; ".join(items) if items else "(none)"))

    if context.tasks:
        lines.append("\nTASKS")
        for task in context.tasks:
            owner = task.assigned_agent_id or "unassigned"
            lines.append(f"- [{task.status}] {task.title} ({owner})")

    lines.append("\nRECENT ROOM MESSAGES (oldest first)")
    if not context.messages:
        lines.append("(the room is empty)")
    for msg in context.messages:
        who = "you" if msg.agent_id == context.self_agent.id else msg.sender_label
        to = ""
        if msg.recipient_agent_id:
            to = " (direct)" if msg.recipient_agent_id == context.self_agent.id else " (to another agent)"
        lines.append(f"[{msg.message_type}] {who}{to}: {msg.content}")

    lines.append(f"\nYOU WERE WOKEN BY: {trigger.type}")
    lines.append(
        "Decide your single next action. Default to IGNORE unless you can add real substance."
    )
    return "\n".join(lines)


def _to_decision(raw: dict[str, Any]) -> Decision:
    recipient = (raw.get("recipient_agent_name") or "").strip()
    patch = MemoryPatch(
        add_decisions=raw.get("memory_add_decisions") or [],
        add_facts=raw.get("memory_add_facts") or [],
        add_assumptions=raw.get("memory_add_assumptions") or [],
        add_open_questions=raw.get("memory_add_open_questions") or [],
        resolve_open_questions=raw.get("memory_resolve_open_questions") or [],
    )
    try:
        relevance = float(raw.get("relevance", 0.0))
    except (TypeError, ValueError):
        relevance = 0.0
    return Decision(
        action=raw.get("action", "IGNORE"),  # type: ignore[arg-type]
        relevance=max(0.0, min(1.0, relevance)),
        message=(raw.get("message") or "").strip(),
        recipient_agent_name=recipient or None,
        memory=patch,
        task_title=(raw.get("task_title") or "").strip(),
        task_description=(raw.get("task_description") or "").strip(),
        note_to_human=(raw.get("note_to_human") or "").strip(),
        reason=(raw.get("reason") or "").strip(),
    )


class GptRoomAgent(RoomAgent):
    """A room participant driven by the OpenAI API."""

    provider = "openai"

    async def decide_action(self, context: RoomContext, trigger: RoomEvent) -> Decision:
        system_prompt = build_gpt_system_prompt(
            agent_name=self.agent.agent_name,
            owner_name=self.agent.owner_name,
            public_objective=self.agent.public_objective,
            room_objective=context.room.objective,
            private_instructions=self.private_instructions,
        )
        user_prompt = render_context(context, trigger)

        try:
            response = await get_client().chat.completions.create(
                model=settings.openai_model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                response_format={
                    "type": "json_schema",
                    "json_schema": {
                        "name": "room_decision",
                        "strict": True,
                        "schema": DECISION_SCHEMA,
                    },
                },
                temperature=0.4,
                max_tokens=600,
            )
        except OpenAIError as exc:
            log.error("OpenAI call failed for %s: %s", self.agent.agent_name, exc)
            return Decision(action="IGNORE", reason=f"model call failed: {exc}")

        content = response.choices[0].message.content or "{}"
        try:
            raw = json.loads(content)
        except json.JSONDecodeError:
            log.error("model returned non-JSON for %s: %s", self.agent.agent_name, content[:200])
            return Decision(action="IGNORE", reason="model returned malformed output")

        return _to_decision(raw)
