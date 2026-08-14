"""Runtime configuration.

Note what is absent: there is no model provider key, base URL, or model name. Agent
Rooms hosts coordination, not inference (ADR-006). If a provider credential appears
in this file, that is a design regression — see `CLAUDE.md`.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

from dotenv import load_dotenv

#: Hosts that mean "only this machine can reach me". An empty host counts: a
#: `PUBLIC_BASE_URL` with no host is not a public deployment, it is a misconfiguration.
LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "0.0.0.0", "::1", ""})

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

    #: Statically exported room console, served from this same origin when present.
    #: One origin means no CORS to misconfigure and one deployment to keep in sync; when
    #: the directory is absent (a plain `uvicorn` dev run) the API simply serves alone and
    #: `npm run dev` supplies the console on :3000 instead.
    console_dir: Path = field(
        default_factory=lambda: Path(os.getenv("CONSOLE_DIR", str(REPO_ROOT / "frontend" / "out")))
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

    # --- MCP transport auth --------------------------------------------
    #: Require an OAuth access token on /mcp. Off by default so a local agent can attach
    #: with no ceremony; the startup guard refuses public exposure while it is off, so
    #: this switch cannot quietly leave a reachable endpoint open.
    mcp_require_auth: bool = field(default_factory=lambda: _bool("MCP_REQUIRE_AUTH", False))

    # --- the instance operator ------------------------------------------
    #: Seed an org, a user, and a principal token for that user at boot.
    #:
    #: This is deliberately not "dev only". On a Hosted-lite instance (D-020) it *is* the
    #: identity model: one operator creates rooms, and everyone else is invited — an
    #: invitation token is the invitee's whole credential, so no account is needed to join.
    #: Multi-operator login is M5, wanted only when a second person must create rooms here.
    #:
    #: What makes that safe is not this flag but `check_public_safety`: a publicly reachable
    #: instance may not run on the *published* default token. So the rule is about secrecy,
    #: which is checkable, rather than about environment, which is not.
    bootstrap_operator: bool = field(default_factory=lambda: _bool("BOOTSTRAP_OPERATOR", True))
    operator_token: str = field(
        default_factory=lambda: os.getenv("OPERATOR_TOKEN", "dev-owner-token")
    )
    #: Who that operator is. Worth configuring on a deployed instance: in a cross-company
    #: room the org name is one of the few fields deliberately *not* minimised away, so it
    #: is what the other side sees (`docs/SECURITY.md`).
    operator_org_name: str = field(
        default_factory=lambda: os.getenv("OPERATOR_ORG_NAME", "Dev Org")
    )
    operator_email: str = field(
        default_factory=lambda: os.getenv("OPERATOR_EMAIL", "dev@example.com")
    )
    operator_display_name: str = field(
        default_factory=lambda: os.getenv("OPERATOR_DISPLAY_NAME", "Dev Owner")
    )

    @property
    def operator_org_slug(self) -> str:
        """Derived, not configured: two sources for one identity is a way to disagree."""
        slug = "".join(c if c.isalnum() else "-" for c in self.operator_org_name.lower())
        return "-".join(part for part in slug.split("-") if part) or "org"

    @property
    def mcp_resource_url(self) -> str:
        """Canonical identifier of the protected resource: the MCP endpoint.

        Lives in config rather than in `api/` because both the API layer (issuing tokens)
        and the MCP adapter (validating them) need it, and an adapter importing `api`
        would break the layering rule.
        """
        return f"{self.public_base_url.rstrip('/')}/mcp"

    @property
    def is_publicly_reachable(self) -> bool:
        """Whether this instance is exposed beyond the local machine.

        Derived from `PUBLIC_BASE_URL`, which is what you must set when running behind
        a tunnel so the MCP URL handed to clients is correct. Setting it to a real
        hostname is therefore a reliable signal that strangers can reach this process.
        """
        # `urlparse().hostname` strips IPv6 brackets and the port, which naive string
        # splitting on ":" does not — `http://[::1]:8000` would otherwise parse as "[".
        try:
            host = (urlparse(self.public_base_url).hostname or "").lower()
        except ValueError:
            # A malformed URL is not a reason to assume we are safely local.
            return True
        return host not in LOCAL_HOSTS

    @property
    def operator_token_is_default(self) -> bool:
        return self.operator_token == DEFAULT_OPERATOR_TOKEN


#: The published default. Anyone reading the repo knows it, so it must never guard a
#: publicly reachable instance.
DEFAULT_OPERATOR_TOKEN = "dev-owner-token"

#: Guardrails explained in one place so the startup checks and their tests agree.
UNSAFE_PUBLIC_OPERATOR = (
    "Refusing to start: PUBLIC_BASE_URL points at a public hostname while "
    "BOOTSTRAP_OPERATOR is enabled with the default token.\n"
    "\n"
    "That combination hands full control of every room in this instance to anyone who "
    "finds the URL — the default token is published in .env.example and in the repo.\n"
    "\n"
    "Pick one:\n"
    "  * set OPERATOR_TOKEN to a long random secret (this is what a real deployment "
    "does — see docs/DEPLOY.md), or\n"
    "  * set BOOTSTRAP_OPERATOR=false and provision a token yourself, or\n"
    "  * leave PUBLIC_BASE_URL on localhost if you are only testing locally."
)

UNSAFE_PUBLIC_MCP = (
    "Refusing to start: PUBLIC_BASE_URL points at a public hostname while "
    "MCP_REQUIRE_AUTH is off.\n"
    "\n"
    "The MCP endpoint would accept tool calls from anyone who found the URL. Set "
    "MCP_REQUIRE_AUTH=true so clients must complete the OAuth flow, or keep "
    "PUBLIC_BASE_URL on localhost."
)


def check_public_safety(config: Settings) -> None:
    """Raise if this instance would be exposed without real protection.

    Called at startup. A warning would not be enough for either case: the failure is
    silent, total, and only discovered after the damage. Two independent checks rather
    than one, so turning off a single switch cannot open the endpoint.
    """
    if not config.is_publicly_reachable:
        return
    if config.bootstrap_operator and config.operator_token_is_default:
        raise RuntimeError(UNSAFE_PUBLIC_OPERATOR)
    if not config.mcp_require_auth:
        raise RuntimeError(UNSAFE_PUBLIC_MCP)


settings = Settings()
