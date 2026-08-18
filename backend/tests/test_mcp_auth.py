"""Bearer enforcement on the MCP endpoint.

A hosted client is given only a URL. The 401 it gets back is what tells it where the
authorization server is, so the challenge header is part of the connection path rather
than an error nicety. And the enforcement has to sit in front of the protocol machinery:
if `initialize` or `tools/list` answered without a token, the surface would be open even
though every individual tool checked.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
from contextlib import contextmanager

import httpx
import pytest

from app.adapters.mcp import auth as mcp_auth
from app.config import UNSAFE_PUBLIC_MCP, Settings, check_public_safety, settings
from app.core import oauth, rooms
from app.main import app

pytestmark = pytest.mark.asyncio

REDIRECT = "https://chatgpt.com/aip/callback"

INITIALIZE = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "test", "version": "1"},
    },
}
MCP_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/event-stream",
}


@contextmanager
def require_auth(enabled: bool):
    """Flip the switch for one test. `Settings` is frozen, so set it via object.

    Worth doing rather than parameterising the app: the middleware reads the flag per
    request, and a test that swapped the whole app would not prove that.
    """
    original = settings.mcp_require_auth
    object.__setattr__(settings, "mcp_require_auth", enabled)
    try:
        yield
    finally:
        object.__setattr__(settings, "mcp_require_auth", original)


async def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://arp.test")


async def _access_token(org_id: str, user_id: str, *, audience: str) -> tuple[str, str]:
    """Run the real flow end to end and return `(token, identity_id)`."""
    client = await oauth.register_client(client_name="ChatGPT", redirect_uris=[REDIRECT])
    verifier = secrets.token_urlsafe(48)
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    )
    request = await oauth.validate_authorization_request(
        client_id=client.client_id,
        redirect_uri=REDIRECT,
        response_type="code",
        code_challenge=challenge,
        code_challenge_method="S256",
        scope="agent",
        state=None,
        resource=None,
    )
    identity = await rooms.ensure_identity(
        org_id=org_id, owner_user_id=user_id, display_name="ChatGPT (bound)"
    )
    code = await oauth.issue_authorization_code(request, agent_identity=identity)
    grant = await oauth.exchange_authorization_code(
        code=code,
        client_id=client.client_id,
        redirect_uri=REDIRECT,
        code_verifier=verifier,
        resource=None,
        expected_audience=audience,
    )
    return grant.access_token, identity.id


# ---------------------------------------------------------------------------
# The 401 challenge
# ---------------------------------------------------------------------------


async def test_no_token_gets_401_with_a_resource_metadata_pointer(fresh_db):
    """RFC 9728. Without this pointer a client has no way to start discovery."""
    with require_auth(True):
        async with await _client() as client:
            response = await client.post("/mcp", json=INITIALIZE, headers=MCP_HEADERS)

    assert response.status_code == 401
    challenge = response.headers.get("www-authenticate", "")
    assert challenge.startswith("Bearer ")
    assert "resource_metadata=" in challenge
    assert "/.well-known/oauth-protected-resource" in challenge


async def test_garbage_token_gets_401_invalid_token(fresh_db):
    with require_auth(True):
        async with await _client() as client:
            response = await client.post(
                "/mcp",
                json=INITIALIZE,
                headers={**MCP_HEADERS, "Authorization": "Bearer not-a-real-token"},
            )
    assert response.status_code == 401
    assert 'error="invalid_token"' in response.headers.get("www-authenticate", "")


async def test_hosted_account_policy_refuses_invitation_as_mcp_auth(make_room):
    room = await make_room()
    original = settings.require_account_for_join
    object.__setattr__(settings, "require_account_for_join", True)
    try:
        with require_auth(True):
            response = await _through_middleware(
                token=room.join_token, audience="https://rooms.test/mcp"
            )
    finally:
        object.__setattr__(settings, "require_account_for_join", original)
    assert response.status_code == 401
    assert "invalid_token" in response.text


async def test_non_bearer_authorization_is_treated_as_absent(fresh_db):
    with require_auth(True):
        async with await _client() as client:
            response = await client.post(
                "/mcp",
                json=INITIALIZE,
                headers={**MCP_HEADERS, "Authorization": "Basic dXNlcjpwYXNz"},
            )
    assert response.status_code == 401


async def test_token_for_another_resource_gets_403_not_401(fresh_db, org):
    """403, because re-authenticating with the same credential would not help. A 401
    would send the client into a pointless discovery loop."""
    org_id, user_id = org
    token, _ = await _access_token(org_id, user_id, audience="https://elsewhere.test/mcp")

    with require_auth(True):
        async with await _client() as client:
            response = await client.post(
                "/mcp",
                json=INITIALIZE,
                headers={**MCP_HEADERS, "Authorization": f"Bearer {token}"},
            )
    assert response.status_code == 403
    assert "different resource" in response.text


async def test_valid_token_and_permissive_mode_both_reach_the_mcp_machinery(fresh_db, org):
    """The positive cases, through to a real `initialize` response.

    Both arms live in one test because `StreamableHTTPSessionManager.run()` may be entered
    only once per instance, and splitting them would mean stubbing the very machinery the
    test exists to reach.
    """
    org_id, user_id = org
    from app.adapters.mcp.server import mcp
    from app.api.oauth import mcp_resource_url

    token, _ = await _access_token(org_id, user_id, audience=mcp_resource_url())

    # `localhost`, not the fake host the other tests use: the SDK's DNS-rebinding
    # protection validates the Host header, and this is the one test that reaches it.
    async with mcp.session_manager.run():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://localhost"
        ) as client:
            with require_auth(True):
                authed = await client.post(
                    "/mcp",
                    json=INITIALIZE,
                    headers={**MCP_HEADERS, "Authorization": f"Bearer {token}"},
                )
            # Local development: no token required, so a bare call must still work. The
            # startup guard is what keeps this mode off a public URL.
            with require_auth(False):
                permissive = await client.post("/mcp", json=INITIALIZE, headers=MCP_HEADERS)

    assert authed.status_code == 200, authed.text
    assert "agent-rooms" in authed.text, "the protocol layer must genuinely have answered"
    assert permissive.status_code == 200, permissive.text
    assert "agent-rooms" in permissive.text


async def test_revoked_token_is_refused_at_the_transport(fresh_db, org):
    org_id, user_id = org
    from app.api.oauth import mcp_resource_url
    from app.db import database as db
    from app.util import hash_token, utcnow_iso

    token, _ = await _access_token(org_id, user_id, audience=mcp_resource_url())
    await db.execute(
        "UPDATE principal_tokens SET revoked_at = ? WHERE token_hash = ?",
        (utcnow_iso(), hash_token(token)),
    )

    with require_auth(True):
        async with await _client() as client:
            response = await client.post(
                "/mcp",
                json=INITIALIZE,
                headers={**MCP_HEADERS, "Authorization": f"Bearer {token}"},
            )
    assert response.status_code == 401


# ---------------------------------------------------------------------------
# What stays open, and why
# ---------------------------------------------------------------------------


async def test_discovery_stays_reachable_with_auth_required(fresh_db):
    """They must be public: they are how an unauthenticated client learns to authenticate."""
    with require_auth(True):
        async with await _client() as client:
            for path in (
                "/.well-known/oauth-protected-resource",
                "/.well-known/oauth-authorization-server",
            ):
                assert (await client.get(path)).status_code == 200, path


async def _through_middleware(*, token: str | None, audience: str) -> httpx.Response:
    """Drive `McpAuthMiddleware` around a trivial inner app.

    Tests the middleware's *decision* in isolation. Needed because
    `StreamableHTTPSessionManager.run()` is single-use per instance, so only one test in
    this module can pass all the way through to the real MCP app — and a permissive-mode
    request does pass through, which would hit a dead session manager.
    """
    seen: dict[str, object] = {}

    async def inner(scope, receive, send):
        principal = mcp_auth.current_principal()
        seen["principal"] = principal
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    wrapped = mcp_auth.McpAuthMiddleware(
        inner,
        resource_metadata_url="https://rooms.test/.well-known/oauth-protected-resource",
        audience=audience,
    )
    headers = {**MCP_HEADERS}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=wrapped), base_url="http://arp.test"
    ) as client:
        response = await client.post("/", json=INITIALIZE, headers=headers)
    response.request.extensions["seen"] = seen  # type: ignore[index]
    return response


async def test_permissive_mode_passes_through_without_a_challenge(fresh_db):
    """With auth off, a bare call must reach the inner app and carry no principal."""
    with require_auth(False):
        response = await _through_middleware(token=None, audience="https://rooms.test/mcp")
    assert response.status_code == 204
    assert "www-authenticate" not in {k.lower() for k in response.headers}


async def test_permissive_mode_still_honours_a_presented_token(fresh_db, org):
    """So local testing exercises the same identity path as production, rather than a
    different one that only works because auth is off."""
    org_id, user_id = org
    token, identity_id = await _access_token(org_id, user_id, audience="https://rooms.test/mcp")

    with require_auth(False):
        response = await _through_middleware(token=token, audience="https://rooms.test/mcp")

    assert response.status_code == 204
    seen = response.request.extensions["seen"]
    principal = seen["principal"]
    assert principal is not None
    assert principal.identity is not None
    assert principal.identity.id == identity_id


async def test_valid_token_sets_the_principal_for_the_request(fresh_db, org):
    org_id, user_id = org
    token, identity_id = await _access_token(org_id, user_id, audience="https://rooms.test/mcp")

    with require_auth(True):
        response = await _through_middleware(token=token, audience="https://rooms.test/mcp")

    assert response.status_code == 204
    principal = response.request.extensions["seen"]["principal"]
    assert principal.identity.id == identity_id


async def test_the_principal_does_not_leak_between_requests(fresh_db, org):
    """The ContextVar must be reset, or one request's identity would bleed into the next."""
    org_id, user_id = org
    token, _ = await _access_token(org_id, user_id, audience="https://rooms.test/mcp")

    with require_auth(True):
        await _through_middleware(token=token, audience="https://rooms.test/mcp")
    assert mcp_auth.current_principal() is None

    with require_auth(False):
        response = await _through_middleware(token=None, audience="https://rooms.test/mcp")
    assert response.request.extensions["seen"]["principal"] is None


