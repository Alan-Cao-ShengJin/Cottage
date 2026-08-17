"""Subscription projection and the one paid capability: creating rooms.

Stripe collects money; Cottage authorizes actions. A checkout redirect is never evidence
of payment. Only a signature-verified, idempotently processed webhook projects provider
state into an organization entitlement, and room creation reads that local projection.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import stripe

from ..config import settings
from ..db import database as db
from ..domain.identity import User
from ..util import UTC, is_past, to_iso, utcnow_iso
from .errors import PaymentRequired

CREATE_ROOMS = "rooms:create"
ACTIVE_SUBSCRIPTION_STATUSES = frozenset({"active", "trialing"})


class BillingUnavailable(Exception):
    """Production billing configuration is incomplete or the provider refused a call."""


@dataclass(frozen=True)
class BillingStatus:
    creator_enabled: bool
    subscription_status: str | None
    current_period_end: str | None
    cancel_at_period_end: bool


def _stripe_client_ready() -> bool:
    return bool(settings.stripe_secret_key and settings.stripe_creator_price_id)


async def grant_creator_entitlement(
    org_id: str, *, source: str, active_until: str | None = None
) -> None:
    now = utcnow_iso()
    await db.execute(
        """
        INSERT INTO organization_entitlements (
            org_id, entitlement, source, active_until, created_at, updated_at
        ) VALUES (?,?,?,?,?,?)
        ON CONFLICT(org_id, entitlement, source) DO UPDATE SET
            active_until = excluded.active_until,
            updated_at = excluded.updated_at
        """,
        (org_id, CREATE_ROOMS, source, active_until, now, now),
    )


async def has_creator_entitlement(org_id: str) -> bool:
    rows = await db.fetch_all(
        "SELECT active_until FROM organization_entitlements WHERE org_id = ? AND entitlement = ?",
        (org_id, CREATE_ROOMS),
    )
    return any(row["active_until"] is None or not is_past(row["active_until"]) for row in rows)


async def require_creator_entitlement(org_id: str) -> None:
    if not settings.enforce_creator_subscription:
        return
    if not await has_creator_entitlement(org_id):
        raise PaymentRequired(
            "An active Cottage Creator subscription is required to start a room.",
            upgrade_url=f"{settings.account_url}/billing",
            entitlement=CREATE_ROOMS,
        )


async def status_for_org(org_id: str) -> BillingStatus:
    row = await db.fetch_one(
        """
        SELECT status, current_period_end, cancel_at_period_end
          FROM billing_subscriptions
         WHERE org_id = ?
         ORDER BY provider_event_created_at DESC
         LIMIT 1
        """,
        (org_id,),
    )
    return BillingStatus(
        creator_enabled=await has_creator_entitlement(org_id),
        subscription_status=row["status"] if row else None,
        current_period_end=row["current_period_end"] if row else None,
        cancel_at_period_end=bool(row["cancel_at_period_end"]) if row else False,
    )


async def _ensure_stripe_customer(user: User) -> str:
    row = await db.fetch_one(
        "SELECT provider_customer_id FROM billing_customers WHERE org_id = ?", (user.org_id,)
    )
    if row is not None:
        return row["provider_customer_id"]
    if not _stripe_client_ready():
        raise BillingUnavailable("Stripe Checkout is not configured on this instance.")

    stripe.api_key = settings.stripe_secret_key
    customer = await asyncio.to_thread(
        stripe.Customer.create,
        email=user.email,
        name=user.display_name,
        metadata={"cottage_org_id": user.org_id, "cottage_user_id": user.id},
    )
    customer_id = str(customer["id"])
    now = utcnow_iso()
    await db.execute(
        """
        INSERT INTO billing_customers (
            org_id, provider, provider_customer_id, created_at, updated_at
        ) VALUES (?,'stripe',?,?,?)
        ON CONFLICT(org_id) DO UPDATE SET
            provider_customer_id = excluded.provider_customer_id,
            updated_at = excluded.updated_at
        """,
        (user.org_id, customer_id, now, now),
    )
    return customer_id


async def create_checkout_url(user: User) -> str:
    customer_id = await _ensure_stripe_customer(user)
    stripe.api_key = settings.stripe_secret_key
    try:
        session = await asyncio.to_thread(
            stripe.checkout.Session.create,
            mode="subscription",
            customer=customer_id,
            line_items=[{"price": settings.stripe_creator_price_id, "quantity": 1}],
            client_reference_id=user.org_id,
            metadata={"cottage_org_id": user.org_id},
            subscription_data={"metadata": {"cottage_org_id": user.org_id}},
            success_url=f"{settings.account_url}?checkout=success",
            cancel_url=f"{settings.account_url}?checkout=cancelled",
        )
    except Exception as exc:
        raise BillingUnavailable("Stripe could not start Checkout.") from exc
    url = session["url"]
    if not url:
        raise BillingUnavailable("Stripe returned no Checkout URL.")
    return str(url)


async def create_portal_url(user: User) -> str:
    row = await db.fetch_one(
        "SELECT provider_customer_id FROM billing_customers WHERE org_id = ?", (user.org_id,)
    )
    if row is None or not settings.stripe_secret_key:
        raise BillingUnavailable("No Stripe billing account exists for this organization.")
    stripe.api_key = settings.stripe_secret_key
    try:
        session = await asyncio.to_thread(
            stripe.billing_portal.Session.create,
            customer=row["provider_customer_id"],
            return_url=settings.account_url,
        )
    except Exception as exc:
        raise BillingUnavailable("Stripe could not open the billing portal.") from exc
    return str(session["url"])


def verify_stripe_event(payload: bytes, signature: str | None) -> dict[str, Any]:
    if not settings.stripe_webhook_secret:
        raise BillingUnavailable("STRIPE_WEBHOOK_SECRET is not configured.")
    if not signature:
        raise ValueError("Missing Stripe-Signature header.")
    event = stripe.Webhook.construct_event(payload, signature, settings.stripe_webhook_secret)
    return event.to_dict()


def _period_end(subscription: Any) -> str | None:
    raw = subscription.get("current_period_end") or subscription.get("trial_end")
    if not raw:
        items = subscription.get("items", {}).get("data", [])
        raw = items[0].get("current_period_end") if items else None
    if not raw:
        return None
    return to_iso(datetime.fromtimestamp(int(raw), tz=UTC))


def _price_id(subscription: Any) -> str:
    items = subscription.get("items", {}).get("data", [])
    if not items:
        return ""
    return str(items[0].get("price", {}).get("id", ""))


async def process_stripe_event(event: dict[str, Any]) -> bool:
    """Project one verified event. Returns False for an already-seen event id."""
    event_id = str(event.get("id", ""))
    event_type = str(event.get("type", ""))
    created = int(event.get("created", 0))
    if not event_id or not event_type:
        raise ValueError("Stripe event is missing id or type.")
    obj = event.get("data", {}).get("object", {})
    now = utcnow_iso()

    async with db.transaction() as tx:
        inserted = await tx.execute(
            "INSERT OR IGNORE INTO billing_webhook_events "
            "(provider_event_id, event_type, event_created_at, received_at) VALUES (?,?,?,?)",
            (event_id, event_type, created, now),
        )
        if inserted == 0:
            return False

        if event_type == "checkout.session.completed":
            org_id = str(
                obj.get("client_reference_id") or obj.get("metadata", {}).get("cottage_org_id", "")
            )
            customer_id = str(obj.get("customer", ""))
            if org_id and customer_id:
                await tx.execute(
                    """
                    INSERT INTO billing_customers (
                        org_id, provider, provider_customer_id, created_at, updated_at
                    ) VALUES (?,'stripe',?,?,?)
                    ON CONFLICT(org_id) DO UPDATE SET
                        provider_customer_id = excluded.provider_customer_id,
                        updated_at = excluded.updated_at
                    """,
                    (org_id, customer_id, now, now),
                )

        if event_type.startswith("customer.subscription."):
            subscription_id = str(obj.get("id", ""))
            org_id = str(obj.get("metadata", {}).get("cottage_org_id", ""))
            if not org_id and obj.get("customer"):
                customer = await tx.fetch_one(
                    "SELECT org_id FROM billing_customers WHERE provider_customer_id = ?",
                    (str(obj["customer"]),),
                )
                org_id = customer["org_id"] if customer else ""
            if subscription_id and org_id:
                status = (
                    "canceled" if event_type.endswith(".deleted") else str(obj.get("status", ""))
                )
                period_end = _period_end(obj)
                affected = await tx.execute(
                    """
                    INSERT INTO billing_subscriptions (
                        provider_subscription_id, org_id, provider, price_id, status,
                        current_period_end, cancel_at_period_end, provider_event_created_at,
                        created_at, updated_at
                    ) VALUES (?,?,'stripe',?,?,?,?,?,?,?)
                    ON CONFLICT(provider_subscription_id) DO UPDATE SET
                        price_id = excluded.price_id,
                        status = excluded.status,
                        current_period_end = excluded.current_period_end,
                        cancel_at_period_end = excluded.cancel_at_period_end,
                        provider_event_created_at = excluded.provider_event_created_at,
                        updated_at = excluded.updated_at
                    WHERE excluded.provider_event_created_at >=
                          billing_subscriptions.provider_event_created_at
                    """,
                    (
                        subscription_id,
                        org_id,
                        _price_id(obj),
                        status,
                        period_end,
                        1 if obj.get("cancel_at_period_end") else 0,
                        created,
                        now,
                        now,
                    ),
                )
                if affected:
                    active_until = (
                        period_end if status in ACTIVE_SUBSCRIPTION_STATUSES and period_end else now
                    )
                    await tx.execute(
                        """
                        INSERT INTO organization_entitlements (
                            org_id, entitlement, source, active_until, created_at, updated_at
                        ) VALUES (?,?,?,?,?,?)
                        ON CONFLICT(org_id, entitlement, source) DO UPDATE SET
                            active_until = excluded.active_until,
                            updated_at = excluded.updated_at
                        """,
                        (
                            org_id,
                            CREATE_ROOMS,
                            f"stripe:{subscription_id}",
                            active_until,
                            now,
                            now,
                        ),
                    )

        await tx.execute(
            "UPDATE billing_webhook_events SET processed_at = ? WHERE provider_event_id = ?",
            (utcnow_iso(), event_id),
        )
    return True
