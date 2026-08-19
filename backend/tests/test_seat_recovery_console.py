"""The browser half of seat recovery: CSRF, once-only display, and no leaks (D-094).

This surface exists in the account console rather than in the API because of what the situation
*is*: the owner has no room credential. The browser session is the only authority left, so the
recovery path must not be one that needs a participant token to reach.

Which makes this a page that displays a live credential — so most of what is tested here is the
handling around it: that a GET never shows one, that the POST requires CSRF, that a failure is
indistinguishable across the three reasons it can fail, and that the response cannot be cached or
leaked through a `Referer`.
"""

from __future__ import annotations

import re

import httpx
import pytest

from app.core import accounts, credentials, rooms, store
from app.db import database as db
from app.domain.commands import CreateInvitationCommand, CreateRoomCommand, JoinRoomCommand

pytestmark = pytest.mark.asyncio

PASSWORD = "correct horse battery staple"


async def _client():
    from app.main import app

    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://arp.test")


def _csrf(page: str) -> str:
    match = re.search(r'name="csrf_token" value="([^"]+)"', page)
    assert match, page[:400]
    return match.group(1)


async def _owner_with_a_room(email: str = "owner@example.test"):
    """A verified account holding a seat in a room it created."""
    registered = await accounts.register_account(
        email=email, display_name="Seat Owner", password=PASSWORD
    )
    await accounts.consume_email_verification(registered.verification_token)
    user = registered.user
    from app.core import billing

    await billing.grant_creator_entitlement(user.org_id, source="test")
    user_row = await db.fetch_one("SELECT * FROM users WHERE id = ?", (user.id,))
    created = await rooms.create_room(
        user=store.to_user(user_row),
        command=CreateRoomCommand(name="Recoverable room"),
        creator_display_name="Seat Owner",
    )
    session_token, _ = await accounts.create_session(user.id)
    return user, created, session_token


# ---------------------------------------------------------------------------
# The listing
# ---------------------------------------------------------------------------


async def test_the_account_page_lists_a_seat_and_offers_a_new_token(fresh_db):
    _user, created, session_token = await _owner_with_a_room()
    async with await _client() as client:
        client.cookies.set("cottage_session", session_token)
        page = await client.get("/account")

    assert page.status_code == 200
    assert "Your seats" in page.text
    assert "Recoverable room" in page.text
    assert created.participant.id in page.text


async def test_the_listing_never_contains_a_token(fresh_db):
    """The page a person leaves open. A credential rendered here would sit in scrollback, in a
    screenshot, and in anything that reads the DOM."""
    _user, created, session_token = await _owner_with_a_room()
    async with await _client() as client:
        client.cookies.set("cottage_session", session_token)
        page = await client.get("/account")

    assert created.participant_token not in page.text


async def test_an_account_with_no_seats_gets_no_panel(fresh_db):
    """Not an empty heading. Somebody who has never joined a room should not be shown machinery
    for recovering something they do not have."""
    registered = await accounts.register_account(
        email="nobody@example.test", display_name="Nobody", password=PASSWORD
    )
    await accounts.consume_email_verification(registered.verification_token)
    session_token, _ = await accounts.create_session(registered.user.id)
    async with await _client() as client:
        client.cookies.set("cottage_session", session_token)
        page = await client.get("/account")

    assert page.status_code == 200
    assert "Your seats" not in page.text


async def test_signing_out_is_enough_to_lose_the_listing(fresh_db):
    _user, _created, _session = await _owner_with_a_room()
    async with await _client() as client:
        page = await client.get("/account", follow_redirects=False)
    assert page.status_code == 303
    assert page.headers["location"] == "/account/login"


# ---------------------------------------------------------------------------
# Issuing
# ---------------------------------------------------------------------------


async def test_a_valid_post_returns_a_working_token_once(fresh_db):
    _user, created, session_token = await _owner_with_a_room()
    async with await _client() as client:
        client.cookies.set("cottage_session", session_token)
        page = await client.get("/account")
        response = await client.post(
            "/account/seats/reissue",
            data={
                "csrf_token": _csrf(page.text),
                "participant_id": created.participant.id,
            },
        )

    assert response.status_code == 200
    match = re.search(r"<code>([A-Za-z0-9_\-]{20,})</code>", response.text)
    assert match, response.text[:600]
    token = match.group(1)
    assert (await store.load_participant_by_token(token)).id == created.participant.id


async def test_the_page_showing_a_token_is_not_cacheable_and_leaks_no_referrer(fresh_db):
    """Both headers matter for this one response: a cached credential page is readable by the
    next person at the machine, and a `Referer` would carry the URL onward — the reason the token
    is in the body of a POST response and never in a redirect."""
    _user, created, session_token = await _owner_with_a_room()
    async with await _client() as client:
        client.cookies.set("cottage_session", session_token)
        page = await client.get("/account")
        response = await client.post(
            "/account/seats/reissue",
            data={"csrf_token": _csrf(page.text), "participant_id": created.participant.id},
        )

    assert response.headers["cache-control"] == "no-store"
    assert response.headers["referrer-policy"] == "no-referrer"


async def test_the_token_is_never_carried_in_a_redirect(fresh_db):
    """A credential in a query string lands in browser history and in server access logs. The
    response is the page itself, so there is no URL to leak."""
    _user, created, session_token = await _owner_with_a_room()
    async with await _client() as client:
        client.cookies.set("cottage_session", session_token)
        page = await client.get("/account")
        response = await client.post(
            "/account/seats/reissue",
            data={"csrf_token": _csrf(page.text), "participant_id": created.participant.id},
            follow_redirects=False,
        )
    assert response.status_code == 200, "a 3xx here would put the token somewhere it can persist"
    assert "location" not in {k.lower() for k in response.headers}


