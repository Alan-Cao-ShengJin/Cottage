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
from typing import Any

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from .adapters.mcp.auth import McpAuthMiddleware, NormalizeMcpPath
from .adapters.mcp.server import mcp
from .api.gpt_schema import build_gpt_schema
from .api.oauth import mcp_resource_url
from .api.oauth import router as oauth_router
from .api.routes import router
from .config import check_public_safety, settings
from .core import presence, rooms, tasks, work
from .core.bus import bus
from .core.errors import RoomError
from .db.database import init_db

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("agent_rooms")

mcp_app = mcp.streamable_http_app()


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


async def _bootstrap_dev_identity() -> None:
    """Seed a dev org/user and a fixed token so the slice is runnable immediately.

    Dev only (`DEV_BOOTSTRAP`). Real identity federation is M5; nothing in `core/`
    assumes this shape.
    """
    org_id, user_id = await rooms.ensure_org_and_user(
        org_name="Dev Org",
        org_slug="dev-org",
        email="dev@example.com",
        display_name="Dev Owner",
    )
    await rooms.set_principal_token(
        token=settings.dev_bootstrap_token,
        subject_kind="user",
        subject_id=user_id,
        org_id=org_id,
        label="dev bootstrap",
    )
    log.warning(
        "DEV_BOOTSTRAP is on: user %s authenticates with DEV_BOOTSTRAP_TOKEN. "
        "Never enable this outside local development.",
        user_id,
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Before anything else: refuse to run a publicly reachable instance guarded by a
    # credential published in this repo. See config.UNSAFE_PUBLIC_BOOTSTRAP.
    check_public_safety(settings)

    await init_db()
    if settings.dev_bootstrap:
        await _bootstrap_dev_identity()

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

# The MCP app is wrapped, not mounted bare: unauthenticated requests must not reach the
# protocol machinery, and the 401 challenge that points clients at the authorization
# server has to be an HTTP response, which a tool cannot produce.
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


@app.get("/")
async def index() -> dict[str, Any]:
    base = settings.public_base_url.rstrip("/")
    return {
        "service": "agent-rooms",
        "protocol": "arp/1",
        "docs": "/docs",
        "api": "/api",
        # Two ways to attach an agent host, both pointed at the same core.
        "mcp": f"{base}/mcp",
        "openapi_for_chatgpt_actions": f"{base}/openapi-gpt.json",
        "oauth_protected_resource": f"{base}/.well-known/oauth-protected-resource",
        "mcp_requires_auth": settings.mcp_require_auth,
        "publicly_reachable": settings.is_publicly_reachable,
    }


@app.get("/openapi-gpt.json")
async def openapi_for_chatgpt() -> dict[str, Any]:
    """OpenAPI 3.0.3 document for a ChatGPT custom-GPT Action.

    Separate from `/openapi.json`, which stays a truthful 3.1 document for everything
    else. See `api/gpt_schema.py` for why the translation is needed.
    """
    return build_gpt_schema(app.openapi(), public_base_url=settings.public_base_url)
