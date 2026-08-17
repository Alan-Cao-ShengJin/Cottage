"""Walk the whole OAuth + MCP flow the way ChatGPT will, against a running server.

Given only a server URL, a client must be able to:
  1. get 401 with a resource_metadata pointer,
  2. discover the protected resource, then the authorization server,
  3. register itself dynamically,
  4. sign a human in and send them through consent,
  5. exchange the code with PKCE,
  6. call MCP tools with the resulting token,
  7. and be seen in the room under the identity the human chose — not a name it picked.

**Why this exists as a script rather than only as unit tests.** It has already caught two
bugs the suite could not:

* the ASGI ContextVar carrying the authenticated principal is invisible inside a tool,
  because streamable HTTP runs tool calls in the session's task — created on an earlier
  request. The unit test set the var in the same task and passed;
* identity resolution was correct and the room *still* displayed a spoofed name, because
  `join_room` accepts a per-room display name and the client's value was winning.

Both only appear when a real client drives a real server. Run it after any change to the
auth path, the MCP adapter, or the join flow.

**And keep it in step with the payloads.** This script rotted once: it asserted on `p["id"]`
after the MCP adapter renamed that key to `participant_id` for compact payloads, and nothing
caught it for four commits because no gate stage runs this file. It failed on first contact
with the live deployment instead (D-022). If you rename a field an adapter returns, change it
here in the same commit.

Usage (server must already be running with MCP_REQUIRE_AUTH=true):

    $env:OPERATOR_EMAIL = "you@example.com"
    $env:OPERATOR_PASSWORD = "your password"  # omit to be prompted
    backend\\.venv\\Scripts\\python.exe scripts\\verify_oauth_flow.py <base-url> <principal-token>

Against the live instance, which is where it is most meaningful — the container runs a
different Python than the gate does:

    backend\\.venv\\Scripts\\python.exe scripts\\verify_oauth_flow.py https://agent-rooms.fly.dev $env:OPERATOR_TOKEN
"""

from __future__ import annotations

import asyncio
import base64
import getpass
import hashlib
import json
import os
import re
import secrets
import sys
from urllib.parse import parse_qs, urlparse

import httpx

BASE = (sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8100").rstrip("/")
PRINCIPAL = sys.argv[2] if len(sys.argv) > 2 else "dev-owner-token"
EMAIL = os.getenv("OPERATOR_EMAIL", "dev@example.com")
REDIRECT = "https://chatgpt.com/aip/callback"
LOOPBACK_REDIRECT = "http://localhost:3118/callback"


def ok(label: str, detail: str = "") -> None:
    print(f"  [ok] {label}{(' - ' + detail) if detail else ''}")


def unwrap(result):
    if getattr(result, "structuredContent", None):
        sc = result.structuredContent
        return sc.get("result", sc) if isinstance(sc, dict) else sc
    for block in result.content or []:
        text = getattr(block, "text", None)
        if text:
            try:
                return json.loads(text)
            except ValueError:
                return text
    return None


def hidden(page: str, name: str) -> str:
    match = re.search(rf'name="{re.escape(name)}" value="([^"]+)"', page)
    assert match, f"missing hidden field {name!r}"
    return match.group(1)


async def authorize(
    http: httpx.AsyncClient,
    endpoint: str,
    *,
    client_id: str,
    resource: str,
    verifier: str,
    password: str,
    state: str | None = None,
    redirect_uri: str = REDIRECT,
) -> str:
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
        .rstrip(b"=")
        .decode()
    )
    page = await http.get(
        endpoint,
        params={
            "client_id": client_id,
            "response_type": "code",
            "redirect_uri": redirect_uri,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "scope": "agent",
            "state": state or "",
            "resource": resource,
        },
    )
    assert page.status_code == 200, page.status_code

    if 'name="password"' in page.text:
        assert "principal_token" not in page.text
        logged_in = await http.post(
            f"{BASE}/oauth/login",
            data={
                "csrf_token": hidden(page.text, "csrf_token"),
                "email": EMAIL,
                "password": password,
            },
        )
        assert logged_in.status_code == 303, logged_in.text[:400]
        page = await http.get(f"{BASE}/oauth/consent")
        assert page.status_code == 200, page.status_code

    assert "cannot rename itself" in page.text
    submitted = await http.post(
        f"{BASE}/oauth/authorize",
        data={
            "csrf_token": hidden(page.text, "csrf_token"),
            "new_agent_name": "ChatGPT (Alan)",
        },
    )
    if urlparse(redirect_uri).hostname in {"localhost", "127.0.0.1", "::1"}:
        assert submitted.status_code == 303, submitted.text[:400]
        handoff = submitted.headers["location"]
        assert handoff.startswith("/oauth/complete#callback=")
        callback = parse_qs(urlparse(handoff).fragment)["callback"][0]
        completion = await http.get(f"{BASE}/oauth/complete")
        assert completion.status_code == 200, completion.text[:400]
        assert "Authorization approved" in completion.text
        assert parse_qs(urlparse(callback).query)["code"][0] not in completion.text
        query = parse_qs(urlparse(callback).query)
    else:
        assert submitted.status_code == 302, submitted.text[:400]
        query = parse_qs(urlparse(submitted.headers["location"]).query)
    if state is not None:
        assert query["state"][0] == state
    return query["code"][0]


