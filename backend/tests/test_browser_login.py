"""Email/password login at the browser half of MCP OAuth authorization."""

from __future__ import annotations

import base64
import hashlib
import re
import secrets

import httpx
import pytest

from app.core import accounts, oauth, rooms
from app.core.oauth import OAuthError
from app.db import database as db
from app.main import app
from app.util import iso_in

pytestmark = pytest.mark.asyncio

REDIRECT = "https://client.example/callback"
PASSWORD = "correct horse battery staple"


def _pkce() -> str:
    verifier = secrets.token_urlsafe(48)
    return (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    )


def _csrf(page: str) -> str:
    match = re.search(r'name="csrf_token" value="([^"]+)"', page)
    assert match is not None
    return match.group(1)


async def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://arp.test")


async def _registered():
    return await oauth.register_client(client_name="Codex", redirect_uris=[REDIRECT])


async def _begin(client: httpx.AsyncClient, registered) -> httpx.Response:
    return await client.get(
        "/oauth/authorize",
        params={
            "client_id": registered.client_id,
            "response_type": "code",
            "redirect_uri": REDIRECT,
            "code_challenge": _pkce(),
            "code_challenge_method": "S256",
            "scope": "agent",
            "state": "opaque",
        },
    )


async def _provision(org) -> str:
    _, user_id = org
    await accounts.set_password_hash(user_id, accounts.hash_password(PASSWORD))
    return user_id


async def _login(client: httpx.AsyncClient, started: httpx.Response) -> httpx.Response:
    logged_in = await client.post(
        "/oauth/login",
        data={
            "email": "owner@acme.test",
            "password": PASSWORD,
            "csrf_token": _csrf(started.text),
        },
        follow_redirects=False,
    )
    assert logged_in.status_code == 303
    return await client.get(logged_in.headers["location"])


async def test_login_replaces_the_principal_token_form(fresh_db, org):
    await _provision(org)
    registered = await _registered()
    async with await _client() as client:
        page = await _begin(client, registered)
    assert page.status_code == 200
    assert 'name="email"' in page.text
    assert 'name="password"' in page.text
    assert 'name="principal_token"' not in page.text


async def test_wrong_and_unknown_accounts_share_one_failure(fresh_db, org):
    await _provision(org)
    registered = await _registered()
    async with await _client() as client:
        first = await _begin(client, registered)
        wrong = await client.post(
            "/oauth/login",
            data={
                "email": "owner@acme.test",
                "password": "this password is wrong",
                "csrf_token": _csrf(first.text),
            },
        )
        second = await _begin(client, registered)
        unknown = await client.post(
            "/oauth/login",
            data={
                "email": "nobody@example.test",
                "password": "this password is wrong",
                "csrf_token": _csrf(second.text),
            },
        )
    assert wrong.status_code == unknown.status_code == 401
    assert "Incorrect email or password." in wrong.text
    assert "Incorrect email or password." in unknown.text


async def test_fifth_failed_attempt_is_throttled(fresh_db, org):
    await _provision(org)
    registered = await _registered()
    async with await _client() as client:
        responses = []
        for _ in range(accounts.LOGIN_FAILURE_LIMIT):
            started = await _begin(client, registered)
            responses.append(
                await client.post(
                    "/oauth/login",
                    data={
                        "email": "owner@acme.test",
                        "password": "this password is wrong",
                        "csrf_token": _csrf(started.text),
                    },
                )
            )
    assert responses[-2].status_code == 401
    assert responses[-1].status_code == 429
    assert int(responses[-1].headers["retry-after"]) > 0


async def test_login_and_consent_both_require_csrf(fresh_db, org):
    await _provision(org)
    registered = await _registered()
    async with await _client() as client:
        started = await _begin(client, registered)
        refused_login = await client.post(
            "/oauth/login",
            data={
                "email": "owner@acme.test",
                "password": PASSWORD,
                "csrf_token": "wrong",
            },
        )
        assert refused_login.status_code == 403

        started = await _begin(client, registered)
        consent = await _login(client, started)
        refused_consent = await client.post(
            "/oauth/authorize",
            data={"csrf_token": "wrong", "new_agent_name": "Codex"},
        )
    assert consent.status_code == 200
    assert refused_consent.status_code == 403
    assert "code=" not in refused_consent.text


async def test_consent_cannot_select_another_users_identity(fresh_db, org):
    await _provision(org)
    other_org, other_user = await rooms.ensure_org_and_user(
        org_name="Other", org_slug="other", email="other@example.test", display_name="Other"
    )
    other_identity = await rooms.ensure_identity(
        org_id=other_org, owner_user_id=other_user, display_name="Not yours"
    )
    registered = await _registered()
    async with await _client() as client:
        consent = await _login(client, await _begin(client, registered))
        refused = await client.post(
            "/oauth/authorize",
            data={
                "csrf_token": _csrf(consent.text),
                "agent_identity_id": other_identity.id,
            },
            follow_redirects=False,
        )
    assert refused.status_code == 422
    assert "do not own" in refused.text


async def test_logout_and_expiry_end_the_browser_session(fresh_db, org):
    await _provision(org)
    registered = await _registered()
    async with await _client() as client:
        consent = await _login(client, await _begin(client, registered))
        logged_out = await client.post(
            "/oauth/logout",
            data={"csrf_token": _csrf(consent.text)},
            follow_redirects=False,
        )
        assert logged_out.status_code == 303
        after_logout = await client.get(logged_out.headers["location"])
        assert "Sign in to Agent Rooms" in after_logout.text

        consent = await _login(client, await _begin(client, registered))
        del consent
        await db.execute("UPDATE web_sessions SET expires_at = ?", (iso_in(-1),))
        after_expiry = await client.get("/oauth/consent")
    assert "Sign in to Agent Rooms" in after_expiry.text


async def test_a_browser_flow_can_issue_only_one_code(fresh_db, org):
    org_id, user_id = org
    registered = await _registered()
    request = await oauth.validate_authorization_request(
        client_id=registered.client_id,
        redirect_uri=REDIRECT,
        response_type="code",
        code_challenge=_pkce(),
        code_challenge_method="S256",
        scope="agent",
        state=None,
        resource=None,
    )
    _, flow = await oauth.create_browser_authorization_flow(request)
    identity = await rooms.ensure_identity(
        org_id=org_id, owner_user_id=user_id, display_name="Codex"
    )
    assert await oauth.issue_authorization_code_for_browser_flow(flow, agent_identity=identity)
    with pytest.raises(OAuthError):
        await oauth.issue_authorization_code_for_browser_flow(flow, agent_identity=identity)
