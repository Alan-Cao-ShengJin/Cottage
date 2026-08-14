"""How a deployed instance is composed — the properties that make it reachable.

These are not domain tests. They cover the seam between the product and the machine it runs
on, which M2.0 introduced and which has a specific way of failing: silently. Serving the
console from the same origin as the API means mounting static files at `/`, and a Starlette
mount at `/` matches every path the route table has not already claimed. Get the ordering
wrong and the API disappears while the site still looks fine — the console loads, and every
agent host gets HTML where it expected JSON.

Related: `tests/test_public_exposure.py` covers refusing to *start* unsafely. This file
covers being correct once started.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path

import httpx
import pytest

from app.config import Settings
from app.main import build_app, service_descriptor


@pytest.fixture()
def console(tmp_path: Path) -> Path:
    """A stand-in for `frontend/out`.

    Deliberately not the real export: these properties must hold in a checkout where the
    frontend has never been built, which is the normal state in CI.
    """
    (tmp_path / "index.html").write_text("<!doctype html><title>console</title>", "utf-8")
    room = tmp_path / "room"
    room.mkdir()
    (room / "index.html").write_text("<!doctype html><title>room</title>", "utf-8")
    return tmp_path


def client_for(console_dir: Path | None) -> Iterator[httpx.AsyncClient]:
    app = build_app(console_dir)
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://testserver")


@pytest.mark.asyncio
async def test_console_mount_does_not_shadow_the_api(console: Path) -> None:
    """The whole point of one origin: both surfaces answer.

    A 404 or an HTML body on `/api/rooms` here means the mount swallowed the API. The
    assertion is on *reaching* the endpoint, not on succeeding: 401 is the correct answer
    to an unauthenticated call and proves routing and auth both ran.
    """
    async with client_for(console) as c:
        root = await c.get("/")
        assert root.status_code == 200
        assert "text/html" in root.headers["content-type"]

        api = await c.post("/api/rooms", json={"name": "shadow check"})
        assert api.status_code == 401, api.text
        assert api.json()["error"] == "unauthenticated"


@pytest.mark.asyncio
async def test_every_advertised_path_is_reachable_behind_the_console(console: Path) -> None:
    """`/healthz` tells clients where to go; those places must exist.

    A descriptor that advertises a path the mount has swallowed is worse than no
    descriptor, because it moves the failure to the client and looks like their bug.
    """
    async with client_for(console) as c:
        health = await c.get("/healthz")
        assert health.status_code == 200
        body = health.json()
        assert body["protocol"] == "arp/1"
        assert body["console"] is True

        # Every advertised path, followed as a client would.
        for url in (body["openapi_for_chatgpt_actions"], body["oauth_protected_resource"]):
            path = url.split("/", 3)[3]
            response = await c.get(f"/{path}")
            assert response.status_code == 200, f"/{path} -> {response.status_code}"
            assert "application/json" in response.headers["content-type"]


@pytest.mark.asyncio
async def test_mcp_answers_as_mcp_from_behind_the_console(
    console: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`/mcp` is the join path, so the console must not be able to swallow it.

    Probed in the configuration a public deployment actually runs — `MCP_REQUIRE_AUTH` on —
    which is also the only way to answer at this layer without a live session manager. The
    challenge header is the assertion that matters: it is how a client discovers where to
    authenticate, so an HTML body here would strand every agent host with no way to even
    begin.
    """
    from app.adapters.mcp import auth as mcp_auth

    monkeypatch.setattr(mcp_auth, "settings", replace(Settings(), mcp_require_auth=True))

    async with client_for(console) as c:
        response = await c.get("/mcp")
        assert response.status_code == 401, response.text
        assert "text/html" not in response.headers.get("content-type", "")
        challenge = response.headers["www-authenticate"]
        assert "resource_metadata=" in challenge


@pytest.mark.asyncio
async def test_deep_console_route_is_served_as_a_document(console: Path) -> None:
    """`/room/` is a directory in the export, not a file.

    The room screen reads its id from `?room=` precisely because a static export cannot
    pre-render an unknown path segment; this asserts the serving half of that decision, so
    a future change to `trailingSlash` cannot break the board without failing here.
    """
    async with client_for(console) as c:
        response = await c.get("/room/")
        assert response.status_code == 200
        assert "text/html" in response.headers["content-type"]
        assert "room" in response.text


@pytest.mark.asyncio
async def test_without_a_console_the_root_describes_the_service() -> None:
    """A backend-only run must still be usable and still be honest.

    This is the `uvicorn` dev path and any deploy that skips the frontend stage: `/` falls
    back to the descriptor, and `console` reports false rather than advertising a UI that
    is not there.
    """
    async with client_for(None) as c:
        root = await c.get("/")
        assert root.status_code == 200
        assert root.json()["console"] is False
        assert root.json()["protocol"] == "arp/1"

        health = await c.get("/healthz")
        assert health.json() == root.json()


def test_descriptor_advertises_the_configured_public_url() -> None:
    """The failure `docs/DEPLOY.md` warns about, pinned.

    A deploy with a stale `PUBLIC_BASE_URL` boots happily and then hands every client an
    MCP URL and OAuth audience pointing at somewhere else, so joins fail with what looks
    like a client-side auth bug. The descriptor is where that is caught in one curl, so it
    has to derive from config rather than restate a constant.
    """
    original = Settings()
    assert original.mcp_resource_url.endswith("/mcp")

    body = service_descriptor(console=False)
    assert body["mcp"] == original.mcp_resource_url
    assert body["publicly_reachable"] == original.is_publicly_reachable
    assert body["mcp_requires_auth"] == original.mcp_require_auth
