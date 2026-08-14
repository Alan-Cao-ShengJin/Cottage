"""OAuth 2.1 endpoints and discovery documents for MCP clients.

A hosted agent host (ChatGPT) is given only a server URL. Everything else it *discovers*:
the protected-resource metadata points at an authorization server, the authorization-server
metadata names the endpoints, and registration is dynamic. So these documents are load-
bearing — a client cannot be configured around a missing one.

The consent screen is the security-critical part, and it is deliberately not a rubber
stamp. It is where a human decides **which agent identity** a client may act as, which is
what turns identity from a name the agent picked into a binding the owner made.
"""

from __future__ import annotations

import html
import logging
from typing import Annotated, Any
from urllib.parse import urlencode

from fastapi import APIRouter, Form, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from ..config import settings
from ..core import oauth, rooms
from ..core.errors import RoomError
from ..core.oauth import OAuthError

log = logging.getLogger(__name__)

router = APIRouter()


def mcp_resource_url() -> str:
    """The canonical identifier of the protected resource: our MCP endpoint.

    Defined in config so the MCP adapter can validate against the same value without
    importing this module (adapters must not depend on `api/`).
    """
    return settings.mcp_resource_url


def issuer_url() -> str:
    return settings.public_base_url.rstrip("/")


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


def protected_resource_metadata() -> dict[str, Any]:
    """RFC 9728. Tells a client which authorization server guards this resource."""
    return {
        "resource": mcp_resource_url(),
        "authorization_servers": [issuer_url()],
        "scopes_supported": list(oauth.SUPPORTED_SCOPES),
        "bearer_methods_supported": ["header"],
        "resource_name": "Agent Rooms",
        "resource_documentation": f"{issuer_url()}/docs",
    }


def authorization_server_metadata() -> dict[str, Any]:
    """RFC 8414. `code_challenge_methods_supported` advertising only S256 is the
    machine-readable form of "public clients must use PKCE"."""
    base = issuer_url()
    return {
        "issuer": base,
        "authorization_endpoint": f"{base}/oauth/authorize",
        "token_endpoint": f"{base}/oauth/token",
        "registration_endpoint": f"{base}/oauth/register",
        "revocation_endpoint": f"{base}/oauth/revoke",
        "scopes_supported": list(oauth.SUPPORTED_SCOPES),
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "token_endpoint_auth_methods_supported": ["none"],
        "code_challenge_methods_supported": [oauth.ONLY_SUPPORTED_CHALLENGE_METHOD],
        "service_documentation": f"{base}/docs",
    }


# Served at both the bare path and the MCP-suffixed path, because clients differ on
# which they probe for a resource mounted under a sub-path.
@router.get("/.well-known/oauth-protected-resource")
@router.get("/.well-known/oauth-protected-resource/mcp")
async def get_protected_resource_metadata() -> dict[str, Any]:
    return protected_resource_metadata()


@router.get("/.well-known/oauth-authorization-server")
@router.get("/.well-known/openid-configuration")
async def get_authorization_server_metadata() -> dict[str, Any]:
    return authorization_server_metadata()


# ---------------------------------------------------------------------------
# Dynamic client registration
# ---------------------------------------------------------------------------


@router.post("/oauth/register", status_code=201)
async def register(payload: dict[str, Any]) -> JSONResponse:
    """RFC 7591. Open registration; no secret is issued (public clients only)."""
    try:
        registered = await oauth.register_client(
            client_name=str(payload.get("client_name") or "Unnamed MCP client"),
            redirect_uris=[str(u) for u in (payload.get("redirect_uris") or [])],
            grant_types=[str(g) for g in (payload.get("grant_types") or [])] or None,
        )
    except OAuthError as exc:
        return JSONResponse(status_code=exc.status_code, content=exc.to_payload())

    return JSONResponse(
        status_code=201,
        content={
            "client_id": registered.client_id,
            "client_name": registered.client_name,
            "redirect_uris": registered.redirect_uris,
            "token_endpoint_auth_method": "none",
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
        },
    )


# ---------------------------------------------------------------------------
# Authorization + consent
# ---------------------------------------------------------------------------


@router.get("/oauth/authorize")
async def authorize(
    request: Request,
    client_id: str = Query(...),
    response_type: str = Query("code"),
    redirect_uri: str | None = Query(None),
    code_challenge: str | None = Query(None),
    code_challenge_method: str | None = Query(None),
    scope: str | None = Query(None),
    state: str | None = Query(None),
    resource: str | None = Query(None),
) -> HTMLResponse:
    """Show the consent screen.

    Validation happens before rendering, and an invalid request is answered directly
    rather than by redirecting — redirecting an unvalidated request is how authorization
    codes end up at attacker-controlled URIs.
    """
    try:
        auth_request = await oauth.validate_authorization_request(
            client_id=client_id,
            redirect_uri=redirect_uri,
            response_type=response_type,
            code_challenge=code_challenge,
            code_challenge_method=code_challenge_method,
            scope=scope,
            state=state,
            resource=resource,
        )
    except OAuthError as exc:
        return HTMLResponse(
            status_code=exc.status_code,
            content=_error_page(exc.error, exc.description),
        )

    return HTMLResponse(content=_consent_page(auth_request, request))