async def main() -> int:
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    password = os.getenv("OPERATOR_PASSWORD") or getpass.getpass(
        f"Password for {EMAIL}: "
    )
    async with httpx.AsyncClient(timeout=20, follow_redirects=False) as http:
        # -- 1. the challenge that starts discovery --------------------------
        print("1. unauthenticated MCP call")
        challenge_response = await http.post(
            f"{BASE}/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            headers={"Accept": "application/json, text/event-stream"},
        )
        assert challenge_response.status_code == 401, challenge_response.status_code
        www = challenge_response.headers.get("www-authenticate", "")
        assert "resource_metadata=" in www, www
        ok("401 with resource_metadata pointer")
        metadata_url = re.search(r'resource_metadata="([^"]+)"', www).group(1)

        # -- 2. discovery ----------------------------------------------------
        print("2. discovery")
        resource = (await http.get(metadata_url)).json()
        assert resource["resource"].endswith("/mcp"), resource
        auth_server_url = resource["authorization_servers"][0]
        ok("protected resource", resource["resource"])

        meta = (
            await http.get(f"{auth_server_url}/.well-known/oauth-authorization-server")
        ).json()
        assert meta["code_challenge_methods_supported"] == ["S256"], meta
        ok("authorization server", meta["issuer"])

        # -- 3. dynamic client registration ----------------------------------
        print("3. dynamic client registration")
        registration = await http.post(
            meta["registration_endpoint"],
            json={
                "client_name": "Cross-host OAuth verifier",
                "redirect_uris": [REDIRECT, LOOPBACK_REDIRECT],
            },
        )
        assert registration.status_code == 201, registration.text
        client_id = registration.json()["client_id"]
        assert "client_secret" not in registration.json()
        ok("registered", client_id)

        # -- 4. consent (a human does this in a browser) ----------------------
        print("4. consent")
        verifier = secrets.token_urlsafe(48)
        code = await authorize(
            http,
            meta["authorization_endpoint"],
            client_id=client_id,
            resource=resource["resource"],
            verifier=verifier,
            password=password,
            state="opaque",
        )
        ok("password login and consent bound the identity")
        ok("code issued, bound to 'ChatGPT (Alan)'")

        # A desktop/CLI client whose local listener is unavailable still receives a
        # refresh-safe Cottage handoff page and a PKCE-exchangeable callback URL.
        loopback_verifier = secrets.token_urlsafe(48)
        loopback_code = await authorize(
            http,
            meta["authorization_endpoint"],
            client_id=client_id,
            resource=resource["resource"],
            verifier=loopback_verifier,
            password=password,
            state="loopback",
            redirect_uri=LOOPBACK_REDIRECT,
        )
        loopback_exchange = await http.post(
            meta["token_endpoint"],
            data={
                "grant_type": "authorization_code",
                "client_id": client_id,
                "code": loopback_code,
                "redirect_uri": LOOPBACK_REDIRECT,
                "code_verifier": loopback_verifier,
                "resource": resource["resource"],
            },
        )
        assert loopback_exchange.status_code == 200, loopback_exchange.text
        ok("loopback fallback page preserved a valid PKCE handoff")

        # -- 5. token exchange ------------------------------------------------
        print("5. token exchange")
        wrong = await http.post(
            meta["token_endpoint"],
            data={
                "grant_type": "authorization_code",
                "client_id": client_id,
                "code": code,
                "redirect_uri": REDIRECT,
                "code_verifier": secrets.token_urlsafe(48),
                "resource": resource["resource"],
            },
        )
        assert wrong.status_code == 400 and wrong.json()["error"] == "invalid_grant"
        ok("wrong PKCE verifier refused")

        exchanged = await http.post(
            meta["token_endpoint"],
            data={
                "grant_type": "authorization_code",
                "client_id": client_id,
                "code": code,
                "redirect_uri": REDIRECT,
                "code_verifier": verifier,
                "resource": resource["resource"],
            },
        )
        assert exchanged.status_code == 200, exchanged.text
        tokens = exchanged.json()
        access = tokens["access_token"]
        ok("access token issued", f"expires_in={tokens['expires_in']}s")

        replay = await http.post(
            meta["token_endpoint"],
            data={
                "grant_type": "authorization_code",
                "client_id": client_id,
                "code": code,
                "redirect_uri": REDIRECT,
                "code_verifier": verifier,
                "resource": resource["resource"],
            },
        )
        assert replay.status_code == 400, replay.text
        ok("code replay refused")

        # The replay burned the token it had already bought.
        after_replay = await http.post(
            f"{BASE}/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            headers={
                "Accept": "application/json, text/event-stream",
                "Authorization": f"Bearer {access}",
            },
        )
        assert after_replay.status_code == 401, after_replay.status_code
        ok("tokens from the replayed code were revoked")

        # -- 6. a clean run, then use the token over MCP -----------------------
        print("6. fresh authorization, then MCP with the token")
        verifier = secrets.token_urlsafe(48)
        code = await authorize(
            http,
            meta["authorization_endpoint"],
            client_id=client_id,
            resource=resource["resource"],
            verifier=verifier,
            password=password,
        )
        tokens = (
            await http.post(
                meta["token_endpoint"],
                data={
                    "grant_type": "authorization_code",
                    "client_id": client_id,
                    "code": code,
                    "redirect_uri": REDIRECT,
                    "code_verifier": verifier,
                    "resource": resource["resource"],
                },
            )
        ).json()
        access = tokens["access_token"]

        # A room to join, created by the human out of band.
        room = (
            await http.post(
                f"{BASE}/api/rooms",
                headers={"Authorization": f"Bearer {PRINCIPAL}"},
                json={"name": "OAuth wire room", "purpose": "prove the flow"},
            )
        ).json()
        join_token = room["join_token"]

    async with streamablehttp_client(
        f"{BASE}/mcp", headers={"Authorization": f"Bearer {access}"}
    ) as (read, write, _):
        async with ClientSession(read, write) as session:
            init = await session.initialize()
            ok("MCP initialize with bearer token", init.serverInfo.name)

            joined = unwrap(
                await session.call_tool(
                    "join_room",
                    {
                        "invitation_token": join_token,
                        # The lie: the identity must come from the token, not this.
                        "display_name": "Totally Someone Else",
                        "execution_mode": "human_turn_only",
                    },
                )
            )
            assert joined.get("ok"), joined
            ok("joined", f"mode=human_turn_only lease={joined['max_lease_seconds']}s")

            state = unwrap(
                await session.call_tool(
                    "get_room_state", {"participant_token": joined["participant_token"]}
                )
            )
            # Compact MCP shape (D-022): `participant_id` / `name` / flattened `liveness`.
            # These key names are part of what this script verifies — a rename that the
            # adapter makes and this script does not follow is exactly the kind of drift
            # that leaves the standing protection green against a payload no client sees.
            me = next(
                p
                for p in state["participants"]
                if p["participant_id"] == joined["participant_id"]
            )
            name = me["name"]
            assert name == "ChatGPT (Alan)", f"identity was spoofable: {name!r}"
            ok("identity came from the token, not the argument", name)
            assert me["liveness"] == "attended", me
            ok("graded honestly", me["liveness"])

    print("\nOAUTH + MCP WIRE FLOW: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