# ---------------------------------------------------------------------------
# DNS-rebinding protection must not lock out the address we publish
# ---------------------------------------------------------------------------


def test_transport_allowlist_includes_the_public_host():
    """The SDK rejects unlisted `Host` headers with 421, before auth and before routing.

    A tunnel hostname is not loopback, so without this a hosted client would be refused
    every request with nothing but a log line to explain it. Whatever address we publish
    must be an address we accept.
    """
    import dataclasses

    from app.adapters.mcp import server as mcp_server

    tunnel = "https://romantic-hippo.trycloudflare.com"
    original = mcp_server.settings
    try:
        mcp_server.settings = dataclasses.replace(Settings(), public_base_url=tunnel)
        security = mcp_server.transport_security()
    finally:
        mcp_server.settings = original

    assert "romantic-hippo.trycloudflare.com" in security.allowed_hosts
    assert "romantic-hippo.trycloudflare.com:*" in security.allowed_hosts
    assert tunnel in security.allowed_origins
    # Loopback must survive, or local development breaks.
    assert "localhost" in security.allowed_hosts
    assert "127.0.0.1" in security.allowed_hosts
    # And the protection stays on: the fix is an allowlist, not a bypass.
    assert security.enable_dns_rebinding_protection is True


def test_transport_allowlist_does_not_admit_arbitrary_hosts():
    from app.adapters.mcp.server import transport_security

    security = transport_security()
    assert "evil.example.com" not in security.allowed_hosts
    assert "*" not in security.allowed_hosts


