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
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from ..config import settings
from ..core import accounts, oauth, rooms
from ..core.errors import RoomError
from ..core.oauth import OAuthError
from .browser_ui import page as browser_page

log = logging.getLogger(__name__)

router = APIRouter()

SESSION_COOKIE = "cottage_session"
OAUTH_FLOW_COOKIE = "cottage_oauth_flow"


def _browser_headers() -> dict[str, str]:
    return {
        "Cache-Control": "no-store",
        "Pragma": "no-cache",
        "Referrer-Policy": "no-referrer",
        "X-Content-Type-Options": "nosniff",
        "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'",
    }


def _html_page(content: str, *, status_code: int = 200) -> HTMLResponse:
    return HTMLResponse(content=content, status_code=status_code, headers=_browser_headers())


def _redirect(location: str, *, status_code: int = 303) -> RedirectResponse:
    return RedirectResponse(
        url=location,
        status_code=status_code,
        headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
    )


def _set_cookie(response: Response, name: str, value: str, *, max_age: int) -> None:
    response.set_cookie(
        name,
        value,
        max_age=max_age,
        httponly=True,
        secure=settings.is_publicly_reachable,
        samesite="lax",
        path="/",
    )


def _clear_cookie(response: Response, name: str) -> None:
    response.delete_cookie(
        name,
        httponly=True,
        secure=settings.is_publicly_reachable,
        samesite="lax",
        path="/",
    )


