"""OAuth 2.1 authorization for MCP clients.

A hosted agent host discovers and completes this flow before it can call a single tool, so
these are not hardening extras — they are the connection path. The properties worth pinning:

* discovery documents exist and say the right things (a client cannot be configured around
  a missing one);
* PKCE is mandatory and `plain` is refused, because clients are public and get no secret;
* a code is single-use, and a *replay* is treated as theft rather than a stale request;
* tokens are bound to one resource and rejected elsewhere;
* **the human binds the identity** — an agent cannot name itself, and cannot be authorized
  as an identity its consenting human does not own;
* an unauthenticated call gets a 401 carrying the pointer that starts discovery.
"""

from __future__ import annotations

import base64
import hashlib
import secrets

import httpx
import pytest

from app.api.oauth import authorization_server_metadata, protected_resource_metadata
from app.core import oauth, rooms
from app.core.errors import Forbidden, Unauthenticated
from app.core.oauth import OAuthError
from app.db import database as db
from app.main import app
from app.util import iso_in

pytestmark = pytest.mark.asyncio

REDIRECT = "https://chatgpt.com/aip/callback"


def _pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(48)
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    )
    return verifier, challenge


async def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://arp.test")


@pytest.fixture
async def registered(fresh_db):
    return await oauth.register_client(client_name="ChatGPT", redirect_uris=[REDIRECT])


@pytest.fixture
async def owner(fresh_db, org):
    """A human with a principal token, as the consent screen requires."""
    org_id, user_id = org
    token = await rooms.issue_principal_token(
        subject_kind="user", subject_id=user_id, org_id=org_id, label="test"
    )
    return {"org_id": org_id, "user_id": user_id, "token": token}


async def _authorized_code(registered, owner, *, challenge: str, resource: str | None = None):
    request = await oauth.validate_authorization_request(
        client_id=registered.client_id,
        redirect_uri=REDIRECT,
        response_type="code",
        code_challenge=challenge,
        code_challenge_method="S256",
        scope="agent",
        state="xyz",
        resource=resource,
    )
    identity = await rooms.ensure_identity(
        org_id=owner["org_id"], owner_user_id=owner["user_id"], display_name="ChatGPT (Alan)"
    )
    code = await oauth.issue_authorization_code(request, agent_identity=identity)
    return code, identity


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


def test_protected_resource_metadata_points_at_an_authorization_server():
    """RFC 9728. Without this a client has no idea who guards the resource."""
    meta = protected_resource_metadata()
    assert meta["resource"].endswith("/mcp")
    assert meta["authorization_servers"], "a client cannot discover anything from an empty list"
    assert "agent" in meta["scopes_supported"]


def test_authorization_server_metadata_advertises_only_s256():
    """Advertising `plain` would invite a client to use it. Public clients get no secret,
    so the code alone must never be enough."""
    meta = authorization_server_metadata()
    assert meta["code_challenge_methods_supported"] == ["S256"]
    assert meta["token_endpoint_auth_methods_supported"] == ["none"]
    for endpoint in ("authorization_endpoint", "token_endpoint", "registration_endpoint"):
        assert meta[endpoint].startswith("http"), endpoint


async def test_discovery_documents_are_served_unauthenticated(fresh_db):
    """They must be reachable without a token; they are how you learn to get one."""
    async with await _client() as client:
        for path in (
            "/.well-known/oauth-protected-resource",
            "/.well-known/oauth-protected-resource/mcp",
            "/.well-known/oauth-authorization-server",
        ):
            response = await client.get(path)
            assert response.status_code == 200, path
            assert response.json()


# ---------------------------------------------------------------------------
# Dynamic client registration
# ---------------------------------------------------------------------------


async def test_registration_issues_a_client_id_and_no_secret(fresh_db):
    async with await _client() as client:
        response = await client.post(
            "/oauth/register",
            json={"client_name": "ChatGPT", "redirect_uris": [REDIRECT]},
        )
    assert response.status_code == 201
    body = response.json()
    assert body["client_id"].startswith("cli_")
    assert "client_secret" not in body, "public clients must not receive a secret"
    assert body["token_endpoint_auth_method"] == "none"


@pytest.mark.parametrize(
    "bad_uri",
    [
        "http://evil.example.com/callback",  # remote http would leak the code
        "ftp://example.com/cb",
    ],
)
async def test_registration_rejects_unsafe_redirect_uris(fresh_db, bad_uri):
    with pytest.raises(OAuthError) as exc:
        await oauth.register_client(client_name="x", redirect_uris=[bad_uri])
    assert exc.value.error == "invalid_redirect_uri"


