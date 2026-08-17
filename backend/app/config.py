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


#: Environment variables a hosting platform injects into every process it runs. Presence of
#: any one means "something other than a laptop is serving this", which is the fact the
#: startup guards need and cannot get from our own configuration.
HOSTING_PLATFORM_MARKERS: tuple[tuple[str, str], ...] = (
    ("FLY_APP_NAME", "fly.io"),
    ("FLY_MACHINE_ID", "fly.io"),
    ("RAILWAY_ENVIRONMENT", "railway"),
    ("RENDER", "render"),
    ("K_SERVICE", "cloud run"),
    ("DYNO", "heroku"),
    ("WEBSITE_INSTANCE_ID", "azure app service"),
    ("KUBERNETES_SERVICE_HOST", "kubernetes"),
)


def _detect_hosting_platform() -> str | None:
    for env_var, platform in HOSTING_PLATFORM_MARKERS:
        if (os.getenv(env_var) or "").strip():
            return platform
    return None


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
    #: Whether `PUBLIC_BASE_URL` was actually set, as opposed to defaulted.
    #:
    #: Recorded separately because the default is a *localhost* URL, so an unset variable
    #: does not merely lose information — it actively asserts "I am local" to every check
    #: that reads it. A field rather than a property so it can be overridden in tests the
    #: same way every other setting is.
    public_base_url_declared: bool = field(
        default_factory=lambda: bool((os.getenv("PUBLIC_BASE_URL") or "").strip())
    )
    #: The hosting platform this process appears to run on, or None.
    #:
    #: Used **only to tighten** the startup guards, never to relax them: an unrecognised
    #: platform leaves behaviour exactly as it was, while a recognised one turns "no public
    #: address declared" from an assumption of safety into a refusal to boot. Every marker
    #: is injected by the platform itself, so no request can conjure one.
    hosting_platform: str | None = field(default_factory=lambda: _detect_hosting_platform())
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

    # --- hosted accounts and billing -----------------------------------
    #: Hosted commercial mode: every join arrives through an account-bound OAuth token.
    #: Kept off by default for Cottage/local compatibility; fly.toml enables it.
    require_account_for_join: bool = field(
        default_factory=lambda: _bool("REQUIRE_ACCOUNT_FOR_JOIN", False)
    )
    #: Enforce the rooms:create entitlement in the shared service used by every adapter.
    enforce_creator_subscription: bool = field(
        default_factory=lambda: _bool("ENFORCE_CREATOR_SUBSCRIPTION", False)
    )
    public_signup_enabled: bool = field(
        default_factory=lambda: _bool("PUBLIC_SIGNUP_ENABLED", False)
    )
    resend_api_key: str = field(default_factory=lambda: os.getenv("RESEND_API_KEY", "").strip())
    email_from: str = field(
        default_factory=lambda: os.getenv("EMAIL_FROM", "Cottage <onboarding@resend.dev>").strip()
    )
    stripe_secret_key: str = field(
        default_factory=lambda: os.getenv("STRIPE_SECRET_KEY", "").strip()
    )
    stripe_webhook_secret: str = field(
        default_factory=lambda: os.getenv("STRIPE_WEBHOOK_SECRET", "").strip()
    )
    stripe_creator_price_id: str = field(
        default_factory=lambda: os.getenv("STRIPE_CREATOR_PRICE_ID", "").strip()
    )

    # --- the instance operator ------------------------------------------
    #: Seed an org, a user, and a principal token for that user at boot.
    #:
    #: This is deliberately not "dev only": it remains a local administration/recovery
    #: principal. Commercial hosted users authenticate through public accounts (D-066).
    #:
    #: What makes that safe is not this flag but `check_public_safety`: a publicly reachable
    #: instance may not run on the *published* default token. So the rule is about secrecy,
    #: which is checkable, rather than about environment, which is not.
    bootstrap_operator: bool = field(default_factory=lambda: _bool("BOOTSTRAP_OPERATOR", True))
    operator_token: str = field(
        default_factory=lambda: os.getenv("OPERATOR_TOKEN", "dev-owner-token")
    )
    #: Argon2id verifier generated offline by scripts/hash_password.py. A verifier is still
    #: sensitive because it can be attacked offline, so deployments provide it as a secret.
    #: An empty value leaves browser login unconfigured without weakening bearer-token auth.
    operator_password_hash: str = field(
        default_factory=lambda: os.getenv("OPERATOR_PASSWORD_HASH", "").strip()
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

    def __post_init__(self) -> None:
        if not 60 <= self.default_room_ttl_seconds <= 90 * 24 * 3600:
            raise ValueError("DEFAULT_ROOM_TTL_SECONDS must be between 60 seconds and 90 days.")

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
    def account_url(self) -> str:
        return f"{self.public_base_url.rstrip('/')}/account"

    @property
    def is_publicly_reachable(self) -> bool:
        """Whether this instance is exposed beyond the local machine.

        Derived from `PUBLIC_BASE_URL`, because whatever address we hand to clients is by
        definition an address they can reach us on.

        **This answers "did someone declare a public address", not "is this process
        reachable".** Those came apart the moment we deployed: behind a tunnel the variable
        had to be set for anything to work, so it was a sound proxy; on a hosting platform
        `<app>.fly.dev` exists whether or not anyone set it. `hosting_platform` covers that
        gap, and `check_public_safety` uses both.
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
    def public_base_url_is_parseable(self) -> bool:
        """Whether the configured URL yields a host at all.

        `PUBLIC_BASE_URL=agent-rooms.fly.dev` (no scheme) parses to *no* hostname, which
        lands in `LOCAL_HOSTS` via the empty string and reads as local — a typo that
        silently disarms the guards. Treated as a configuration error instead.
        """
        try:
            return bool(urlparse(self.public_base_url).hostname)
        except ValueError:
            return False

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

UNSAFE_PUBLIC_SIGNUP_EMAIL = (
    "Refusing to start: public signup is enabled on a reachable instance but "
    "RESEND_API_KEY is missing.\n\n"
    "Without outbound email, new accounts cannot verify and can never sign in. Set "
    "RESEND_API_KEY and EMAIL_FROM, or disable PUBLIC_SIGNUP_ENABLED."
)

UNSAFE_PUBLIC_BILLING = (
    "Refusing to start: creator subscriptions are enforced but Stripe is not fully "
    "configured.\n\n"
    "Set STRIPE_SECRET_KEY, STRIPE_WEBHOOK_SECRET, and STRIPE_CREATOR_PRICE_ID, or "
    "disable ENFORCE_CREATOR_SUBSCRIPTION."
)

UNDECLARED_PUBLIC_BASE_URL = (
    "Refusing to start: this looks like a deployment on {platform}, but PUBLIC_BASE_URL "
    "is not set.\n"
    "\n"
    "Unset does not mean local. It means every safety check below reads the default "
    "'http://localhost:8000', concludes this instance is private, and waves through the "
    "published default operator token — on a hostname anyone can reach. The MCP endpoint "
    "would also answer 421 to its own real hostname, because the Host allowlist is built "
    "from this same value.\n"
    "\n"
    "Set PUBLIC_BASE_URL to the address clients will use, e.g.:\n"
    "  fly secrets set PUBLIC_BASE_URL=https://<app>.fly.dev\n"
    "\n"
    "If this is genuinely not a deployment, set PUBLIC_BASE_URL=http://localhost:8000 "
    "explicitly to say so."
)

UNPARSEABLE_PUBLIC_BASE_URL = (
    "Refusing to start: PUBLIC_BASE_URL={value!r} has no hostname.\n"
    "\n"
    "A value without a scheme parses to no host at all, which every check then reads as "
    "'local' — so a typo here silently disarms them. Include the scheme:\n"
    "  PUBLIC_BASE_URL=https://rooms.example.com"
)

UNSAFE_DEFAULT_OPERATOR_ON_PLATFORM = (
    "Refusing to start: running on {platform} with the published default OPERATOR_TOKEN.\n"
    "\n"
    "That token is printed in this repository, so anyone who reads it owns every room on "
    "this instance. Set OPERATOR_TOKEN to a long random secret:\n"
    "  fly secrets set OPERATOR_TOKEN=$(openssl rand -hex 20)"
)


def check_public_safety(config: Settings) -> None:
    """Raise if this instance would be exposed without real protection.

    Called at startup. A warning would not be enough for any of these: the failure is
    silent, total, and only discovered after the damage.

    **Fails closed on ignorance, which it did not always do.** The original version asked
    one question — "does `PUBLIC_BASE_URL` name a public host?" — and returned early when
    the answer was no. On a laptop behind a tunnel that was sound, because the variable had
    to be set for the tunnel to work at all. On a hosting platform it inverted: the app is
    reachable at a hostname the platform assigns, so forgetting to set the variable left
    both guards disarmed *and* handed the instance to whoever had read the default token
    out of the repo. An audit of the first deployment found it (D-024).

    So: an unset value on a recognised platform is a configuration error, not an assertion
    of privacy, and the default token is refused on any recognised platform regardless of
    what the URL says. Platform detection only ever *adds* refusals — an unrecognised
    platform behaves exactly as before, so this cannot become a way to boot something the
    old checks would have stopped.
    """
    platform = config.hosting_platform

    if config.public_base_url_declared and not config.public_base_url_is_parseable:
        raise RuntimeError(UNPARSEABLE_PUBLIC_BASE_URL.format(value=config.public_base_url))

    if platform is not None:
        if not config.public_base_url_declared:
            raise RuntimeError(UNDECLARED_PUBLIC_BASE_URL.format(platform=platform))
        # Independent of the URL: on a platform, the published default is never acceptable,
        # so a wrong-but-parseable URL cannot buy it a pass either.
        if config.bootstrap_operator and config.operator_token_is_default:
            raise RuntimeError(UNSAFE_DEFAULT_OPERATOR_ON_PLATFORM.format(platform=platform))

    if not config.is_publicly_reachable:
        return
    if config.bootstrap_operator and config.operator_token_is_default:
        raise RuntimeError(UNSAFE_PUBLIC_OPERATOR)
    if not config.mcp_require_auth:
        raise RuntimeError(UNSAFE_PUBLIC_MCP)
    if config.public_signup_enabled and not config.resend_api_key:
        raise RuntimeError(UNSAFE_PUBLIC_SIGNUP_EMAIL)
    if config.enforce_creator_subscription and not all(
        (
            config.stripe_secret_key,
            config.stripe_webhook_secret,
            config.stripe_creator_price_id,
        )
    ):
        raise RuntimeError(UNSAFE_PUBLIC_BILLING)


settings = Settings()
