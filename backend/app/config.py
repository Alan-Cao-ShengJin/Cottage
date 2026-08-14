"""Runtime configuration.

Note what is absent: there is no model provider key, base URL, or model name. Agent
Rooms hosts coordination, not inference (ADR-006). If a provider credential appears
in this file, that is a design regression — see `CLAUDE.md`.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parents[2]

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


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    # --- storage -------------------------------------------------------
    database_path: Path = field(
        default_factory=lambda: Path(
            os.getenv("DATABASE_PATH", str(REPO_ROOT / "backend" / "data" / "agent_rooms.db"))
        )
    )

    # --- server --------------------------------------------------------
    host: str = field(default_factory=lambda: os.getenv("HOST", "127.0.0.1"))
    port: int = field(default_factory=lambda: _int("PORT", 8000))
    public_base_url: str = field(
        default_factory=lambda: os.getenv("PUBLIC_BASE_URL", "http://localhost:8000")
    )
    cors_origins: tuple[str, ...] = field(
        default_factory=lambda: tuple(
            o.strip()
            for o in os.getenv("CORS_ORIGINS", "http://localhost:3000,http://127.0.0.1:3000").split(
                ","
            )
            if o.strip()
        )
    )

    # --- realtime ------------------------------------------------------
    #: Server-assigned heartbeat cadence handed to every connection.
    heartbeat_interval_seconds: int = field(
        default_factory=lambda: _int("HEARTBEAT_INTERVAL_SECONDS", 20)
    )
    #: SSE comment frame cadence; keeps proxies and idle sockets alive.
    sse_keepalive_seconds: int = field(default_factory=lambda: _int("SSE_KEEPALIVE_SECONDS", 15))
    #: Ceiling on an MCP `await_events` block. Must stay under typical client
    #: request timeouts, so a poll returns empty rather than failing.
    max_long_poll_seconds: int = field(default_factory=lambda: _int("MAX_LONG_POLL_SECONDS", 25))
    #: How often the reaper expires leases, stales work, and closes dead
    #: connections. Correctness does not depend on this firing on time — expiry is
    #: also enforced on read — but latency to reclaim does.
    reaper_interval_seconds: int = field(
        default_factory=lambda: _int("REAPER_INTERVAL_SECONDS", 10)
    )
    #: Cap on concurrent event streams per participant, to bound fanout cost.
    max_connections_per_participant: int = field(
        default_factory=lambda: _int("MAX_CONNECTIONS_PER_PARTICIPANT", 8)
    )

    # --- rooms ---------------------------------------------------------
    default_room_ttl_seconds: int = field(
        default_factory=lambda: _int("DEFAULT_ROOM_TTL_SECONDS", 7 * 24 * 3600)
    )
    default_lease_seconds: int = field(default_factory=lambda: _int("DEFAULT_LEASE_SECONDS", 900))
    max_lease_seconds: int = field(default_factory=lambda: _int("MAX_LEASE_SECONDS", 3600))

    # --- dev conveniences ----------------------------------------------
    #: Seeds a demo org/user/token at boot so the slice is runnable immediately.
    #: Never enable in a deployed environment; real identity is M5.
    dev_bootstrap: bool = field(default_factory=lambda: _bool("DEV_BOOTSTRAP", True))
    dev_bootstrap_token: str = field(
        default_factory=lambda: os.getenv("DEV_BOOTSTRAP_TOKEN", "dev-owner-token")
    )


settings = Settings()