async def test_registration_allows_loopback_http_for_native_clients(fresh_db):
    client = await oauth.register_client(
        client_name="local", redirect_uris=["http://127.0.0.1:53682/callback"]
    )
    assert client.client_id


# ---------------------------------------------------------------------------
# Authorization request validation
# ---------------------------------------------------------------------------


async def test_missing_pkce_challenge_is_refused(registered):
    with pytest.raises(OAuthError) as exc:
        await oauth.validate_authorization_request(
            client_id=registered.client_id,
            redirect_uri=REDIRECT,
            response_type="code",
            code_challenge=None,
            code_challenge_method="S256",
            scope="agent",
            state=None,
            resource=None,
        )
    assert exc.value.error == "invalid_request"
    assert "PKCE" in exc.value.description


async def test_plain_pkce_method_is_refused(registered):
    _, challenge = _pkce()
    with pytest.raises(OAuthError) as exc:
        await oauth.validate_authorization_request(
            client_id=registered.client_id,
            redirect_uri=REDIRECT,
            response_type="code",
            code_challenge=challenge,
            code_challenge_method="plain",
            scope="agent",
            state=None,
            resource=None,
        )
    assert exc.value.error == "invalid_request"


async def test_unregistered_redirect_uri_is_refused(registered):
    """Never honour an unregistered URI — that is how codes reach an attacker."""
    _, challenge = _pkce()
    with pytest.raises(OAuthError) as exc:
        await oauth.validate_authorization_request(
            client_id=registered.client_id,
            redirect_uri="https://attacker.example.com/cb",
            response_type="code",
            code_challenge=challenge,
            code_challenge_method="S256",
            scope="agent",
            state=None,
            resource=None,
        )
    assert exc.value.error == "invalid_request"


async def test_unknown_client_is_refused(fresh_db):
    _, challenge = _pkce()
    with pytest.raises(OAuthError) as exc:
        await oauth.validate_authorization_request(
            client_id="cli_does_not_exist",
            redirect_uri=REDIRECT,
            response_type="code",
            code_challenge=challenge,
            code_challenge_method="S256",
            scope="agent",
            state=None,
            resource=None,
        )
    assert exc.value.error == "invalid_client"


# ---------------------------------------------------------------------------
# Token exchange
# ---------------------------------------------------------------------------


async def test_full_code_exchange_yields_a_token_bound_to_the_chosen_identity(registered, owner):
    verifier, challenge = _pkce()
    code, identity = await _authorized_code(registered, owner, challenge=challenge)

    grant = await oauth.exchange_authorization_code(
        code=code,
        client_id=registered.client_id,
        redirect_uri=REDIRECT,
        code_verifier=verifier,
        resource=None,
        expected_audience="https://rooms.test/mcp",
    )
    assert grant.access_token and grant.refresh_token
    assert grant.expires_in > 0

    principal = await oauth.authenticate_access_token(
        grant.access_token, expected_audience="https://rooms.test/mcp"
    )
    # The subject is the identity the *human* selected, not anything the client supplied.
    assert principal.subject_kind == "agent_identity"
    assert principal.identity is not None
    assert principal.identity.id == identity.id
    assert principal.identity.display_name == "ChatGPT (Alan)"
    assert principal.client_id == registered.client_id


async def test_wrong_code_verifier_is_refused(registered, owner):
    _, challenge = _pkce()
    code, _ = await _authorized_code(registered, owner, challenge=challenge)

    with pytest.raises(OAuthError) as exc:
        await oauth.exchange_authorization_code(
            code=code,
            client_id=registered.client_id,
            redirect_uri=REDIRECT,
            code_verifier=secrets.token_urlsafe(48),  # not the one that made the challenge
            resource=None,
            expected_audience="https://rooms.test/mcp",
        )
    assert exc.value.error == "invalid_grant"


async def test_code_is_single_use_and_replay_revokes_the_issued_tokens(registered, owner):
    """A replayed code means the code leaked. The tokens it already bought are burned."""
    verifier, challenge = _pkce()
    code, identity = await _authorized_code(registered, owner, challenge=challenge)

    grant = await oauth.exchange_authorization_code(
        code=code,
        client_id=registered.client_id,
        redirect_uri=REDIRECT,
        code_verifier=verifier,
        resource=None,
        expected_audience="https://rooms.test/mcp",
    )
    # The first token works.
    await oauth.authenticate_access_token(
        grant.access_token, expected_audience="https://rooms.test/mcp"
    )

    with pytest.raises(OAuthError) as exc:
        await oauth.exchange_authorization_code(
            code=code,
            client_id=registered.client_id,
            redirect_uri=REDIRECT,
            code_verifier=verifier,
            resource=None,
            expected_audience="https://rooms.test/mcp",
        )
    assert exc.value.error == "invalid_grant"

    # And the replay burned the tokens the original exchange produced.
    with pytest.raises(Unauthenticated):
        await oauth.authenticate_access_token(
            grant.access_token, expected_audience="https://rooms.test/mcp"
        )
    del identity


