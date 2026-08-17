"""Transactional account email with a deliberately small provider boundary."""

from __future__ import annotations

import html
import logging

import httpx

from ..config import settings

log = logging.getLogger(__name__)


class EmailUnavailable(Exception):
    pass


async def send_account_link(
    *, recipient: str, subject: str, heading: str, action: str, url: str
) -> None:
    """Deliver one verification/recovery link, or expose it only on localhost."""
    if not settings.resend_api_key:
        if settings.is_publicly_reachable:
            raise EmailUnavailable("Outbound account email is not configured.")
        log.warning("LOCAL ACCOUNT EMAIL | %s | %s", recipient, url)
        return

    body = {
        "from": settings.email_from,
        "to": [recipient],
        "subject": subject,
        "html": (
            f"<h1>{html.escape(heading)}</h1>"
            f'<p><a href="{html.escape(url, quote=True)}">{html.escape(action)}</a></p>'
            "<p>If you did not request this, you can ignore this message.</p>"
        ),
    }
    async with httpx.AsyncClient(timeout=15) as client:
        response = await client.post(
            "https://api.resend.com/emails",
            headers={
                "Authorization": f"Bearer {settings.resend_api_key}",
                "Content-Type": "application/json",
            },
            json=body,
        )
    if response.status_code >= 300:
        log.error("account email provider refused delivery: status=%s", response.status_code)
        raise EmailUnavailable("The verification email could not be sent.")
