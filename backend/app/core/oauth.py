"""OAuth 2.1 authorization for MCP clients.

ChatGPT (and any hosted agent host) attaches to an MCP server by *discovering* its
authorization server and running an authorization-code flow. So this is not an optional
hardening pass — it is the only way a hosted client can connect at all.

Three properties matter more than spec-completeness, and the design is shaped around
them:

1. **The human binds the identity, not the agent.** Before this existed, a client
   redeeming a join token chose its own `display_name`, so identity was a claim. Here the
   authorization code carries an `agent_identity_id` that a human selected at the consent
   screen. The resulting access token's subject is that identity, and the agent cannot
   rename itself.
2. **Public clients only, so PKCE is mandatory.** A dynamically-registered client gets no
   secret — there is nowhere safe to keep one — which means the authorization code alone
   must not be usable. `S256` is the only accepted challenge method; `plain` is refused
   rather than tolerated.
3. **Tokens are bound to a resource.** A token is issued for one `audience` (this MCP
   endpoint) and rejected elsewhere (RFC 8707). Without this, a token leaked from one
   deployment replays against another.

Codes are single-use with a `consumed_at` guard rather than a delete, so a replay is
*detectable* rather than merely unsuccessful. Refresh tokens rotate and record what
replaced them, for the same reason.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import secrets
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

from ..db import database as db
from ..domain.identity import AgentIdentity
from ..util import hash_token, is_past, iso_in, new_token, tokens_equal, utcnow_iso
from . import store
from .errors import Forbidden, InvalidCommand, Unauthenticated

log = logging.getLogger(__name__)

#: Authorization codes are exchanged immediately by a machine; a long life only widens
#: the window for a leaked code.
CODE_TTL_SECONDS = 300
ACCESS_TOKEN_TTL_SECONDS = 8 * 3600
REFRESH_TOKEN_TTL_SECONDS = 30 * 24 * 3600
BROWSER_FLOW_TTL_SECONDS = 10 * 60

#: What a client may ask for. Deliberately coarse: fine-grained scopes belong to the
#: room's own scope model (`domain.room.Scope`), which is enforced per participant and is
#: not something an OAuth client should be able to widen.
SUPPORTED_SCOPES = ("agent",)

ONLY_SUPPORTED_CHALLENGE_METHOD = "S256"


class OAuthError(Exception):
    """An OAuth protocol error, rendered as the spec's JSON error body.

    Distinct from `RoomError` because the wire format is different: OAuth defines its own
    `error` / `error_description` shape and status codes, and clients parse it.
    """

    def __init__(self, error: str, description: str, *, status_code: int = 400) -> None:
        super().__init__(description)
        self.error = error
        self.description = description
        self.status_code = status_code

    def to_payload(self) -> dict[str, str]:
        return {"error": self.error, "error_description": self.description}


# ---------------------------------------------------------------------------
# Dynamic client registration (RFC 7591)
# ---------------------------------------------------------------------------


@dataclass
class RegisteredClient:
    client_id: str
    client_name: str
    redirect_uris: list[str]


async def register_client(
    *, client_name: str, redirect_uris: list[str], grant_types: list[str] | None = None
) -> RegisteredClient:
    """Register a public client. No secret is issued.

    Open registration is what lets ChatGPT attach without us provisioning anything by
    hand. It is safe because a client id grants nothing on its own: every code still
    requires a human to complete the consent screen, and every exchange still requires the
    PKCE verifier.
    """
    if not redirect_uris:
        raise OAuthError("invalid_redirect_uri", "At least one redirect_uri is required.")
    for uri in redirect_uris:
        _validate_redirect_uri(uri)

    client_id = f"cli_{secrets.token_urlsafe(18)}"
    await db.execute(
        """
        INSERT INTO oauth_clients (
            client_id, client_name, redirect_uris, grant_types,
            token_endpoint_auth_method, created_at
        ) VALUES (?,?,?,?,'none',?)
        """,
        (
            client_id,
            client_name[:200],
            db.dumps(redirect_uris),
            db.dumps(grant_types or ["authorization_code", "refresh_token"]),
            utcnow_iso(),
        ),
    )
    log.info("registered oauth client %s (%s)", client_id, client_name[:60])
    return RegisteredClient(
        client_id=client_id, client_name=client_name, redirect_uris=redirect_uris
    )


def _validate_redirect_uri(uri: str) -> None:
    """Reject redirect targets that could be used to exfiltrate a code.

    Loopback HTTP is allowed because native clients legitimately use it; everything else
    must be HTTPS. An open redirect here would hand codes to an attacker even with PKCE,
    since the code is delivered to whatever URI we honour.
    """
    parsed = urlparse(uri)
    if parsed.fragment:
        raise OAuthError("invalid_redirect_uri", "redirect_uri must not contain a fragment.")
    if parsed.scheme == "https":
        return
    if is_loopback_redirect_uri(uri):
        return
    # A private-use scheme is how native clients receive codes. RFC 8252 §7.1 says it
    # should be a reverse-DNS name the client controls (`com.example.app:/cb`), and the
    # dot is what distinguishes that from a general network scheme — accepting any
    # non-http scheme would have let `ftp://` and `ws://` through.
    if "." in parsed.scheme:
        return
    raise OAuthError(
        "invalid_redirect_uri",
        "redirect_uri must be https, a loopback http URL, or a reverse-DNS private-use "
        f"scheme such as com.example.app:/callback — got {uri!r}",
    )


def is_loopback_redirect_uri(uri: str) -> bool:
    """Whether a validated client redirect returns through this device's loopback.

    The authorization API uses this shape, never a client/vendor label, to choose the
    desktop handoff experience. Keep this predicate aligned with `_validate_redirect_uri`:
    it must never classify a URI as loopback that registration would refuse.
    """
    parsed = urlparse(uri)
    return parsed.scheme == "http" and (parsed.hostname or "").lower() in {
        "localhost",
        "127.0.0.1",
        "::1",
    }


async def load_client(client_id: str) -> dict[str, Any]:
    row = await db.fetch_one("SELECT * FROM oauth_clients WHERE client_id = ?", (client_id,))
    if row is None or row["revoked_at"]:
        raise OAuthError("invalid_client", "Unknown or revoked client_id.", status_code=401)
    return {
        "client_id": row["client_id"],
        "client_name": row["client_name"],
        "redirect_uris": db.str_list(row["redirect_uris"]),
        "grant_types": db.str_list(row["grant_types"]),
    }


# ---------------------------------------------------------------------------
# Authorization request + consent
# ---------------------------------------------------------------------------


@dataclass
class AuthorizationRequest:
    """A validated `/authorize` request, ready for a human to consent to."""

    client_id: str
    client_name: str
    redirect_uri: str
    state: str | None
    code_challenge: str
    scope: str
    resource: str | None


@dataclass(frozen=True)
class BrowserAuthorizationFlow:
    """A validated OAuth request held server-side while a human logs in and consents."""

    token_hash: str
    csrf_token: str
    request: AuthorizationRequest
    expires_at: str


async def validate_authorization_request(
    *,
    client_id: str,
    redirect_uri: str | None,
    response_type: str,
    code_challenge: str | None,
    code_challenge_method: str | None,
    scope: str | None,
    state: str | None,
    resource: str | None,
) -> AuthorizationRequest:
    """Validate before showing a consent screen.

    Order matters: anything that would make it unsafe to *redirect* is rejected here with
    a direct error rather than a redirect, because redirecting an invalid request is how
    codes end up at attacker-controlled URIs.
    """
    client = await load_client(client_id)

    if redirect_uri is None:
        if len(client["redirect_uris"]) != 1:
            raise OAuthError(
                "invalid_request",
                "redirect_uri is required when the client registered more than one.",
            )
        redirect_uri = client["redirect_uris"][0]
    elif redirect_uri not in client["redirect_uris"]:
        # Never redirect to an unregistered URI, not even to report the error.
        raise OAuthError("invalid_request", "redirect_uri does not match a registered URI.")

    if response_type != "code":
        raise OAuthError("unsupported_response_type", "Only response_type=code is supported.")
    if not code_challenge:
        raise OAuthError(
            "invalid_request",
            "code_challenge is required: this server registers public clients only, so "
            "PKCE is mandatory.",
        )
    if (code_challenge_method or "plain") != ONLY_SUPPORTED_CHALLENGE_METHOD:
        raise OAuthError(
            "invalid_request",
            f"code_challenge_method must be {ONLY_SUPPORTED_CHALLENGE_METHOD}; "
            "`plain` offers no protection for a public client.",
        )

    requested = [s for s in (scope or "agent").split() if s]
    unknown = [s for s in requested if s not in SUPPORTED_SCOPES]
    if unknown:
        raise OAuthError("invalid_scope", f"Unsupported scope(s): {', '.join(unknown)}")

    return AuthorizationRequest(
        client_id=client_id,
        client_name=client["client_name"],
        redirect_uri=redirect_uri,
        state=state,
        code_challenge=code_challenge,
        scope=" ".join(requested or ["agent"]),
        resource=resource,
    )


async def create_browser_authorization_flow(
    request: AuthorizationRequest,
) -> tuple[str, BrowserAuthorizationFlow]:
    """Persist a validated request and return the opaque browser cookie value."""
    flow_token = new_token()
    csrf_token = new_token()
    now = utcnow_iso()
    expires_at = iso_in(BROWSER_FLOW_TTL_SECONDS)
    await db.execute(
        """
        INSERT INTO oauth_browser_flows (
            flow_hash, csrf_token, client_id, redirect_uri, code_challenge, scope,
            state, resource, created_at, expires_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?)
        """,
        (
            hash_token(flow_token),
            csrf_token,
            request.client_id,
            request.redirect_uri,
            request.code_challenge,
            request.scope,
            request.state,
            request.resource,
            now,
            expires_at,
        ),
    )
    return flow_token, BrowserAuthorizationFlow(
        token_hash=hash_token(flow_token),
        csrf_token=csrf_token,
        request=request,
        expires_at=expires_at,
    )


async def load_browser_authorization_flow(flow_token: str | None) -> BrowserAuthorizationFlow:
    """Load and revalidate a live browser flow without trusting form fields."""
    if not flow_token:
        raise OAuthError(
            "invalid_request",
            "The authorization session is missing or expired. Restart the MCP connection.",
        )
    token_hash = hash_token(flow_token)
    row = await db.fetch_one("SELECT * FROM oauth_browser_flows WHERE flow_hash = ?", (token_hash,))
    if row is None or row["consumed_at"] or is_past(row["expires_at"]):
        raise OAuthError(
            "invalid_request",
            "The authorization session is missing or expired. Restart the MCP connection.",
        )
    request = await validate_authorization_request(
        client_id=row["client_id"],
        redirect_uri=row["redirect_uri"],
        response_type="code",
        code_challenge=row["code_challenge"],
        code_challenge_method=ONLY_SUPPORTED_CHALLENGE_METHOD,
        scope=row["scope"],
        state=row["state"],
        resource=row["resource"],
    )
    return BrowserAuthorizationFlow(
        token_hash=token_hash,
        csrf_token=row["csrf_token"],
        request=request,
        expires_at=row["expires_at"],
    )


async def load_completed_browser_authorization_flow(
    flow_token: str | None,
) -> BrowserAuthorizationFlow:
    """Load a consumed browser flow solely to render its loopback handoff page.

    The authorization code is never stored on this record. It travels in the browser URL
    fragment, which is not sent in the completion-page HTTP request. This lookup proves
    only that this HttpOnly cookie belongs to a live, successfully consumed flow and
    recovers the validated client/redirect metadata needed to render a trustworthy page.
    """
    if not flow_token:
        raise OAuthError(
            "invalid_request",
            "The authorization handoff is missing or expired. Restart the MCP connection.",
        )
    token_hash = hash_token(flow_token)
    row = await db.fetch_one("SELECT * FROM oauth_browser_flows WHERE flow_hash = ?", (token_hash,))
    if row is None or not row["consumed_at"] or is_past(row["expires_at"]):
        raise OAuthError(
            "invalid_request",
            "The authorization handoff is missing or expired. Restart the MCP connection.",
        )
    request = await validate_authorization_request(
        client_id=row["client_id"],
        redirect_uri=row["redirect_uri"],
        response_type="code",
        code_challenge=row["code_challenge"],
        code_challenge_method=ONLY_SUPPORTED_CHALLENGE_METHOD,
        scope=row["scope"],
        state=row["state"],
        resource=row["resource"],
    )
    if not is_loopback_redirect_uri(request.redirect_uri):
        raise OAuthError("invalid_request", "This authorization does not use a local callback.")
    return BrowserAuthorizationFlow(
        token_hash=token_hash,
        csrf_token=row["csrf_token"],
        request=request,
        expires_at=row["expires_at"],
    )


def browser_flow_csrf_matches(flow: BrowserAuthorizationFlow, candidate: str) -> bool:
    return bool(candidate) and tokens_equal(hash_token(candidate), hash_token(flow.csrf_token))


async def issue_authorization_code(
    request: AuthorizationRequest, *, agent_identity: AgentIdentity
) -> str:
    """Mint a single-use code bound to the identity the human chose."""
    code = new_token(32)
    await db.execute(
        """
        INSERT INTO oauth_authorization_codes (
            code_hash, client_id, redirect_uri, code_challenge, code_challenge_method,
            scope, resource, agent_identity_id, org_id, created_at, expires_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            hash_token(code),
            request.client_id,
            request.redirect_uri,
            request.code_challenge,
            ONLY_SUPPORTED_CHALLENGE_METHOD,
            request.scope,
            request.resource,
            agent_identity.id,
            agent_identity.org_id,
            utcnow_iso(),
            iso_in(CODE_TTL_SECONDS),
        ),
    )
    log.info(
        "issued authorization code for client=%s identity=%s",
        request.client_id,
        agent_identity.id,
    )
    return code


