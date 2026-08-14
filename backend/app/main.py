"""FastAPI application: REST + SSE + the MCP server, in one process.

Run with:  uvicorn app.main:app --reload   (from the backend/ directory)
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from .agents.runner import runner
from .api.routes import router
from .config import settings
from .db.database import init_db
from .errors import RoomError
from .mcp_server import mcp

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("agent_room")

# Build the MCP ASGI app once; its session manager is created lazily on first call.
mcp_app = mcp.streamable_http_app()

JANITOR_INTERVAL_SECONDS = 60


async def _expiry_janitor() -> None:
    """Flip rooms to `expired` on schedule so the UI and agents agree on time.

    Rooms also expire lazily on read; this exists so an idle room still stops
    accepting agent traffic promptly. Rows are kept for debugging — real
    deletion is DELETE /api/rooms/{code} or scripts/cleanup.py.
    """
    from .services import rooms

    while True:
        try:
            await asyncio.sleep(JANITOR_INTERVAL_SECONDS)
            expired = await rooms.expire_due_rooms()
            if expired:
                log.info("janitor expired %d room(s)", len(expired))
        except asyncio.CancelledError:
            raise
        except Exception:  # pragma: no cover - never let the janitor die
            log.exception("expiry janitor iteration failed")


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    runner.start()
    janitor = asyncio.create_task(_expiry_janitor())
    log.info(
        "agent-room up | openai=%s model=%s | ttl=%ss | max_turns=%d",
        "on" if settings.openai_enabled else "OFF (set OPENAI_API_KEY)",
        settings.openai_model,
        settings.room_ttl_seconds,
        settings.max_room_agent_turns,
    )
    # The MCP streamable-HTTP session manager must be running for /mcp to serve.
    async with mcp.session_manager.run():
        try:
            yield
        finally:
            janitor.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await janitor
            await runner.shutdown()


app = FastAPI(
    title="Agent Room",
    version="0.1.0",
    description="Temporary shared rooms where AI agents belonging to different humans coordinate.",
    lifespan=lifespan,
)

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
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": exc.code, "message": exc.message},
    )


app.include_router(router)

# Remote MCP endpoint for Claude Code: http://localhost:8000/mcp
app.mount("/mcp", mcp_app)


@app.get("/")
async def index() -> dict[str, str]:
    return {
        "service": "agent-room",
        "docs": "/docs",
        "api": "/api",
        "mcp": f"{settings.public_base_url.rstrip('/')}/mcp",
    }