async def test_code_issued_to_another_client_cannot_be_exchanged(registered, owner):
    verifier, challenge = _pkce()
    code, _ = await _authorized_code(registered, owner, challenge=challenge)
    other = await oauth.register_client(client_name="Other", redirect_uris=[REDIRECT])

    with pytest.raises(OAuthError) as exc:
        await oauth.exchange_authorization_code(
            code=code,
            client_id=other.client_id,
            redirect_uri=REDIRECT,
            code_verifier=verifier,
            resource=None,
            expected_audience="https://rooms.test/mcp",
        )
    assert exc.value.error == "invalid_grant"


async def test_expired_code_is_refused(registered, owner):
    verifier, challenge = _pkce()
    code, _ = await _authorized_code(registered, owner, challenge=challenge)
    from app.util import hash_token

    await db.execute(
        "UPDATE oauth_authorization_codes SET expires_at = ? WHERE code_hash = ?",
        (iso_in(-10), hash_token(code)),
    )
    with pytest.raises(OAuthError) as exc:
        await oauth.exchange_authorization_code(
            code=code,
            client_id=registered.client_id,
            redirect_uri=REDIRECT,
            code_verifier=verifier,
            resource=None,
            expected_audience="https://rooms.test/mcp",
        )
    assert exc.value.error == "invalid_grant"


async def test_resource_mismatch_is_refused_at_exchange(registered, owner):
    """RFC 8707. A token for another deployment must not be mintable here."""
    verifier, challenge = _pkce()
    code, _ = await _authorized_code(
        registered, owner, challenge=challenge, resource="https://someone-else.test/mcp"
    )
    with pytest.raises(OAuthError) as exc:
        await oauth.exchange_authorization_code(
            code=code,
            client_id=registered.client_id,
            redirect_uri=REDIRECT,
            code_verifier=verifier,
            resource=None,
            expected_audience="https://rooms.test/mcp",
        )
    assert exc.value.error == "invalid_target"


async def test_token_is_rejected_when_presented_to_a_different_resource(registered, owner):
    verifier, challenge = _pkce()
    code, _ = await _authorized_code(registered, owner, challenge=challenge)
    grant = await oauth.exchange_authorization_code(
        code=code,
        client_id=registered.client_id,
        redirect_uri=REDIRECT,
        code_verifier=verifier,
        resource=None,
        expected_audience="https://rooms.test/mcp",
    )
    with pytest.raises(Forbidden):
        await oauth.authenticate_access_token(
            grant.access_token, expected_audience="https://other.test/mcp"
        )


# ---------------------------------------------------------------------------
# Refresh rotation
# ---------------------------------------------------------------------------


async def test_refresh_rotates_and_the_old_token_stops_working(registered, owner):
    verifier, challenge = _pkce()
    code, _ = await _authorized_code(registered, owner, challenge=challenge)
    first = await oauth.exchange_authorization_code(
        code=code,
        client_id=registered.client_id,
        redirect_uri=REDIRECT,
        code_verifier=verifier,
        resource=None,
        expected_audience="https://rooms.test/mcp",
    )

    second = await oauth.refresh_access_token(
        refresh_token=first.refresh_token,
        client_id=registered.client_id,
        resource=None,
        expected_audience="https://rooms.test/mcp",
    )
    assert second.refresh_token != first.refresh_token

    # Reusing the rotated token is treated as theft: the whole chain is revoked.
    with pytest.raises(OAuthError):
        await oauth.refresh_access_token(
            refresh_token=first.refresh_token,
            client_id=registered.client_id,
            resource=None,
            expected_audience="https://rooms.test/mcp",
        )
    with pytest.raises(Unauthenticated):
        await oauth.authenticate_access_token(
            second.access_token, expected_audience="https://rooms.test/mcp"
        )


