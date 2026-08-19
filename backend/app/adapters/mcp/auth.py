"""Bearer authentication in front of the MCP endpoint.

Implemented as an ASGI wrapper rather than inside the tools, for two reasons:

* **Unauthenticated requests must never reach the MCP machinery.** Checking inside each
  tool would leave the protocol surface (initialize, tools/list) open, and would make
  "did I remember the check?" a per-tool question.
* **The 401 is what drives discovery.** A client with no token needs
  `WWW-Authenticate: Bearer resource_metadata="…"` (RFC 9728) to find the authorization
  server. That has to be an HTTP response, which a tool cannot produce.

**Where tools get the caller from, and why not the obvious place.** The wrapper records the
principal in a `ContextVar`, but a tool must *not* read it: streamable HTTP runs tool calls
in the session's task, created on an earlier request, so a value set while handling this POST
is invisible there. A unit test that set the var in the same task passed while the real client
was still able to spoof its identity. Tools therefore call `principal_for_tool`, which reads
the bearer token from the request carrying *that* tool call. The ContextVar remains as a
fallback for transports where a per-message request is not available.

That resolution is what removes the self-chosen `display_name`: identity comes from the token
a human consented to, not from an argument the agent supplies.
"""

from __future__ import annotations

import json
import logging
from contextvars import ContextVar
from typing import Any

from ...config import settings
from ...core.errors import Forbidden, RoomError, Unauthenticated
from ...core.oauth import TokenPrincipal, authenticate_access_token

log = logging.getLogger(__name__)

#: The authenticated caller for the current request. `None` outside a request, and
#: outside an authenticated one — tools must handle that rather than assume.
_current: ContextVar[TokenPrincipal | None] = ContextVar("mcp_principal", default=None)


def current_principal() -> TokenPrincipal | None:
    """The principal for the current ASGI request, if the middleware set one.

    **Do not rely on this inside an MCP tool.** Streamable HTTP creates the session's task
    when the session is established, and later requests run their tool calls in that task —
    so a ContextVar set while handling *this* POST is not visible there. That is not a
    theoretical gap: an identity-spoofing test passed as a unit test (same task) and failed
    over the wire (different task). Tools must use `principal_for_tool` instead.
    """
    return _current.get()


async def principal_for_tool(ctx: Any, audience: str) -> TokenPrincipal | None:
    """Resolve the caller's principal from inside an MCP tool.

    Reads the bearer token from the request carrying *this* tool call, because that is the
    only source that is reliably per-message. Falls back to the SDK's own auth context and
    then to the ASGI ContextVar, so this keeps working if the SDK's plumbing is adopted or
    the transport changes.
    """
    token = _token_from_sdk_context(ctx)
    if token:
        try:
            return await authenticate_access_token(token, expected_audience=audience)
        except RoomError:
            # The transport already rejected invalid tokens; reaching here means the token
            # became invalid mid-session (revoked, expired). Treat as unauthenticated.
            log.info("token presented to a tool is no longer valid")
            return None
    return _current.get()


def _token_from_sdk_context(ctx: Any) -> str | None:
    """Best-effort bearer extraction from the MCP request context.

    Two sources, both optional depending on SDK version and transport, which is why each
    is guarded rather than assumed:
      * the SDK's authenticated-user context, populated when FastMCP's own auth is wired;
      * the ASGI request attached to this JSON-RPC message.
    """
    try:
        from mcp.server.auth.middleware.auth_context import get_access_token

        access = get_access_token()
        if access is not None and getattr(access, "token", None):
            return str(access.token)
    except Exception:  # pragma: no cover - SDK without auth context
        pass

    request = getattr(getattr(ctx, "request_context", None), "request", None)
    headers = getattr(request, "headers", None)
    if headers is None:
        return None
    value = headers.get("authorization") or headers.get("Authorization")
    if value and value.lower().startswith("bearer "):
        return value[7:].strip() or None
    return None


async def _is_live_invitation(token: str) -> bool:
    """Whether this bearer is a currently-redeemable invitation.

    Only enough to let the request reach the tools; it deliberately grants nothing and
    resolves nothing. `join_room` authenticates the invitation again for itself, so a token
    that dies between this check and that one is still refused where it matters.
    """
    from ...core import rooms

    try:
        await rooms.authenticate_invitation(token)
    except RoomError:
        return False
    return True


def require_principal() -> TokenPrincipal:
    principal = _current.get()
    if principal is None:
        raise Unauthenticated(
            "This MCP endpoint requires an access token. Your client should discover the "
            "authorization server from the 401 challenge and complete the OAuth flow."
        )
    return principal