# ---------------------------------------------------------------------------
# The guard that keeps the permissive mode local
# ---------------------------------------------------------------------------


def test_public_url_without_mcp_auth_refuses_to_start():
    """Two independent checks, so turning off one switch cannot open the endpoint."""
    import dataclasses

    config = dataclasses.replace(
        Settings(),
        public_base_url="https://romantic-hippo.trycloudflare.com",
        bootstrap_operator=False,
        mcp_require_auth=False,
    )
    with pytest.raises(RuntimeError) as exc:
        check_public_safety(config)
    assert str(exc.value) == UNSAFE_PUBLIC_MCP


def test_public_url_with_mcp_auth_and_a_real_secret_is_allowed():
    import dataclasses

    config = dataclasses.replace(
        Settings(),
        public_base_url="https://romantic-hippo.trycloudflare.com",
        bootstrap_operator=True,
        operator_token="s6xk2p9qw4m7v1t8z3r5n0h6j2c4b8d1f7g9k3l5",
        mcp_require_auth=True,
    )
    check_public_safety(config)


# ---------------------------------------------------------------------------
# Identity comes from the token
# ---------------------------------------------------------------------------


class _FakeRequestContext:
    """Stands in for the per-message request the SDK hands a tool.

    Necessary because the ASGI ContextVar is *not* visible inside a tool — streamable HTTP
    runs tool calls in the session's task, created on an earlier request. A wire test found
    this; asserting against the ContextVar here would re-create the same false pass.
    """

    def __init__(self, token: str | None) -> None:
        headers = {"authorization": f"Bearer {token}"} if token else {}
        self.request = type("Req", (), {"headers": headers})()