async def test_the_old_token_stops_working_after_the_post(fresh_db):
    _user, created, session_token = await _owner_with_a_room()
    async with await _client() as client:
        client.cookies.set("cottage_session", session_token)
        page = await client.get("/account")
        await client.post(
            "/account/seats/reissue",
            data={"csrf_token": _csrf(page.text), "participant_id": created.participant.id},
        )

    from app.core.errors import Unauthenticated

    with pytest.raises(Unauthenticated):
        await store.load_participant_by_token(created.participant_token)


# ---------------------------------------------------------------------------
# Refusing
# ---------------------------------------------------------------------------


async def test_a_missing_csrf_token_issues_nothing(fresh_db):
    """A credential-minting POST is the last place to accept a cross-site form."""
    _user, created, session_token = await _owner_with_a_room()
    async with await _client() as client:
        client.cookies.set("cottage_session", session_token)
        response = await client.post(
            "/account/seats/reissue",
            data={"csrf_token": "wrong", "participant_id": created.participant.id},
            follow_redirects=False,
        )

    assert response.status_code == 303
    # And the original token still works, so the refusal happened before any rotation.
    assert (
        await store.load_participant_by_token(created.participant_token)
    ).id == created.participant.id


async def test_no_session_at_all_issues_nothing(fresh_db):
    _user, created, _session = await _owner_with_a_room()
    async with await _client() as client:
        response = await client.post(
            "/account/seats/reissue",
            data={"csrf_token": "anything", "participant_id": created.participant.id},
            follow_redirects=False,
        )
    assert response.status_code == 303
    assert (
        await store.load_participant_by_token(created.participant_token)
    ).id == created.participant.id


async def test_somebody_elses_seat_and_an_invented_one_answer_identically(fresh_db):
    """The console must not undo the core's indistinguishability by rendering three different
    pages for the three reasons a seat cannot be recovered."""
    _owner, created, _owner_session = await _owner_with_a_room()
    other = await accounts.register_account(
        email="other@example.test", display_name="Other", password=PASSWORD
    )
    await accounts.consume_email_verification(other.verification_token)
    other_session, _ = await accounts.create_session(other.user.id)

    async with await _client() as client:
        client.cookies.set("cottage_session", other_session)
        page = await client.get("/account")
        csrf = _csrf(page.text)
        real = await client.post(
            "/account/seats/reissue",
            data={"csrf_token": csrf, "participant_id": created.participant.id},
        )
        invented = await client.post(
            "/account/seats/reissue",
            data={"csrf_token": csrf, "participant_id": "par_01NOSUCHSEAT"},
        )

    assert real.status_code == invented.status_code == 404
    assert real.text == invented.text
    # And the real seat's credential was not touched on the way to refusing.
    assert (
        await store.load_participant_by_token(created.participant_token)
    ).id == created.participant.id


async def test_the_refusal_page_carries_no_credential(fresh_db):
    _owner, created, _owner_session = await _owner_with_a_room()
    other = await accounts.register_account(
        email="other2@example.test", display_name="Other", password=PASSWORD
    )
    await accounts.consume_email_verification(other.verification_token)
    other_session, _ = await accounts.create_session(other.user.id)

    async with await _client() as client:
        client.cookies.set("cottage_session", other_session)
        page = await client.get("/account")
        refused = await client.post(
            "/account/seats/reissue",
            data={"csrf_token": _csrf(page.text), "participant_id": created.participant.id},
        )
    assert created.participant_token not in refused.text


async def test_recovery_does_not_require_a_paid_plan(fresh_db):
    """Free accounts can join invited rooms, so they must be able to recover them. Gating this
    behind Creator would strand people in rooms they were invited to."""
    registered = await accounts.register_account(
        email="guest@example.test", display_name="Guest", password=PASSWORD
    )
    await accounts.consume_email_verification(registered.verification_token)
    _owner, created, _session = await _owner_with_a_room(email="host@example.test")

    invitation = await rooms.create_invitation(
        participant=created.participant, command=CreateInvitationCommand()
    )
    identity = await rooms.ensure_identity(
        org_id=registered.user.org_id,
        owner_user_id=registered.user.id,
        display_name="Guest agent",
    )
    joined = await rooms.join_room(
        identity=identity, command=JoinRoomCommand(invitation_token=invitation.token)
    )
    session_token, _ = await accounts.create_session(registered.user.id)

    async with await _client() as client:
        client.cookies.set("cottage_session", session_token)
        page = await client.get("/account")
        assert "Free account" in page.text
        response = await client.post(
            "/account/seats/reissue",
            data={"csrf_token": _csrf(page.text), "participant_id": joined.participant.id},
        )

    assert response.status_code == 200
    token = re.search(r"<code>([A-Za-z0-9_\-]{20,})</code>", response.text).group(1)
    assert (await store.load_participant_by_token(token)).id == joined.participant.id


async def test_the_core_and_the_console_agree_on_who_owns_what(fresh_db):
    """The console renders what `seats_owned_by` returns, so a drift between them would show
    somebody a button that always fails, or hide one that would work."""
    _user, created, session_token = await _owner_with_a_room()
    seats = await credentials.seats_owned_by(_user.id)
    async with await _client() as client:
        client.cookies.set("cottage_session", session_token)
        page = await client.get("/account")
    for seat in seats:
        assert seat.participant_id in page.text
    assert len(seats) == 1
    assert seats[0].participant_id == created.participant.id