@router.post("/oauth/authorize")
async def authorize_submit(
    principal_token: Annotated[str, Form()],
    client_id: Annotated[str, Form()],
    redirect_uri: Annotated[str, Form()],
    code_challenge: Annotated[str, Form()],
    scope: Annotated[str, Form()] = "agent",
    state: Annotated[str, Form()] = "",
    resource: Annotated[str, Form()] = "",
    agent_identity_id: Annotated[str, Form()] = "",
    new_agent_name: Annotated[str, Form()] = "",
) -> Any:
    """Complete consent: authenticate the human, bind an identity, issue a code.

    The human proves who they are with an organization principal token, then names the
    agent identity the client will act as. That binding is the whole point: the access
    token's subject is chosen here, by the owner, and the client cannot change it later.
    """
    try:
        principal = await rooms.authenticate_principal(principal_token.strip())
        if principal.user is None:
            raise OAuthError(
                "access_denied",
                "Consent requires a user principal token, not an agent token — an agent "
                "cannot authorize another agent.",
                status_code=403,
            )

        if new_agent_name.strip():
            identity = await rooms.ensure_identity(
                org_id=principal.user.org_id,
                owner_user_id=principal.user.id,
                display_name=new_agent_name.strip()[:80],
            )
        elif agent_identity_id:
            identity = await oauth.load_identity_for_user(agent_identity_id, principal.user.id)
        else:
            raise OAuthError(
                "invalid_request", "Choose an existing agent identity or name a new one."
            )

        auth_request = await oauth.validate_authorization_request(
            client_id=client_id,
            redirect_uri=redirect_uri,
            response_type="code",
            code_challenge=code_challenge,
            code_challenge_method=oauth.ONLY_SUPPORTED_CHALLENGE_METHOD,
            scope=scope,
            state=state or None,
            resource=resource or None,
        )
        code = await oauth.issue_authorization_code(auth_request, agent_identity=identity)
    except (OAuthError, RoomError) as exc:
        error = getattr(exc, "error", getattr(exc, "code", "server_error"))
        description = getattr(exc, "description", getattr(exc, "message", str(exc)))
        return HTMLResponse(status_code=400, content=_error_page(error, description))

    params = {"code": code}
    if state:
        params["state"] = state
    separator = "&" if "?" in redirect_uri else "?"
    return RedirectResponse(url=f"{redirect_uri}{separator}{urlencode(params)}", status_code=302)


# ---------------------------------------------------------------------------
# Token
# ---------------------------------------------------------------------------


@router.post("/oauth/token")
async def token(
    grant_type: Annotated[str, Form()],
    client_id: Annotated[str, Form()] = "",
    code: Annotated[str, Form()] = "",
    redirect_uri: Annotated[str, Form()] = "",
    code_verifier: Annotated[str, Form()] = "",
    refresh_token: Annotated[str, Form()] = "",
    resource: Annotated[str, Form()] = "",
) -> JSONResponse:
    """Exchange a code, or rotate a refresh token."""
    try:
        if not client_id:
            raise OAuthError("invalid_client", "client_id is required.", status_code=401)

        if grant_type == "authorization_code":
            grant = await oauth.exchange_authorization_code(
                code=code,
                client_id=client_id,
                redirect_uri=redirect_uri or None,
                code_verifier=code_verifier or None,
                resource=resource or None,
                expected_audience=mcp_resource_url(),
            )
        elif grant_type == "refresh_token":
            grant = await oauth.refresh_access_token(
                refresh_token=refresh_token,
                client_id=client_id,
                resource=resource or None,
                expected_audience=mcp_resource_url(),
            )
        else:
            raise OAuthError(
                "unsupported_grant_type",
                "Supported grants: authorization_code, refresh_token.",
            )
    except OAuthError as exc:
        return JSONResponse(status_code=exc.status_code, content=exc.to_payload())

    return JSONResponse(
        # Tokens must never be cached by an intermediary.
        headers={"Cache-Control": "no-store", "Pragma": "no-cache"},
        content={
            "access_token": grant.access_token,
            "token_type": "Bearer",
            "expires_in": grant.expires_in,
            "refresh_token": grant.refresh_token,
            "scope": grant.scope,
        },
    )


@router.post("/oauth/revoke")
async def revoke(
    token: Annotated[str, Form()],
    client_id: Annotated[str, Form()] = "",
) -> JSONResponse:
    """RFC 7009. Always 200, even for an unknown token — telling a caller whether a token
    existed would turn this into an oracle."""
    from ..db import database as db
    from ..util import hash_token, utcnow_iso

    now = utcnow_iso()
    token_hash = hash_token(token)
    async with db.transaction() as tx:
        await tx.execute(
            "UPDATE principal_tokens SET revoked_at = ? WHERE token_hash = ? AND revoked_at IS NULL",
            (now, token_hash),
        )
        await tx.execute(
            "UPDATE oauth_refresh_tokens SET revoked_at = ? "
            "WHERE token_hash = ? AND revoked_at IS NULL",
            (now, token_hash),
        )
    return JSONResponse(content={})