class _FakeCtx:
    def __init__(self, token: str | None) -> None:
        self.request_context = _FakeRequestContext(token)


async def test_authenticated_identity_and_name_both_come_from_the_token(fresh_db, org):
    """The anti-spoofing property, at the layer that decides it.

    Returning only the identity was not enough: `join_room` accepts a per-room display name
    (D-015), so a caller could keep its bound identity and still present under any name.
    `_resolve_identity` therefore returns the effective name too.
    """
    from app.adapters.mcp.server import _resolve_identity
    from app.api.oauth import mcp_resource_url

    org_id, user_id = org
    token, identity_id = await _access_token(org_id, user_id, audience=mcp_resource_url())

    identity, effective_name = await _resolve_identity(
        "irrelevant-invitation-token",
        "Totally Someone Else",  # the lie
        "",
        [],
        ctx=_FakeCtx(token),
    )

    assert identity.id == identity_id
    assert effective_name == "ChatGPT (bound)", "the supplied name must be ignored"


async def test_authenticated_join_records_the_bound_name_in_the_room(fresh_db, org, make_room):
    """End to end through the tool, because the previous bug lived *between* the two
    steps: identity resolution was right and the room still showed the spoofed name."""
    from app.adapters.mcp import server as mcp_server
    from app.api.oauth import mcp_resource_url
    from app.core import store

    org_id, user_id = org
    room = await make_room()
    token, _ = await _access_token(org_id, user_id, audience=mcp_resource_url())

    with require_auth(True):
        joined = await mcp_server.join_room(
            invitation_token=room.join_token,
            display_name="Totally Someone Else",
            execution_mode="human_turn_only",
            ctx=_FakeCtx(token),  # type: ignore[arg-type]
        )

    assert joined["ok"] is True, joined
    assert joined["display_name"] == "ChatGPT (bound)"
    assert joined["display_name_was_overridden"] is True

    participants = await store.list_participants(room.room.id)
    names = {p.identity.display_name for p in participants}
    assert "ChatGPT (bound)" in names
    assert "Totally Someone Else" not in names, "the room must never show the spoofed name"


async def test_authenticated_agent_creates_without_a_principal_token(fresh_db, org):
    """The natural-language path is one tool call after OAuth, with no browser form."""
    from app.adapters.mcp import server as mcp_server
    from app.api.oauth import mcp_resource_url

    org_id, user_id = org
    token, _ = await _access_token(org_id, user_id, audience=mcp_resource_url())

    with require_auth(True):
        created = await mcp_server.create_room(
            name="Created by request",
            purpose="No principal-token ceremony",
            charter="Coordinate in small claims; ready means the shared gate is green.",
            ctx=_FakeCtx(token),  # type: ignore[arg-type]
        )

    assert created["ok"] is True, created
    assert created["room_name"] == "Created by request"
    assert created["join_token"]

    # Read the charter back from the room rather than from the response. The compact
    # response no longer echoes it (D-085), and an echo was never evidence anyway: the
    # caller supplied that string, and a charter that failed content inspection would
    # have raised instead of being quietly altered. Storage is the claim, so load it.
    from app.core import store

    room = await store.load_room(created["room_id"])
    assert room.charter == "Coordinate in small claims; ready means the shared gate is green."


async def test_without_a_token_the_client_names_itself(fresh_db, org, make_room):
    """The local-development path, stated so its weakness is visible rather than implied."""
    from app.adapters.mcp.server import _resolve_identity

    room = await make_room()
    identity, effective_name = await _resolve_identity(
        room.join_token, "Local Agent", "", [], ctx=_FakeCtx(None)
    )
    assert effective_name == "Local Agent"
    assert identity.display_name == "Local Agent"