#: Requests that are allowed through unauthenticated. Nothing here reveals room content:
#: discovery documents exist precisely so an unauthenticated client can learn how to
#: authenticate, and CORS preflight cannot carry an Authorization header by definition.
def _is_public(scope: Any) -> bool:
    if scope.get("method") == "OPTIONS":
        return True
    path = scope.get("path", "")
    return path.startswith("/.well-known/")


class NormalizeMcpPath:
    """Serve `/mcp` without a redirect.

    Starlette mounts match `/mcp/…`, so a request to exactly `/mcp` falls through to the
    router's `redirect_slashes` handling and gets a 307 to `/mcp/`. Two reasons not to
    live with that:

    * the redirect is answered *before* the auth wrapper runs, so an unauthenticated
      client gets a 307 instead of the 401 challenge that starts discovery;
    * some clients drop the `Authorization` header when following a redirect, which turns
      a working configuration into an intermittently failing one.

    Rewriting the path in place costs one comparison and removes both problems. Applied
    to the whole app, before routing.
    """

    def __init__(self, app: Any, *, mount_path: str = "/mcp") -> None:
        self.app = app
        self.mount_path = mount_path

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope.get("type") == "http" and scope.get("path") == self.mount_path:
            scope = dict(scope)
            scope["path"] = f"{self.mount_path}/"
            raw = scope.get("raw_path")
            if isinstance(raw, bytes):
                scope["raw_path"] = raw + b"/"
        await self.app(scope, receive, send)


class McpAuthMiddleware:
    """Require a bearer token for the wrapped app; answer 401 with a discovery pointer.

    Auth can be disabled for local development (`MCP_REQUIRE_AUTH=false`), which is why
    the startup guard in `config.check_public_safety` also refuses insecure public
    exposure — a single switch guarding a public endpoint would be too easy to leave off.
    """

    def __init__(self, app: Any, *, resource_metadata_url: str, audience: str) -> None:
        self.app = app
        self.resource_metadata_url = resource_metadata_url
        self.audience = audience

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] != "http" or _is_public(scope):
            await self.app(scope, receive, send)
            return

        if not settings.mcp_require_auth:
            token = _bearer_from(scope)
            if token:
                # Even in permissive mode, honour a presented token so local testing
                # exercises the same identity path as production.
                try:
                    _current.set(
                        await authenticate_access_token(token, expected_audience=self.audience)
                    )
                except RoomError:
                    log.debug("ignoring invalid token while MCP_REQUIRE_AUTH is off")
            await self.app(scope, receive, send)
            return

        token = _bearer_from(scope)
        if not token:
            await self._challenge(
                send,
                status=401,
                error="unauthorized",
                description=(
                    "An access token is required. Discover the authorization server from "
                    "the resource metadata in the WWW-Authenticate header."
                ),
            )
            return

        try:
            principal = await authenticate_access_token(token, expected_audience=self.audience)
        except Unauthenticated as exc:
            # An invitation is also a credential here — narrowly. It authorizes joining the
            # one room it names and nothing else, which is what lets a stranger begin at
            # all: completing OAuth requires an account, and on this instance only the
            # operator has one, so before this the invited party had no way in (D-025).
            #
            # The blast radius is bounded by the tools rather than by this check:
            # `create_room` takes an explicit `principal_token` argument, and every other
            # tool needs a participant token that only joining produces. `join_room` then
            # re-resolves the invitation itself.
            if not settings.require_account_for_join and await _is_live_invitation(token):
                await self.app(scope, receive, send)
                return
            await self._challenge(send, status=401, error="invalid_token", description=exc.message)
            return
        except Forbidden as exc:
            # A valid token for the wrong resource. 403, not 401: re-authenticating with
            # the same credential would not help, and a 401 would send the client into a
            # pointless discovery loop.
            await self._challenge(send, status=403, error="invalid_token", description=exc.message)
            return

        token_context = _current.set(principal)
        try:
            await self.app(scope, receive, send)
        finally:
            _current.reset(token_context)

    async def _challenge(self, send: Any, *, status: int, error: str, description: str) -> None:
        challenge = (
            f'Bearer error="{error}", error_description="{description}", '
            f'resource_metadata="{self.resource_metadata_url}"'
        )
        body = json.dumps({"error": error, "error_description": description}).encode()
        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"www-authenticate", challenge.encode()),
                    (b"content-length", str(len(body)).encode()),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})


def _bearer_from(scope: Any) -> str | None:
    for name, value in scope.get("headers", ()):
        if name.lower() == b"authorization":
            decoded = value.decode("latin-1").strip()
            if decoded.lower().startswith("bearer "):
                return decoded[7:].strip() or None
            return None
    return None