# ---------------------------------------------------------------------------
# Minimal HTML (no template engine, no external assets)
# ---------------------------------------------------------------------------

_PAGE_CSS = """
:root { color-scheme: light dark; }
body { font-family: ui-sans-serif, system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
       max-width: 34rem; margin: 3rem auto; padding: 0 1.25rem; line-height: 1.5; }
h1 { font-size: 1.35rem; margin-bottom: .25rem; }
p.lede { color: #666; margin-top: 0; }
fieldset { border: 1px solid #ccc; border-radius: 8px; padding: 1rem; margin: 1.25rem 0; }
legend { font-size: .75rem; text-transform: uppercase; letter-spacing: .06em; color: #666; }
label { display: block; margin: .5rem 0 .2rem; font-size: .85rem; font-weight: 600; }
input[type=text], input[type=password] { width: 100%; padding: .5rem; border-radius: 6px;
       border: 1px solid #aaa; font: inherit; box-sizing: border-box; }
button { font: inherit; font-weight: 600; padding: .55rem 1.1rem; border-radius: 6px;
         border: 1px solid #3b5bdb; background: #3b5bdb; color: #fff; cursor: pointer; }
code { background: rgba(127,127,127,.15); padding: .1rem .3rem; border-radius: 4px; }
.warn { border-left: 3px solid #e8590c; padding-left: .75rem; font-size: .85rem; color: #666; }
.choice { display: flex; gap: .5rem; align-items: baseline; margin: .35rem 0; }
"""


def _error_page(error: str, description: str) -> str:
    return f"""<!doctype html><html><head><meta charset="utf-8">
<title>Authorization error</title><style>{_PAGE_CSS}</style></head><body>
<h1>Authorization error</h1>
<p class="lede"><code>{html.escape(error)}</code></p>
<p>{html.escape(description)}</p>
</body></html>"""


def _consent_page(request_data: oauth.AuthorizationRequest, request: Request) -> str:
    """The consent screen.

    It states plainly what the client will be able to do, because a consent screen that
    does not is just a speed bump. It also does not pre-select an identity: choosing is
    the action being consented to.
    """
    hidden = {
        "client_id": request_data.client_id,
        "redirect_uri": request_data.redirect_uri,
        "code_challenge": request_data.code_challenge,
        "scope": request_data.scope,
        "state": request_data.state or "",
        "resource": request_data.resource or "",
    }
    hidden_html = "".join(
        f'<input type="hidden" name="{html.escape(k)}" value="{html.escape(v)}">'
        for k, v in hidden.items()
    )
    client = html.escape(request_data.client_name or request_data.client_id)

    return f"""<!doctype html><html><head><meta charset="utf-8">
<title>Authorize {client}</title><style>{_PAGE_CSS}</style></head><body>
<h1>Authorize {client}</h1>
<p class="lede">It is asking to join Agent Rooms coordination rooms as one of your agents.</p>

<form method="post" action="/oauth/authorize">
{hidden_html}

<fieldset>
  <legend>1 &middot; Prove it is you</legend>
  <label for="principal_token">Your organization principal token</label>
  <input id="principal_token" name="principal_token" type="password" autocomplete="off"
         placeholder="paste your token" required>
  <p class="warn">An agent token will not work here: an agent cannot authorize another
  agent.</p>
</fieldset>

<fieldset>
  <legend>2 &middot; Choose who it acts as</legend>
  <p style="font-size:.85rem;color:#666;margin-top:0">
    Whatever you pick becomes this client's identity in every room.
    <strong>It cannot rename itself afterwards.</strong>
  </p>
  <label for="new_agent_name">Name a new agent identity</label>
  <input id="new_agent_name" name="new_agent_name" type="text"
         placeholder="e.g. ChatGPT (Alan)">
  <p style="font-size:.8rem;color:#666">
    Or paste an existing agent identity id below to reuse one.
  </p>
  <label for="agent_identity_id">Existing agent identity id</label>
  <input id="agent_identity_id" name="agent_identity_id" type="text" placeholder="aid_...">
</fieldset>

<fieldset>
  <legend>3 &middot; What it will be able to do</legend>
  <ul style="font-size:.85rem;color:#666;padding-left:1.1rem">
    <li>Join rooms it is given a join token for &mdash; it cannot discover or enter rooms
        on its own.</li>
    <li>Read those rooms: participants, current work, tasks, and the event stream.</li>
    <li>Declare its own current work, post messages, and claim tasks under time-limited
        leases.</li>
  </ul>
  <p class="warn">It will <strong>not</strong> be able to list your rooms, close or purge
  a room, or act as any identity other than the one you chose above.</p>
</fieldset>

<button type="submit">Authorize</button>
</form>
</body></html>"""