async def test_revoked_access_token_stops_working(registered, owner):
    verifier, challenge = _pkce()
    code, _ = await _authorized_code(registered, owner, challenge=challenge)
    grant = await oauth.exchange_authorization_code(
        code=code,
        client_id=registered.client_id,
        redirect_uri=REDIRECT,
        code_verifier=verifier,
        resource=None,
        expected_audience="https://rooms.test/mcp",
    )
    async with await _client() as client:
        response = await client.post(
            "/oauth/revoke",
            data={"token": grant.access_token, "client_id": registered.client_id},
        )
    assert response.status_code == 200

    with pytest.raises(Unauthenticated):
        await oauth.authenticate_access_token(
            grant.access_token, expected_audience="https://rooms.test/mcp"
        )


async def test_revoking_an_unknown_token_still_returns_200(fresh_db):
    """Otherwise the endpoint becomes an oracle for whether a token exists."""
    async with await _client() as client:
        response = await client.post("/oauth/revoke", data={"token": "not-a-real-token"})
    assert response.status_code == 200


# ---------------------------------------------------------------------------
# Consent binds identity — the anti-spoofing property
# ---------------------------------------------------------------------------


async def test_a_human_cannot_authorize_an_identity_they_do_not_own(fresh_db, org, owner):
    """Otherwise consent would let anyone borrow somebody else's agent identity."""
    from app.core.errors import InvalidCommand

    other_org, other_user = await rooms.ensure_org_and_user(
        org_name="Other", org_slug="other", email="o@other.test", display_name="Other"
    )
    someone_elses = await rooms.ensure_identity(
        org_id=other_org, owner_user_id=other_user, display_name="Their Agent"
    )

    with pytest.raises(InvalidCommand):
        await oauth.load_identity_for_user(someone_elses.id, owner["user_id"])


async def test_consent_requires_a_user_principal_not_an_agent_token(registered, owner):
    """An agent must not be able to authorize another agent."""
    identity = await rooms.ensure_identity(
        org_id=owner["org_id"], owner_user_id=owner["user_id"], display_name="Agent A"
    )
    agent_token = await rooms.issue_principal_token(
        subject_kind="agent_identity",
        subject_id=identity.id,
        org_id=owner["org_id"],
        label="agent",
    )
    _, challenge = _pkce()

    async with await _client() as client:
        response = await client.post(
            "/oauth/authorize",
            data={
                "principal_token": agent_token,
                "client_id": registered.client_id,
                "redirect_uri": REDIRECT,
                "code_challenge": challenge,
                "scope": "agent",
                "new_agent_name": "Sneaky",
            },
        )
    assert response.status_code == 400
    assert "cannot authorize another agent" in response.text


async def test_consent_screen_states_what_the_client_will_be_able_to_do(registered):
    """A consent screen that does not say what it grants is a speed bump."""
    _, challenge = _pkce()
    async with await _client() as client:
        response = await client.get(
            "/oauth/authorize",
            params={
                "client_id": registered.client_id,
                "response_type": "code",
                "redirect_uri": REDIRECT,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "scope": "agent",
                "state": "xyz",
            },
        )
    assert response.status_code == 200
    page = response.text
    assert "ChatGPT" in page
    assert "cannot rename itself" in page
    assert "join token" in page.lower()
    # And it must say what is withheld, not only what is granted.
    assert "not" in page.lower() and "purge" in page.lower()


async def test_invalid_authorize_request_does_not_redirect(registered):
    """An invalid request is answered directly. Redirecting an unvalidated request is how
    codes reach attacker-controlled URIs."""
    async with await _client() as client:
        response = await client.get(
            "/oauth/authorize",
            params={
                "client_id": registered.client_id,
                "response_type": "code",
                "redirect_uri": "https://attacker.example.com/cb",
                "code_challenge": "x" * 43,
                "code_challenge_method": "S256",
            },
            follow_redirects=False,
        )
    assert response.status_code == 400
    assert "location" not in {k.lower() for k in response.headers}


async def test_successful_consent_redirects_with_a_code_and_state(registered, owner):
    _, challenge = _pkce()
    async with await _client() as client:
        response = await client.post(
            "/oauth/authorize",
            data={
                "principal_token": owner["token"],
                "client_id": registered.client_id,
                "redirect_uri": REDIRECT,
                "code_challenge": challenge,
                "scope": "agent",
                "state": "opaque-state",
                "new_agent_name": "ChatGPT (Alan)",
            },
            follow_redirects=False,
        )
    assert response.status_code == 302
    location = response.headers["location"]
    assert location.startswith(REDIRECT)
    assert "code=" in location
    assert "state=opaque-state" in location
