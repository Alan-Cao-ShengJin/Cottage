"""Free accounts authenticate joins; paid entitlement gates only room creation."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import time
from contextlib import contextmanager

import httpx
import pytest
import stripe

from app.config import settings
from app.core import accounts, billing, rooms
from app.core.errors import PaymentRequired
from app.db import database as db
from app.domain.commands import CreateRoomCommand

pytestmark = pytest.mark.asyncio

PASSWORD = "correct horse battery staple"


@contextmanager
def _settings(**values):
    original = {key: getattr(settings, key) for key in values}
    try:
        for key, value in values.items():
            object.__setattr__(settings, key, value)
        yield
    finally:
        for key, value in original.items():
            object.__setattr__(settings, key, value)


def _csrf(page: str) -> str:
    match = re.search(r'name="csrf_token" value="([^"]+)"', page)
    assert match
    return match.group(1)


def _verification_token(page: str) -> str:
    match = re.search(r"/account/verify\?token=([^\"&]+)", page)
    assert match
    return match.group(1)


async def _client():
    from app.main import app

    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://arp.test")


async def _registered(email: str = "free@example.test"):
    result = await accounts.register_account(
        email=email, display_name="Free User", password=PASSWORD
    )
    await accounts.consume_email_verification(result.verification_token)
    return result.user


async def test_public_signup_requires_csrf_and_email_verification(fresh_db):
    with _settings(public_signup_enabled=True):
        async with await _client() as client:
            page = await client.get("/account/signup")
            refused = await client.post(
                "/account/signup",
                data={
                    "csrf_token": "wrong",
                    "email": "new@example.test",
                    "display_name": "New User",
                    "password": PASSWORD,
                },
            )
            created = await client.post(
                "/account/signup",
                data={
                    "csrf_token": _csrf(page.text),
                    "email": "new@example.test",
                    "display_name": "New User",
                    "password": PASSWORD,
                },
            )
            login_page = await client.get("/account/login")
            before_verify = await client.post(
                "/account/login",
                data={
                    "csrf_token": _csrf(login_page.text),
                    "email": "new@example.test",
                    "password": PASSWORD,
                },
            )
            verified = await client.get(
                f"/account/verify?token={_verification_token(created.text)}",
                follow_redirects=False,
            )
            dashboard = await client.get(verified.headers["location"])

    credential = await db.fetch_one(
        "SELECT password_hash FROM user_password_credentials c "
        "JOIN users u ON u.id = c.user_id WHERE u.email = ?",
        ("new@example.test",),
    )
    assert refused.status_code == 403
    assert created.status_code == 201
    assert before_verify.status_code == 401
    assert verified.status_code == 303
    assert "Free account" in dashboard.text
    assert credential is not None and credential["password_hash"] != PASSWORD


async def test_password_reset_rotates_password_and_revokes_sessions(fresh_db):
    user = await _registered()
    old_session, _ = await accounts.create_session(user.id)
    issued = await accounts.create_password_reset(user.email)
    assert issued is not None
    _, token = issued

    await accounts.reset_password(token, "a different secure password")
    assert await accounts.load_session(old_session) is None
    with pytest.raises(accounts.LoginDenied):
        await accounts.authenticate_password(user.email, PASSWORD, "127.0.0.1")
    assert (
        await accounts.authenticate_password(user.email, "a different secure password", "127.0.0.1")
    ).id == user.id


async def test_only_creator_entitlement_can_start_rooms(fresh_db):
    user = await _registered()
    principal = rooms.Principal(kind="user", org_id=user.org_id, user=user)
    with _settings(enforce_creator_subscription=True):
        with pytest.raises(PaymentRequired) as exc:
            await rooms.create_room(
                principal=principal, command=CreateRoomCommand(name="Not yet paid")
            )
        assert exc.value.details["upgrade_url"].endswith("/account/billing")

        await billing.grant_creator_entitlement(user.org_id, source="test")
        created = await rooms.create_room(
            principal=principal, command=CreateRoomCommand(name="Creator room")
        )
    assert created.room.name == "Creator room"


async def test_checkout_success_redirect_never_grants_creator_entitlement(fresh_db):
    user = await _registered()
    session_token, _ = await accounts.create_session(user.id)
    async with await _client() as client:
        client.cookies.set("cottage_session", session_token)
        dashboard = await client.get("/account?checkout=success")

    assert dashboard.status_code == 200
    assert "activation follows the signed webhook" in dashboard.text
    assert "Free account" in dashboard.text
    assert not await billing.has_creator_entitlement(user.org_id)


async def test_hosted_http_join_requires_free_account_plus_invitation(fresh_db, make_room):
    from app.domain.room import RoomVisibility

    room = await make_room(visibility=RoomVisibility.CROSS_ORG)
    user = await _registered()
    session_token, _ = await accounts.create_session(user.id)

    with _settings(require_account_for_join=True):
        async with await _client() as stranger:
            refused = await stranger.post(
                "/api/rooms/join",
                headers={"Authorization": f"Bearer {room.join_token}"},
                json={
                    "invitation_token": room.join_token,
                    "display_name": "Free User",
                },
            )
        async with await _client() as signed_in:
            signed_in.cookies.set("cottage_session", session_token)
            joined = await signed_in.post(
                "/api/rooms/join",
                json={
                    "invitation_token": room.join_token,
                    "display_name": "Free User",
                },
            )
    assert refused.status_code == 401
    assert joined.status_code == 201
    assert joined.json()["participant_token"]


def _subscription_event(
    *,
    event_id: str,
    org_id: str,
    status: str,
    created: int,
    period_end: int,
    event_type: str = "customer.subscription.updated",
) -> dict:
    return {
        "id": event_id,
        "type": event_type,
        "created": created,
        "data": {
            "object": {
                "id": "sub_creator",
                "customer": "cus_creator",
                "status": status,
                "current_period_end": period_end,
                "cancel_at_period_end": False,
                "metadata": {"cottage_org_id": org_id},
                "items": {"data": [{"price": {"id": "price_creator"}}]},
            }
        },
    }


async def test_stripe_projection_is_idempotent_and_rejects_stale_state(fresh_db):
    user = await _registered()
    now = int(time.time())
    active = _subscription_event(
        event_id="evt_active",
        org_id=user.org_id,
        status="active",
        created=now,
        period_end=now + 3600,
    )
    assert await billing.process_stripe_event(active) is True
    assert await billing.process_stripe_event(active) is False
    assert await billing.has_creator_entitlement(user.org_id)

    canceled = _subscription_event(
        event_id="evt_canceled",
        org_id=user.org_id,
        status="canceled",
        created=now + 2,
        period_end=now + 3600,
        event_type="customer.subscription.deleted",
    )
    assert await billing.process_stripe_event(canceled) is True
    assert not await billing.has_creator_entitlement(user.org_id)

    stale = _subscription_event(
        event_id="evt_stale",
        org_id=user.org_id,
        status="active",
        created=now + 1,
        period_end=now + 7200,
    )
    assert await billing.process_stripe_event(stale) is True
    assert not await billing.has_creator_entitlement(user.org_id)


async def test_stripe_webhook_signature_is_verified_before_processing(fresh_db):
    timestamp = int(time.time())
    payload = json.dumps(
        {
            "id": "evt_signed",
            "object": "event",
            "type": "invoice.paid",
            "created": timestamp,
            "data": {"object": {}},
        },
        separators=(",", ":"),
    ).encode()
    secret = "whsec_test_signature"
    digest = hmac.new(
        secret.encode(), f"{timestamp}.".encode() + payload, hashlib.sha256
    ).hexdigest()
    with _settings(stripe_webhook_secret=secret):
        event = billing.verify_stripe_event(payload, f"t={timestamp},v1={digest}")
        with pytest.raises(stripe.error.SignatureVerificationError):
            billing.verify_stripe_event(payload + b" ", f"t={timestamp},v1={digest}")
    assert event["id"] == "evt_signed"


async def test_signed_stripe_webhook_is_the_activation_path(fresh_db):
    user = await _registered()
    timestamp = int(time.time())
    event = _subscription_event(
        event_id="evt_webhook_active",
        org_id=user.org_id,
        status="active",
        created=timestamp,
        period_end=timestamp + 3600,
    )
    event["object"] = "event"
    payload = json.dumps(event, separators=(",", ":")).encode()
    secret = "whsec_test_route"
    digest = hmac.new(
        secret.encode(), f"{timestamp}.".encode() + payload, hashlib.sha256
    ).hexdigest()

    with _settings(stripe_webhook_secret=secret):
        async with await _client() as client:
            response = await client.post(
                "/billing/stripe/webhook",
                content=payload,
                headers={
                    "Content-Type": "application/json",
                    "Stripe-Signature": f"t={timestamp},v1={digest}",
                },
            )

    assert response.status_code == 200
    assert response.json() == {"ok": True, "processed": True}
    assert await billing.has_creator_entitlement(user.org_id)