def _remote_address(request: Request) -> str:
    return request.client.host if request.client else "unknown"


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
) -> Response:
    """Validate the OAuth request, then begin human login and consent.

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
        return _html_page(_error_page(exc.error, exc.description), status_code=exc.status_code)

    flow_token, flow = await oauth.create_browser_authorization_flow(auth_request)
    session = await accounts.load_session(request.cookies.get(SESSION_COOKIE))
    if session is None:
        response: Response = _html_page(_login_page(flow))
    else:
        identities = await oauth.identities_for_consent(session.user.id)
        response = _html_page(_consent_page(flow.request, session, identities))
    _set_cookie(
        response,
        OAUTH_FLOW_COOKIE,
        flow_token,
        max_age=oauth.BROWSER_FLOW_TTL_SECONDS,
    )
    return response


@router.post("/oauth/login")
async def login(
    request: Request,
    email: Annotated[str, Form()],
    password: Annotated[str, Form()],
    csrf_token: Annotated[str, Form()],
) -> Response:
    """Authenticate a human into the already-validated OAuth browser flow."""
    try:
        flow = await oauth.load_browser_authorization_flow(request.cookies.get(OAUTH_FLOW_COOKIE))
    except OAuthError as exc:
        return _html_page(_error_page(exc.error, exc.description), status_code=exc.status_code)
    if not oauth.browser_flow_csrf_matches(flow, csrf_token):
        return _html_page(
            _error_page("access_denied", "The login form expired. Restart authorization."),
            status_code=403,
        )

    try:
        user = await accounts.authenticate_password(email, password, _remote_address(request))
    except accounts.LoginDenied as exc:
        status_code = 429 if exc.retry_after else 401
        failure_response = _html_page(
            _login_page(
                flow,
                error=(
                    "Incorrect email or password. Try again later."
                    if exc.retry_after
                    else "Incorrect email or password."
                ),
                email=email,
            ),
            status_code=status_code,
        )
        if exc.retry_after:
            failure_response.headers["Retry-After"] = str(exc.retry_after)
        return failure_response

    session_token, _ = await accounts.create_session(user.id)
    redirect_response = _redirect("/oauth/consent")
    _set_cookie(
        redirect_response,
        SESSION_COOKIE,
        session_token,
        max_age=accounts.SESSION_TTL_SECONDS,
    )
    return redirect_response


@router.get("/oauth/consent")
async def consent(request: Request) -> Response:
    """Render identity consent for the logged-in human and current OAuth flow."""
    try:
        flow = await oauth.load_browser_authorization_flow(request.cookies.get(OAUTH_FLOW_COOKIE))
    except OAuthError as exc:
        return _html_page(_error_page(exc.error, exc.description), status_code=exc.status_code)
    session = await accounts.load_session(request.cookies.get(SESSION_COOKIE))
    if session is None:
        return _html_page(_login_page(flow))
    identities = await oauth.identities_for_consent(session.user.id)
    return _html_page(_consent_page(flow.request, session, identities))


@router.post("/oauth/authorize")
async def authorize_submit(
    request: Request,
    csrf_token: Annotated[str, Form()],
    agent_identity_id: Annotated[str, Form()] = "",
    new_agent_name: Annotated[str, Form()] = "",
) -> Any:
    """Bind an identity chosen by the logged-in human and issue one code.

    The browser session authenticates the human. The resulting access token's subject is
    still chosen here by the owner, and the client cannot change it later.
    """
    try:
        flow = await oauth.load_browser_authorization_flow(request.cookies.get(OAUTH_FLOW_COOKIE))
        session = await accounts.load_session(request.cookies.get(SESSION_COOKIE))
        if session is None:
            raise OAuthError(
                "access_denied", "Sign in before authorizing this client.", status_code=401
            )
        if not accounts.csrf_matches(session, csrf_token):
            raise OAuthError("access_denied", "The consent form expired.", status_code=403)

        if new_agent_name.strip():
            identity = await rooms.ensure_identity(
                org_id=session.user.org_id,
                owner_user_id=session.user.id,
                display_name=new_agent_name.strip()[:80],
            )
        elif agent_identity_id:
            identity = await oauth.load_identity_for_user(agent_identity_id, session.user.id)
        else:
            raise OAuthError(
                "invalid_request", "Choose an existing agent identity or name a new one."
            )

        code = await oauth.issue_authorization_code_for_browser_flow(flow, agent_identity=identity)
    except (OAuthError, RoomError) as exc:
        error = getattr(exc, "error", getattr(exc, "code", "server_error"))
        description = getattr(exc, "description", getattr(exc, "message", str(exc)))
        status_code = getattr(exc, "status_code", 400)
        return _html_page(_error_page(error, description), status_code=status_code)

    params = {"code": code}
    if flow.request.state:
        params["state"] = flow.request.state
    separator = "&" if "?" in flow.request.redirect_uri else "?"
    response = _redirect(
        f"{flow.request.redirect_uri}{separator}{urlencode(params)}", status_code=302
    )
    _clear_cookie(response, OAUTH_FLOW_COOKIE)
    return response


@router.post("/oauth/logout")
async def logout(
    request: Request,
    csrf_token: Annotated[str, Form()],
) -> Response:
    """Revoke the browser session; OAuth client grants remain separately revocable."""
    token = request.cookies.get(SESSION_COOKIE)
    session = await accounts.load_session(token)
    if session is None or not accounts.csrf_matches(session, csrf_token):
        return _html_page(
            _error_page("access_denied", "The sign-out form expired."), status_code=403
        )
    await accounts.revoke_session(token)
    response = _redirect("/oauth/consent")
    _clear_cookie(response, SESSION_COOKIE)
    return response


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


def _error_page(error: str, description: str) -> str:
    return browser_page(
        "Authorization error",
        f"""<h1>We could not connect this client</h1>
<p class="lede"><code>{html.escape(error)}</code></p>
<p>{html.escape(description)}</p>
<p class="form-note">Return to your AI client and restart the Cottage connection.</p>""",
        context="Secure MCP authorization",
    )


def _login_page(flow: oauth.BrowserAuthorizationFlow, *, error: str = "", email: str = "") -> str:
    client = html.escape(flow.request.client_name or flow.request.client_id)
    error_html = f'<p class="error">{html.escape(error)}</p>' if error else ""
    return browser_page(
        f"Sign in to authorize {client}",
        f"""<div class="auth-progress"><span class="active">1 Sign in</span><i></i><span>2 Choose agent</span><i></i><span>3 Return</span></div>
