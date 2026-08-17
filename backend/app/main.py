"""ASGI composition: ARP HTTP + SSE, the MCP adapter, and the background reaper.

One process, one service layer. The MCP adapter is mounted onto the same app so
there is nothing extra to run and no way for the two doors into the product to drift
apart on behavior.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles

from .adapters.mcp.auth import McpAuthMiddleware, NormalizeMcpPath
from .adapters.mcp.server import mcp
from .api.account import router as account_router
from .api.gpt_schema import build_gpt_schema
from .api.oauth import mcp_resource_url
from .api.oauth import router as oauth_router
from .api.routes import router
from .config import check_public_safety, settings
from .core import accounts, billing, presence, rooms, tasks, work
from .core.bus import bus
from .core.errors import RoomError
from .db.database import init_db
from .db.database import shutdown as shutdown_db

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("agent_rooms")

mcp_app = mcp.streamable_http_app()

#: The exported console to serve, or None if this deployment has no console compiled in.
#: Resolved once at import: `/` is either the console or the service descriptor, decided
#: at composition time rather than re-checked on every request.
_CONFIGURED_CONSOLE_DIR = (
    settings.console_dir if (settings.console_dir / "index.html").is_file() else None
)


async def _reaper() -> None:
    """Expire leases, close dead connections, stale work, close expired rooms.

    Correctness does not depend on this loop: lease expiry is enforced on every read
    (`core/store.to_task`), so a reaper that is late or dead cannot let two
    participants hold one task. What it provides is *timeliness* — the durable status
    change and the `task.claim_expired` event that tells the room work is reclaimable.
    """
    from .db import database as db

    while True:
        try:
            await asyncio.sleep(settings.reaper_interval_seconds)
            await tasks.reap_expired_leases()
            await presence.reap_dead_connections()
            await rooms.expire_due_rooms()

            rows = await db.fetch_all("SELECT id FROM rooms WHERE status = 'open'")
            for row in rows:
                room = await rooms.store.load_room(row["id"])
                await work.mark_stale_declarations(room)
        except asyncio.CancelledError:
            raise
        except Exception:  # pragma: no cover - never let the reaper die
            log.exception("reaper iteration failed")


async def _bootstrap_operator_identity() -> None:
    """Seed the instance operator: an org, a user, and a principal token for that user.

    On a laptop this is a dev convenience. On a Hosted-lite instance it is the identity
    model (D-020): this one person creates rooms, and everyone else is *invited* — an
    invitation token is the invitee's entire credential, so joining needs no account here.
    Multi-operator login is M5, and nothing in `core/` assumes either shape.

    Idempotent, because a container restarts and the volume persists.
    """
    org_id, user_id = await rooms.ensure_org_and_user(
        org_name=settings.operator_org_name,
        org_slug=settings.operator_org_slug,
        email=settings.operator_email,
        display_name=settings.operator_display_name,
    )
    await rooms.set_principal_token(
        token=settings.operator_token,
        subject_kind="user",
        subject_id=user_id,
        org_id=org_id,
        label="instance operator",
    )
    await accounts.mark_email_verified(user_id)
    # Bootstrap is an explicit operator grant, not a fake paid subscription. Public
    # signups receive no such row and must earn rooms:create through billing webhooks.
    await billing.grant_creator_entitlement(org_id, source="bootstrap")
    if settings.operator_password_hash:
        changed = await accounts.set_password_hash(user_id, settings.operator_password_hash)
        if changed:
            log.info(
                "operator %s password verifier installed; prior browser sessions revoked", user_id
            )
    else:
        log.warning(
            "operator password login is not configured. Set OPERATOR_PASSWORD_HASH using "
            "scripts/hash_password.py before using hosted OAuth consent."
        )
    # Only alarming in the case that is actually alarming. `check_public_safety` has
    # already refused to boot if this token is the published default while reachable.
    if settings.operator_token_is_default:
        log.warning(
            "operator %s authenticates with the PUBLISHED DEFAULT OPERATOR_TOKEN. "
            "Fine locally; set a real secret before exposing this instance.",
            user_id,
        )
    else:
        log.info("operator %s ready | org=%s", user_id, settings.operator_org_name)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Before anything else: refuse to run a publicly reachable instance guarded by a
    # credential published in this repo. See config.UNSAFE_PUBLIC_OPERATOR.
    check_public_safety(settings)

    await init_db()
    if settings.bootstrap_operator:
        await _bootstrap_operator_identity()

    reaper = asyncio.create_task(_reaper())
    log.info(
        "agent-rooms up | protocol=arp/1 | reaper=%ss | heartbeat=%ss | no model provider (by design)",
        settings.reaper_interval_seconds,
        settings.heartbeat_interval_seconds,
    )
    # The MCP streamable-HTTP session manager must be running for /mcp to serve.
    async with mcp.session_manager.run():
        try:
            yield
        finally:
            reaper.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await reaper
            bus.clear()
            # After the reaper, so nothing is still mid-transaction when the pooled
            # connections close.
            await shutdown_db()


def service_descriptor(*, console: bool) -> dict[str, Any]:
    """What this instance is and how to attach to it.

    Doubles as the health payload. Deliberately more than `{"ok": true}`: a deploy that
    boots but advertises the wrong `PUBLIC_BASE_URL` hands every client a broken MCP URL
    and OAuth audience, and that failure is otherwise silent until a join fails. Here it
    is visible in one curl.
    """
    base = settings.public_base_url.rstrip("/")
    return {
        "service": "agent-rooms",
        "status": "ok",
        "protocol": "arp/1",
        "docs": "/docs",
        "api": "/api",
        # Two ways to attach an agent host, both pointed at the same core.
        "mcp": f"{base}/mcp",
        "openapi_for_chatgpt_actions": f"{base}/openapi-gpt.json",
        "oauth_protected_resource": f"{base}/.well-known/oauth-protected-resource",
        "account": settings.account_url,
        "mcp_requires_auth": settings.mcp_require_auth,
        "account_required_for_join": settings.require_account_for_join,
        "public_signup_enabled": settings.public_signup_enabled,
        "creator_subscription_required": settings.enforce_creator_subscription,
        "publicly_reachable": settings.is_publicly_reachable,
        "console": console,
    }


def build_app(console_dir: Path | None = _CONFIGURED_CONSOLE_DIR) -> FastAPI:
    """Compose the service.

    A factory rather than a module-level script for one reason: whether a console is
    present changes the route table, and the ordering that keeps both working is fragile
    enough to deserve a test that builds it both ways
    (`tests/test_deployment_shape.py`). `console_dir` is None when this image has no
    console — a plain `uvicorn` dev run, where `npm run dev` serves it on :3000 instead.
    """
    app = FastAPI(
        title="Agent Rooms",
        version="0.2.0",
        description=(
            "A provider-neutral live collaboration network for independently owned AI "
            "agents. Coordination, not inference."
        ),
        lifespan=lifespan,
    )

    # Must sit outside routing so `/mcp` reaches the mount instead of being redirected.
    app.add_middleware(NormalizeMcpPath, mount_path="/mcp")

    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(settings.cors_origins),
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["*"],
    )

    @app.exception_handler(RoomError)
    async def room_error_handler(_: Request, exc: RoomError) -> JSONResponse:
        """Domain errors are structured data with a stable `error` code, so a
        coordinating agent can branch on the reason rather than parse prose."""
        return JSONResponse(status_code=exc.status_code, content=exc.to_payload())

    app.include_router(router)
    app.include_router(oauth_router)
    app.include_router(account_router)

    # The MCP app is wrapped, not mounted bare: unauthenticated requests must not reach
    # the protocol machinery, and the 401 challenge that points clients at the
    # authorization server has to be an HTTP response, which a tool cannot produce.
    app.mount(
        "/mcp",
        McpAuthMiddleware(
            mcp_app,
            resource_metadata_url=(
                f"{settings.public_base_url.rstrip('/')}/.well-known/oauth-protected-resource"
            ),
            audience=mcp_resource_url(),
        ),
    )

    @app.get("/healthz")
    async def healthz() -> dict[str, Any]:
        """Liveness for the platform, and identity for us.

        Separate from `/`, which belongs to the console when there is one. A health check
        must not depend on whether the frontend was compiled into the image.
        """
        return service_descriptor(console=console_dir is not None)

    @app.get("/openapi-gpt.json")
    async def openapi_for_chatgpt() -> dict[str, Any]:
        """OpenAPI 3.0.3 document for a ChatGPT custom-GPT Action.

        Separate from `/openapi.json`, which stays a truthful 3.1 document for everything
        else. See `api/gpt_schema.py` for why the translation is needed.
        """
        return build_gpt_schema(app.openapi(), public_base_url=settings.public_base_url)

    # The same static export serves two deliberately different entry points. The apex
    # hostname gets the public explanation; the configured `app.*` product hostname
    # enters at the connection guide. Keeping both in one image avoids a second deploy.
    if console_dir is not None:
        configured_host = (urlparse(settings.public_base_url).hostname or "").lower()

        @app.get("/", include_in_schema=False)
        async def web_entry(request: Request) -> Response:
            request_host = (request.url.hostname or "").lower()
            if configured_host.startswith("app.") and request_host == configured_host:
                return RedirectResponse("/connect/", status_code=307)
            return FileResponse(console_dir / "index.html")

    # Registered last, and it must stay last: a mount at "/" matches everything Starlette
    # has not already matched, so any route declared after it becomes unreachable. That
    # failure is silent and total, which is why a test asserts the ordering.
    if console_dir is not None:
        app.mount("/", StaticFiles(directory=console_dir, html=True), name="console")
        log.info("serving the Cottage web surface from %s", console_dir)
    else:

        @app.get("/")
        async def index() -> dict[str, Any]:
            """No console here, so the root answers as the service itself."""
            return service_descriptor(console=False)

    return app


app = build_app()
