"""Runtime configuration. All secrets come from the environment (see .env.example)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parents[2]

# Load .env from the repo root, then backend/.env (the latter wins for local overrides).
load_dotenv(REPO_ROOT / ".env")
load_dotenv(REPO_ROOT / "backend" / ".env", override=True)


def _int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw)
    except ValueError:
        return default


@dataclass(frozen=True)
class Settings:
    # --- storage -------------------------------------------------------
    database_path: Path = field(
        default_factory=lambda: Path(
            os.getenv("DATABASE_PATH", str(REPO_ROOT / "backend" / "data" / "agent_room.db"))
        )
    )

    # --- server --------------------------------------------------------
    host: str = field(default_factory=lambda: os.getenv("HOST", "127.0.0.1"))
    port: int = field(default_factory=lambda: _int("PORT", 8000))
    cors_origins: tuple[str, ...] = field(
        default_factory=lambda: tuple(
            o.strip()
            for o in os.getenv("CORS_ORIGINS", "http://localhost:3000,http://127.0.0.1:3000").split(",")
            if o.strip()
        )
    )
    public_base_url: str = field(
        default_factory=lambda: os.getenv("PUBLIC_BASE_URL", "http://localhost:8000")
    )

    # --- OpenAI --------------------------------------------------------
    openai_api_key: str | None = field(default_factory=lambda: os.getenv("OPENAI_API_KEY") or None)
    openai_model: str = field(default_factory=lambda: os.getenv("OPENAI_MODEL", "gpt-4o-mini"))
    openai_base_url: str | None = field(default_factory=lambda: os.getenv("OPENAI_BASE_URL") or None)

    # --- room lifetime -------------------------------------------------
    room_ttl_seconds: int = field(default_factory=lambda: _int("ROOM_TTL_SECONDS", 2 * 60 * 60))

    # --- runaway-conversation guardrails -------------------------------
    # Total autonomous agent turns allowed in a room before autonomy halts.
    max_room_agent_turns: int = field(default_factory=lambda: _int("MAX_ROOM_AGENT_TURNS", 12))
    # How many times in a row a single agent may speak without another agent speaking.
    max_consecutive_turns_per_agent: int = field(
        default_factory=lambda: _int("MAX_CONSECUTIVE_TURNS_PER_AGENT", 2)
    )
    # Minimum seconds between two autonomous turns by the same agent.
    agent_cooldown_seconds: float = field(default_factory=lambda: _float("AGENT_COOLDOWN_SECONDS", 4.0))
    # Model-reported relevance below this threshold is forced to IGNORE.
    min_response_relevance: float = field(default_factory=lambda: _float("MIN_RESPONSE_RELEVANCE", 0.55))
    # How much room transcript an agent sees per turn.
    agent_context_messages: int = field(default_factory=lambda: _int("AGENT_CONTEXT_MESSAGES", 25))

    @property
    def openai_enabled(self) -> bool:
        return bool(self.openai_api_key)


settings = Settings()