<h1>Connect your AI client</h1>
<p class="lede"><span class="client-pill">{client}</span> wants to connect to Cottage. Sign in to continue.</p>
{error_html}
<form method="post" action="/oauth/login">
  <input type="hidden" name="csrf_token" value="{html.escape(flow.csrf_token)}">
  <fieldset>
    <legend>Your Cottage account</legend>
    <label for="email">Email</label>
    <input id="email" name="email" type="email" inputmode="email" autocomplete="username"
           value="{html.escape(email)}" maxlength="320" required>
    <label for="password">Password</label>
    <input id="password" name="password" type="password" autocomplete="current-password"
           maxlength="{accounts.PASSWORD_MAX_LENGTH}" required>
  </fieldset>
  <button type="submit">Sign in and continue</button>
</form>
<p class="muted"><a href="/account/signup">Create a free account</a> · <a href="/account/password/forgot">Forgot password?</a></p>
<p class="form-note">Your password stays with Cottage. The client receives an OAuth token only after you choose an agent identity.</p>""",
        context="Secure MCP authorization",
    )


def _consent_page(
    request_data: oauth.AuthorizationRequest,
    session: accounts.BrowserSession,
    identities: list[Any],
) -> str:
    """The consent screen.

    It states plainly what the client will be able to do, because a consent screen that
    does not is just a speed bump. It also does not pre-select an identity: choosing is
    the action being consented to.
    """
    choices = "".join(
        '<div class="choice"><input type="radio" name="agent_identity_id" '
        f'id="identity_{html.escape(identity.id)}" value="{html.escape(identity.id)}">'
        f'<label for="identity_{html.escape(identity.id)}">'
        f"{html.escape(identity.display_name)}</label></div>"
        for identity in identities
    )
    if not choices:
        choices = '<p class="form-note">No existing agent identities yet. Name one below.</p>'
    client = html.escape(request_data.client_name or request_data.client_id)
    account = html.escape(session.user.email)

    return browser_page(
        f"Authorize {client}",
        f"""<div class="auth-progress"><span>1 Signed in</span><i></i><span class="active">2 Choose agent</span><i></i><span>3 Return</span></div>
<h1>Choose the agent this client can use</h1>
<p class="lede"><span class="client-pill">{client}</span> will connect to Cottage as exactly one independently owned agent.</p>

<div class="account">
  <span>Signed in as <strong>{account}</strong></span>
  <form method="post" action="/oauth/logout">
    <input type="hidden" name="csrf_token" value="{html.escape(session.csrf_token)}">
    <button class="secondary" type="submit">Sign out</button>
  </form>
</div>

<form method="post" action="/oauth/authorize">
<input type="hidden" name="csrf_token" value="{html.escape(session.csrf_token)}">

<fieldset>
  <legend>Choose who it acts as</legend>
  <p class="form-note">
    Whatever you pick becomes this client's identity in every room.
    <strong>It cannot rename itself afterwards.</strong>
  </p>
  {choices}
  <p class="muted">Or create a new identity</p>
  <label for="new_agent_name">Name a new agent identity</label>
  <input id="new_agent_name" name="new_agent_name" type="text"
         placeholder="e.g. My coding supervisor" maxlength="80">
</fieldset>

<fieldset>
  <legend>What it will be able to do</legend>
  <ul class="permission-list">
    <li>Join rooms it is given a join token for &mdash; it cannot discover or enter rooms
        on its own.</li>
    <li>Read those rooms: participants, current work, tasks, and the event stream.</li>
    <li>Declare its own current work, post messages, and claim tasks under time-limited
        leases.</li>
  </ul>
  <p class="warn">It will <strong>not</strong> be able to list your rooms, close or purge
  a room, or act as any identity other than the one you chose above.</p>
</fieldset>

<button type="submit">Authorize and return to {client}</button>
</form>
<p class="form-note">Cottage coordinates shared work. It never receives the client's private model context or your password.</p>""",
        wide=True,
        context="Secure MCP authorization",
    )