async def issue_authorization_code_for_browser_flow(
    flow: BrowserAuthorizationFlow, *, agent_identity: AgentIdentity
) -> str:
    """Consume the browser flow and issue exactly one code in the same transaction."""
    code = new_token(32)
    now = utcnow_iso()
    request = flow.request
    async with db.transaction() as tx:
        affected = await tx.execute(
            "UPDATE oauth_browser_flows SET consumed_at = ? "
            "WHERE flow_hash = ? AND consumed_at IS NULL AND expires_at > ?",
            (now, flow.token_hash, now),
        )
        if affected == 0:
            raise OAuthError(
                "invalid_request",
                "The authorization session was already used or expired. Restart the MCP connection.",
            )
        await tx.execute(
            """
            INSERT INTO oauth_authorization_codes (
                code_hash, client_id, redirect_uri, code_challenge, code_challenge_method,
                scope, resource, agent_identity_id, org_id, created_at, expires_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                hash_token(code),
                request.client_id,
                request.redirect_uri,
                request.code_challenge,
                ONLY_SUPPORTED_CHALLENGE_METHOD,
                request.scope,
                request.resource,
                agent_identity.id,
                agent_identity.org_id,
                now,
                iso_in(CODE_TTL_SECONDS),
            ),
        )
    log.info(
        "issued authorization code for client=%s identity=%s through browser login",
        request.client_id,
        agent_identity.id,
    )
    return code


# ---------------------------------------------------------------------------
# Token exchange
# ---------------------------------------------------------------------------


def _pkce_matches(verifier: str, challenge: str) -> bool:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    expected = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return secrets.compare_digest(expected, challenge)


@dataclass
class TokenGrant:
    access_token: str
    refresh_token: str
    expires_in: int
    scope: str


async def exchange_authorization_code(
    *,
    code: str,
    client_id: str,
    redirect_uri: str | None,
    code_verifier: str | None,
    resource: str | None,
    expected_audience: str,
) -> TokenGrant:
    """Exchange a code for tokens. Single-use, PKCE-verified, audience-bound."""
    if not code_verifier:
        raise OAuthError("invalid_request", "code_verifier is required (PKCE).")

    row = await db.fetch_one(
        "SELECT * FROM oauth_authorization_codes WHERE code_hash = ?", (hash_token(code),)
    )
    if row is None:
        raise OAuthError("invalid_grant", "Unknown authorization code.")

    if row["consumed_at"]:
        # A replay. The legitimate holder already exchanged this code, so the copy came
        # from somewhere it should not have. Revoke what it bought.
        log.warning(
            "authorization code replay detected for client=%s identity=%s",
            row["client_id"],
            row["agent_identity_id"],
        )
        await _revoke_tokens_from_code(row["agent_identity_id"], row["client_id"])
        raise OAuthError("invalid_grant", "Authorization code has already been used.")

    if is_past(row["expires_at"]):
        raise OAuthError("invalid_grant", "Authorization code has expired.")
    if row["client_id"] != client_id:
        raise OAuthError("invalid_grant", "Authorization code was issued to another client.")
    if redirect_uri is not None and redirect_uri != row["redirect_uri"]:
        raise OAuthError("invalid_grant", "redirect_uri does not match the authorization request.")
    if not _pkce_matches(code_verifier, row["code_challenge"]):
        raise OAuthError("invalid_grant", "code_verifier does not match the code_challenge.")

    # RFC 8707: if the client named a resource, it must be the one we serve.
    requested_resource = resource or row["resource"]
    _assert_resource(requested_resource, expected_audience)

    # Consume with a guarded update so two concurrent exchanges cannot both win.
    async with db.transaction() as tx:
        affected = await tx.execute(
            "UPDATE oauth_authorization_codes SET consumed_at = ? "
            "WHERE code_hash = ? AND consumed_at IS NULL",
            (utcnow_iso(), hash_token(code)),
        )
        if affected == 0:
            raise OAuthError("invalid_grant", "Authorization code has already been used.")

    return await _issue_tokens(
        client_id=client_id,
        agent_identity_id=row["agent_identity_id"],
        org_id=row["org_id"],
        scope=row["scope"],
        audience=expected_audience,
    )


def _assert_resource(requested: str | None, expected_audience: str) -> None:
    if requested is None:
        return
    if requested.rstrip("/") != expected_audience.rstrip("/"):
        raise OAuthError(
            "invalid_target",
            f"This authorization server issues tokens for {expected_audience!r}, "
            f"not {requested!r}.",
        )


async def _issue_tokens(
    *, client_id: str, agent_identity_id: str, org_id: str, scope: str, audience: str
) -> TokenGrant:
    access = new_token(32)
    refresh = new_token(32)
    now = utcnow_iso()

    async with db.transaction() as tx:
        await tx.execute(
            """
            INSERT INTO principal_tokens (
                token_hash, subject_kind, subject_id, org_id, label, created_at,
                expires_at, client_id, scope, audience
            ) VALUES (?,'agent_identity',?,?,?,?,?,?,?,?)
            """,
            (
                hash_token(access),
                agent_identity_id,
                org_id,
                f"oauth:{client_id}",
                now,
                iso_in(ACCESS_TOKEN_TTL_SECONDS),
                client_id,
                scope,
                audience,
            ),
        )
        await tx.execute(
            """
            INSERT INTO oauth_refresh_tokens (
                token_hash, client_id, agent_identity_id, org_id, scope, audience,
                created_at, expires_at
            ) VALUES (?,?,?,?,?,?,?,?)
            """,
            (
                hash_token(refresh),
                client_id,
                agent_identity_id,
                org_id,
                scope,
                audience,
                now,
                iso_in(REFRESH_TOKEN_TTL_SECONDS),
            ),
        )

    return TokenGrant(
        access_token=access,
        refresh_token=refresh,
        expires_in=ACCESS_TOKEN_TTL_SECONDS,
        scope=scope,
    )


async def refresh_access_token(
    *, refresh_token: str, client_id: str, resource: str | None, expected_audience: str
) -> TokenGrant:
    """Rotate a refresh token and issue a new access token.

    Rotation means a stolen refresh token is usable at most once before the legitimate
    holder's next refresh invalidates it — and the attempt is visible in the log.
    """
    row = await db.fetch_one(
        "SELECT * FROM oauth_refresh_tokens WHERE token_hash = ?", (hash_token(refresh_token),)
    )
    if row is None:
        raise OAuthError("invalid_grant", "Unknown refresh token.")
    if row["client_id"] != client_id:
        raise OAuthError("invalid_grant", "Refresh token was issued to another client.")
    if row["revoked_at"]:
        if row["rotated_to_hash"]:
            log.warning(
                "rotated refresh token reused for identity=%s; revoking the chain",
                row["agent_identity_id"],
            )
            await _revoke_tokens_from_code(row["agent_identity_id"], row["client_id"])
        raise OAuthError("invalid_grant", "Refresh token has been revoked.")
    if is_past(row["expires_at"]):
        raise OAuthError("invalid_grant", "Refresh token has expired.")

    _assert_resource(resource, expected_audience)

    grant = await _issue_tokens(
        client_id=client_id,
        agent_identity_id=row["agent_identity_id"],
        org_id=row["org_id"],
        scope=row["scope"],
        audience=expected_audience,
    )
    await db.execute(
        "UPDATE oauth_refresh_tokens SET revoked_at = ?, rotated_to_hash = ? WHERE token_hash = ?",
        (utcnow_iso(), hash_token(grant.refresh_token), hash_token(refresh_token)),
    )
    return grant


async def _revoke_tokens_from_code(agent_identity_id: str, client_id: str) -> None:
    """Revoke everything a client holds for one identity, after suspected token theft."""
    now = utcnow_iso()
    async with db.transaction() as tx:
        await tx.execute(
            "UPDATE principal_tokens SET revoked_at = ? "
            "WHERE subject_id = ? AND client_id = ? AND revoked_at IS NULL",
            (now, agent_identity_id, client_id),
        )
        await tx.execute(
            "UPDATE oauth_refresh_tokens SET revoked_at = ? "
            "WHERE agent_identity_id = ? AND client_id = ? AND revoked_at IS NULL",
            (now, agent_identity_id, client_id),
        )


# ---------------------------------------------------------------------------
# Token introspection for the transport
# ---------------------------------------------------------------------------


@dataclass
class TokenPrincipal:
    """An authenticated bearer, resolved from an access token."""

    subject_kind: str
    org_id: str
    identity: AgentIdentity | None
    user_id: str | None
    scope: str
    client_id: str | None


async def authenticate_access_token(token: str, *, expected_audience: str) -> TokenPrincipal:
    """Resolve a bearer token, enforcing expiry, revocation, and audience.

    Audience is checked here rather than at issuance only: a token that was valid for a
    different resource must not work here even though it is otherwise well-formed.
    """
    row = await db.fetch_one(
        "SELECT * FROM principal_tokens WHERE token_hash = ?", (hash_token(token),)
    )
    if row is None:
        raise Unauthenticated("Unknown token.")
    if row["revoked_at"]:
        raise Unauthenticated("Token has been revoked.")
    if is_past(row["expires_at"]):
        raise Unauthenticated("Token has expired.")

    audience = row["audience"]
    if audience and audience.rstrip("/") != expected_audience.rstrip("/"):
        raise Forbidden(
            "This token was issued for a different resource.",
            audience=audience,
            expected=expected_audience,
        )

    # Best-effort usage stamp; never fail a request because the stamp did not write.
    try:
        await db.execute(
            "UPDATE principal_tokens SET last_used_at = ? WHERE token_hash = ?",
            (utcnow_iso(), hash_token(token)),
        )
    except Exception:  # pragma: no cover - diagnostics only
        log.debug("could not stamp last_used_at", exc_info=True)

    if row["subject_kind"] == "agent_identity":
        identity_row = await db.fetch_one(
            "SELECT * FROM agent_identities WHERE id = ?", (row["subject_id"],)
        )
        if identity_row is None:
            raise Unauthenticated("Token subject no longer exists.")
        return TokenPrincipal(
            subject_kind="agent_identity",
            org_id=row["org_id"],
            identity=store.to_identity(identity_row),
            user_id=None,
            scope=row["scope"] or "",
            client_id=row["client_id"],
        )

    user_row = await db.fetch_one("SELECT id FROM users WHERE id = ?", (row["subject_id"],))
    if user_row is None:
        raise Unauthenticated("Token subject no longer exists.")
    return TokenPrincipal(
        subject_kind="user",
        org_id=row["org_id"],
        identity=None,
        user_id=row["subject_id"],
        scope=row["scope"] or "",
        client_id=row["client_id"],
    )


# ---------------------------------------------------------------------------
# Consent support
# ---------------------------------------------------------------------------


async def identities_for_consent(user_id: str) -> list[AgentIdentity]:
    """Agent identities a human may authorize a client to act as."""
    rows = await db.fetch_all(
        "SELECT * FROM agent_identities WHERE owner_user_id = ? ORDER BY created_at DESC",
        (user_id,),
    )
    return [store.to_identity(r) for r in rows]


async def load_identity_for_user(identity_id: str, user_id: str) -> AgentIdentity:
    """Load an identity, refusing one the consenting human does not own.

    Without this check, a human could authorize a client to act as somebody else's agent.
    """
    row = await db.fetch_one(
        "SELECT * FROM agent_identities WHERE id = ? AND owner_user_id = ?",
        (identity_id, user_id),
    )
    if row is None:
        raise InvalidCommand("That agent identity does not exist or you do not own it.")
    return store.to_identity(row)
