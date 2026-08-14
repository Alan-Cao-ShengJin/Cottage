"""Safeguards against runaway agent-to-agent conversations.

Every agent-authored message passes through `check_turn`, whether it arrives via
REST, MCP or the server-side GPT loop. Enforcing this in the room service (not
in the agent prompt) means a misbehaving or adversarial client cannot spin the
room forever.

Budgets are per-room and reset only when a human asks for it.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from ..config import settings


@dataclass(frozen=True)
class GuardrailVerdict:
    allowed: bool
    reason: str = ""

    def __bool__(self) -> bool:  # convenience in `if verdict:`
        return self.allowed


ALLOWED = GuardrailVerdict(True)

# agent_id -> monotonic timestamp of that agent's last accepted turn.
_last_turn_at: dict[str, float] = {}


@dataclass(frozen=True)
class RoomTurnState:
    """The slice of room state the guardrails care about."""

    status: str
    autonomy_enabled: bool
    agent_turns_used: int
    last_speaker_id: str | None
    consecutive_turns: int
    seconds_remaining: int


def check_turn(state: RoomTurnState, agent_id: str, *, autonomous: bool) -> GuardrailVerdict:
    """Decide whether `agent_id` may take a turn in this room right now.

    `autonomous` marks a turn produced by our own wake-decide loop. Those are
    additionally rate-limited, because only they can fire back-to-back with no
    natural latency. Every other limit applies to every agent turn, whichever
    entry point it came through.
    """
    if state.status != "active" or state.seconds_remaining <= 0:
        return GuardrailVerdict(False, "Room is expired or closed; no new messages are accepted.")

    if not state.autonomy_enabled:
        return GuardrailVerdict(
            False, "Agent collaboration is paused for this room. A human must resume it."
        )

    if state.agent_turns_used >= settings.max_room_agent_turns:
        return GuardrailVerdict(
            False,
            f"Room turn budget exhausted ({settings.max_room_agent_turns} agent turns). "
            "A human must reset the budget before agents can speak again.",
        )

    if (
        state.last_speaker_id == agent_id
        and state.consecutive_turns >= settings.max_consecutive_turns_per_agent
    ):
        return GuardrailVerdict(
            False,
            f"You have spoken {state.consecutive_turns} times in a row "
            f"(limit {settings.max_consecutive_turns_per_agent}). Wait for another agent or a human.",
        )

    if autonomous:
        last = _last_turn_at.get(agent_id)
        if last is not None:
            elapsed = time.monotonic() - last
            if elapsed < settings.agent_cooldown_seconds:
                wait = settings.agent_cooldown_seconds - elapsed
                return GuardrailVerdict(False, f"Cooldown active; {wait:.1f}s remaining.")

    return ALLOWED


def check_relevance(relevance: float) -> GuardrailVerdict:
    """Force silence when the agent itself rates the turn as low-value."""
    if relevance < settings.min_response_relevance:
        return GuardrailVerdict(
            False,
            f"Self-rated relevance {relevance:.2f} is below the "
            f"{settings.min_response_relevance:.2f} threshold; staying silent.",
        )
    return ALLOWED


def record_turn(agent_id: str) -> None:
    _last_turn_at[agent_id] = time.monotonic()


def clear_agent(agent_id: str) -> None:
    _last_turn_at.pop(agent_id, None)


def reset() -> None:
    """Test helper."""
    _last_turn_at.clear()
