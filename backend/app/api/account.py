"""Free account lifecycle and the browser billing surface."""

from __future__ import annotations

import contextlib
import html
from collections.abc import Awaitable, Callable
from typing import Annotated, Any
from urllib.parse import urlencode

from fastapi import APIRouter, Form, Header, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from ..config import settings
from ..core import accounts, billing, credentials, mailer
from ..core.errors import NotFound
from .browser_ui import page as browser_page

router = APIRouter()

SESSION_COOKIE = "cottage_session"
ACCOUNT_FLOW_COOKIE = "cottage_account_flow"
OAUTH_FLOW_COOKIE = "cottage_oauth_flow"


def _headers() -> dict[str, str]:
    return {
        "Cache-Control": "no-store",
        "Pragma": "no-cache",
        "Referrer-Policy": "no-referrer",
        "X-Content-Type-Options": "nosniff",
        "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'",
    }


def _page(title: str, body: str, *, status_code: int = 200) -> HTMLResponse:
    return HTMLResponse(
        browser_page(title, body),
        status_code=status_code,
        headers=_headers(),
    )


def _redirect(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303, headers={"Cache-Control": "no-store"})


def _set_cookie(response: Response, name: str, value: str, max_age: int) -> None:
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


def _remote(request: Request) -> str:
    return request.client.host if request.client else "unknown"


async def _form_response(
    purpose: str,
    render: Callable[[str], str],
    *,
    status_code: int = 200,
) -> HTMLResponse:
    token, flow = await accounts.create_account_browser_flow(purpose)
    response = _page("Cottage account", render(flow.csrf_token), status_code=status_code)
    _set_cookie(response, ACCOUNT_FLOW_COOKIE, token, accounts.ACCOUNT_FORM_TTL_SECONDS)
    return response


async def _consume_form(request: Request, purpose: str, csrf_token: str) -> bool:
    return await accounts.consume_account_browser_flow(
        request.cookies.get(ACCOUNT_FLOW_COOKIE),
        purpose=purpose,
        csrf_token=csrf_token,
    )


def _account_form(
    *,
    action: str,
    csrf: str,
    heading: str,
    fields: str,
    submit: str,
    error: str = "",
    footer: str = "",
) -> str:
    error_html = f'<p class="error">{html.escape(error)}</p>' if error else ""
    return f"""
<h1>{html.escape(heading)}</h1>
<p class="lede">One free account connects every compatible AI client.</p>{error_html}
<form method="post" action="{html.escape(action, quote=True)}">
  <input type="hidden" name="csrf_token" value="{html.escape(csrf)}">
  {fields}
  <button type="submit">{html.escape(submit)}</button>
</form>
<div class="muted">{footer}</div>
"""


def _signup_fields(email: str = "", display_name: str = "") -> str:
    return f"""
<label for="display_name">Display name</label>
<input id="display_name" name="display_name" maxlength="80" required
       autocomplete="name" value="{html.escape(display_name)}">
<label for="email">Email</label>
<input id="email" name="email" type="email" maxlength="320" required
       autocomplete="username" value="{html.escape(email)}">
<label for="password">Password</label>
<input id="password" name="password" type="password" minlength="15"
       maxlength="{accounts.PASSWORD_MAX_LENGTH}" required autocomplete="new-password">
<p class="muted">At least 15 characters. Cottage stores only an Argon2id verifier.</p>
"""


@router.get("/account/signup")
async def signup_page() -> Response:
    if not settings.public_signup_enabled:
        return _page("Signup unavailable", "<h1>Signup is not enabled.</h1>", status_code=404)
    return await _form_response(
        "signup",
        lambda csrf: _account_form(
            action="/account/signup",
            csrf=csrf,
            heading="Create your free Cottage account",
            fields=_signup_fields(),
            submit="Create account",
            footer='<a href="/account/login">Already have an account? Sign in.</a>',
        ),
    )


@router.post("/account/signup")
async def signup_submit(
    request: Request,
    csrf_token: Annotated[str, Form()],
    email: Annotated[str, Form()],
    display_name: Annotated[str, Form()],
    password: Annotated[str, Form()],
) -> Response:
    if not settings.public_signup_enabled:
        return _page("Signup unavailable", "<h1>Signup is not enabled.</h1>", status_code=404)
    if not await _consume_form(request, "signup", csrf_token):
        return _page(
            "Expired form", "<h1>This signup form expired. Start again.</h1>", status_code=403
        )
    try:
        result = await accounts.register_account(
            email=email, display_name=display_name, password=password
        )
    except accounts.AccountExists:
        return await _form_response(
            "signup",
            lambda csrf: _account_form(
                action="/account/signup",
                csrf=csrf,
                heading="Create your free Cottage account",
                fields=_signup_fields(email, display_name),
                submit="Create account",
                error="That email already has an account. Sign in or reset its password.",
                footer='<a href="/account/login">Sign in</a> · '
                '<a href="/account/password/forgot">Reset password</a>',
            ),
            status_code=409,
        )
    except ValueError as exc:
        message = str(exc)
        return await _form_response(
            "signup",
            lambda csrf: _account_form(
                action="/account/signup",
                csrf=csrf,
                heading="Create your free Cottage account",
                fields=_signup_fields(email, display_name),
                submit="Create account",
                error=message,
            ),
            status_code=422,
        )

    verify_url = f"{settings.public_base_url.rstrip('/')}/account/verify?" + urlencode(
        {"token": result.verification_token}
    )
    try:
        await mailer.send_account_link(
            recipient=result.user.email,
            subject="Verify your Cottage account",
            heading="Verify your Cottage account",
            action="Verify email",
            url=verify_url,
        )
    except mailer.EmailUnavailable as exc:
        return _page(
            "Account created",
            f"<h1>Account created, but email delivery failed.</h1><p>{html.escape(str(exc))}</p>"
            '<p><a href="/account/verify/resend">Try sending verification again.</a></p>',
            status_code=503,
        )
    local_link = (
        f'<p><a href="{html.escape(verify_url, quote=True)}">Local development: verify now</a></p>'
        if not settings.is_publicly_reachable
        else ""
    )
    return _page(
        "Check your email",
        "<h1>Check your email</h1><p>We sent a verification link. Verify before signing in.</p>"
        + local_link,
        status_code=201,
    )


@router.get("/account/verify")
async def verify_email(request: Request, token: str) -> Response:
    try:
        user = await accounts.consume_email_verification(token)
    except accounts.InvalidAccountAction:
        return _page(
            "Invalid verification link",
            "<h1>This verification link is invalid or expired.</h1>"
            '<p><a href="/account/verify/resend">Send another link.</a></p>',
            status_code=400,
        )
    session_token, _ = await accounts.create_session(user.id)
    # If signup began during IDE authorization, continue that browser flow after
    # verification instead of making the person restart the MCP connection.
    target = "/oauth/consent" if request.cookies.get(OAUTH_FLOW_COOKIE) else "/account"
    response = _redirect(target)
    _set_cookie(response, SESSION_COOKIE, session_token, accounts.SESSION_TTL_SECONDS)
    return response


@router.get("/account/verify/resend")
async def resend_page() -> Response:
    return await _form_response(
        "resend_verification",
        lambda csrf: _account_form(
            action="/account/verify/resend",
            csrf=csrf,
            heading="Send another verification link",
            fields='<label for="email">Email</label><input id="email" name="email" '
            'type="email" maxlength="320" required autocomplete="username">',
            submit="Send link",
        ),
    )


@router.post("/account/verify/resend")
async def resend_submit(
    request: Request,
    csrf_token: Annotated[str, Form()],
    email: Annotated[str, Form()],
) -> Response:
    if not await _consume_form(request, "resend_verification", csrf_token):
        return _page("Expired form", "<h1>This form expired. Start again.</h1>", status_code=403)
    issued = await accounts.create_email_verification(email)
    if issued is not None:
        user, token = issued
        url = f"{settings.public_base_url.rstrip('/')}/account/verify?" + urlencode(
            {"token": token}
        )
        with contextlib.suppress(mailer.EmailUnavailable):
            await mailer.send_account_link(
                recipient=user.email,
                subject="Verify your Cottage account",
                heading="Verify your Cottage account",
                action="Verify email",
                url=url,
            )
    return _page(
        "Check your email",
        "<h1>Check your email</h1><p>If that unverified account exists, a link was sent.</p>",
    )


@router.get("/account/login")
async def login_page() -> Response:
    return await _form_response(
        "login",
        lambda csrf: _account_form(
            action="/account/login",
            csrf=csrf,
            heading="Sign in to Cottage",
            fields='<label for="email">Email</label><input id="email" name="email" '
            'type="email" maxlength="320" required autocomplete="username">'
            '<label for="password">Password</label><input id="password" name="password" '
            f'type="password" maxlength="{accounts.PASSWORD_MAX_LENGTH}" required '
            'autocomplete="current-password">',
            submit="Sign in",
            footer='<a href="/account/signup">Create account</a> · '
            '<a href="/account/password/forgot">Forgot password?</a>',
        ),
    )


@router.post("/account/login")
async def login_submit(
    request: Request,
    csrf_token: Annotated[str, Form()],
    email: Annotated[str, Form()],
    password: Annotated[str, Form()],
) -> Response:
    if not await _consume_form(request, "login", csrf_token):
        return _page(
            "Expired form", "<h1>This login form expired. Start again.</h1>", status_code=403
        )
    try:
        user = await accounts.authenticate_password(email, password, _remote(request))
    except accounts.LoginDenied as exc:
        failure_response = await _form_response(
            "login",
            lambda csrf: _account_form(
                action="/account/login",
                csrf=csrf,
                heading="Sign in to Cottage",
                fields=(
                    '<label for="email">Email</label><input id="email" name="email" '
                    f'type="email" required autocomplete="username" value="{html.escape(email)}">'
                    '<label for="password">Password</label><input id="password" name="password" '
                    'type="password" required autocomplete="current-password">'
                ),
                submit="Sign in",
                error="Incorrect email or password.",
            ),
            status_code=429 if exc.retry_after else 401,
        )
        if exc.retry_after:
            failure_response.headers["Retry-After"] = str(exc.retry_after)
        return failure_response
    session_token, _ = await accounts.create_session(user.id)
    response = _redirect("/account")
    _set_cookie(response, SESSION_COOKIE, session_token, accounts.SESSION_TTL_SECONDS)
    return response


@router.get("/account/password/forgot")
async def forgot_page() -> Response:
    return await _form_response(
        "forgot_password",
        lambda csrf: _account_form(
            action="/account/password/forgot",
            csrf=csrf,
            heading="Reset your Cottage password",
            fields='<label for="email">Email</label><input id="email" name="email" '
            'type="email" maxlength="320" required autocomplete="username">',
            submit="Send reset link",
        ),
    )


@router.post("/account/password/forgot")
async def forgot_submit(
    request: Request,
    csrf_token: Annotated[str, Form()],
    email: Annotated[str, Form()],
) -> Response:
    if not await _consume_form(request, "forgot_password", csrf_token):
        return _page("Expired form", "<h1>This form expired. Start again.</h1>", status_code=403)
    issued = await accounts.create_password_reset(email)
    if issued is not None:
        user, token = issued
        url = f"{settings.public_base_url.rstrip('/')}/account/password/reset?" + urlencode(
            {"token": token}
        )
        with contextlib.suppress(mailer.EmailUnavailable):
            await mailer.send_account_link(
                recipient=user.email,
                subject="Reset your Cottage password",
                heading="Reset your Cottage password",
                action="Choose a new password",
                url=url,
            )
    return _page(
        "Check your email",
        "<h1>Check your email</h1><p>If that verified account exists, a reset link was sent.</p>",
    )


@router.get("/account/password/reset")
async def reset_page(token: str) -> Response:
    return await _form_response(
        "reset_password",
        lambda csrf: _account_form(
            action="/account/password/reset",
            csrf=csrf,
            heading="Choose a new password",
            fields=(
                f'<input type="hidden" name="token" value="{html.escape(token)}">'
                '<label for="password">New password</label>'
                f'<input id="password" name="password" type="password" minlength="15" '
                f'maxlength="{accounts.PASSWORD_MAX_LENGTH}" required autocomplete="new-password">'
            ),
            submit="Change password",
        ),
    )


@router.post("/account/password/reset")
async def reset_submit(
    request: Request,
    csrf_token: Annotated[str, Form()],
    token: Annotated[str, Form()],
    password: Annotated[str, Form()],
) -> Response:
    if not await _consume_form(request, "reset_password", csrf_token):
        return _page("Expired form", "<h1>This form expired. Start again.</h1>", status_code=403)
    try:
        await accounts.reset_password(token, password)
    except (accounts.InvalidAccountAction, ValueError):
        return _page(
            "Invalid reset link",
            "<h1>This reset link is invalid or expired.</h1>",
            status_code=400,
        )
    return _page(
        "Password changed",
        '<h1>Password changed</h1><p><a class="button" href="/account/login">Sign in</a></p>',
    )


async def _browser_session(request: Request) -> accounts.BrowserSession | None:
    return await accounts.load_session(request.cookies.get(SESSION_COOKIE))


def _seat_row(seat: credentials.OwnedSeat, csrf_token: str) -> str:
    """One seat, with the button that re-credentials it.

    The room name is the label because that is what somebody recognises; the participant id is
    shown too, since two seats can share a room name and the id is what the form actually posts.
    """
    lost = "" if seat.has_credential else ' <span class="muted">(no credential)</span>'
    return f"""<div class="panel">
<strong>{html.escape(seat.room_name)}</strong>{lost}<br>
<span class="muted">{html.escape(seat.display_name)} &middot; {html.escape(seat.role)}
&middot; {html.escape(seat.participant_id)}</span>
<form method="post" action="/account/seats/reissue">
  <input type="hidden" name="csrf_token" value="{html.escape(csrf_token)}">
  <input type="hidden" name="participant_id" value="{html.escape(seat.participant_id)}">
  <button class="secondary" type="submit">Issue a new token</button>
</form>
</div>"""


async def _seats_panel(session: accounts.BrowserSession) -> str:
    """The rooms this account holds a seat in, and a way back into each.

    Here rather than only in an API, because the situation this serves is *having no room
    credential at all* — the browser session is the only authority left, so the recovery path
    cannot be one that needs a participant token to reach.
    """
    seats = await credentials.seats_owned_by(session.user.id)
    if not seats:
        return ""
    rows = "".join(_seat_row(seat, session.csrf_token) for seat in seats)
    return f"""<h2>Your seats</h2>
<p class="muted">A participant token is shown once and stored hashed, so it cannot be looked up
again. Issuing a new one keeps the same seat &mdash; its leases, work, and history are
unchanged &mdash; and <strong>immediately stops the old token working</strong>, which is also how
you revoke one that leaked.</p>
{rows}"""


@router.post("/account/seats/reissue")
async def reissue_seat_token(
    request: Request,
    csrf_token: Annotated[str, Form()],
    participant_id: Annotated[str, Form()],
) -> Response:
    """Issue a fresh participant token for a seat this account owns, and show it once.

    Rendered into the POST response rather than carried through a redirect: a credential in a
    query string lands in browser history, and `Referer` on every subsequent request.

    **Not rate limited, deliberately.** Reaching this needs an authenticated session, and a
    session holder can already list their own seats on `/account` -- so there is nothing to
    enumerate that they cannot simply read. What is left is an owner rotating their own token
    repeatedly, which costs them their own runtimes and nobody else anything. Reusing the login
    throttle would be actively worse: its buckets are keyed for password failures, so mistyping
    a seat id could lock somebody out of signing in.
    """
    session = await _require_session_csrf(request, csrf_token)
    if session is None:
        return _redirect("/account/login")
    try:
        token = await credentials.reissue_seat_token(
            user_id=session.user.id, participant_id=participant_id
        )
    except NotFound:
        # Only `NotFound`. The core already answers identically for "no such seat", "not yours",
        # and "no longer in the room", so that indistinguishability is preserved by catching it
        # -- while a database error still surfaces as an error instead of being reported as a
        # missing seat, which would be this page lying about what happened.
        return _page(
            "Seat not found",
            "<h1>Seat not found</h1><p>No such seat, or it is not one you own.</p>"
            '<div class="row"><a class="button" href="/account">Back to your account</a></div>',
            status_code=404,
        )
    return _page(
        "New participant token",
        f"""<h1>New participant token</h1>
<p><strong>Copy this now.</strong> It is stored hashed and cannot be shown again.</p>
<div class="panel"><code>{html.escape(token)}</code></div>
<p class="muted">The previous token for this seat has stopped working. Anything still using
it &mdash; a companion, a relay, another editor session &mdash; needs this one.</p>
<div class="row"><a class="button" href="/account">Back to your account</a></div>""",
    )


@router.get("/account")
async def account_home(request: Request, checkout: str | None = None) -> Response:
    session = await _browser_session(request)
    if session is None:
        return _redirect("/account/login")
    status = await billing.status_for_org(session.user.org_id)
    notice = (
        '<p class="panel">Checkout completed. Subscription activation follows the signed webhook.</p>'
        if checkout == "success"
        else ""
    )
    state = "Creator enabled" if status.creator_enabled else "Free account"
    billing_action = (
        '<form method="post" action="/account/billing/portal">'
        f'<input type="hidden" name="csrf_token" value="{html.escape(session.csrf_token)}">'
        '<button type="submit">Manage billing</button></form>'
        if status.subscription_status
        else '<form method="post" action="/account/billing/checkout">'
        f'<input type="hidden" name="csrf_token" value="{html.escape(session.csrf_token)}">'
        '<button type="submit">Upgrade to Cottage Creator</button></form>'
    )
    authorization_action = (
        '<div class="row"><a class="button" href="/oauth/consent">Continue IDE authorization</a></div>'
        if request.cookies.get(OAUTH_FLOW_COOKIE)
        else ""
    )
    seats = await _seats_panel(session)
    return _page(
        "Your Cottage account",
        f"""
<h1>Your Cottage account</h1>{notice}
<div class="panel"><strong>{html.escape(session.user.display_name)}</strong><br>
{html.escape(session.user.email)}<br><span class="muted">{state}</span></div>
<p>Free accounts can connect MCP clients and join invited rooms. Creator starts rooms.</p>
{authorization_action}
<div class="row"><a class="button" href="/">Open Cottage</a></div>
{billing_action}
<form method="post" action="/account/logout">
  <input type="hidden" name="csrf_token" value="{html.escape(session.csrf_token)}">
  <button class="secondary" type="submit">Sign out</button>
</form>
{seats}
""",
    )


async def _require_session_csrf(
    request: Request, csrf_token: str
) -> accounts.BrowserSession | None:
    session = await _browser_session(request)
    return session if session and accounts.csrf_matches(session, csrf_token) else None


@router.post("/account/logout")
async def account_logout(request: Request, csrf_token: Annotated[str, Form()]) -> Response:
    session = await _require_session_csrf(request, csrf_token)
    if session is None:
        return _page("Expired form", "<h1>This sign-out form expired.</h1>", status_code=403)
    await accounts.revoke_session(request.cookies.get(SESSION_COOKIE))
    response = _redirect("/account/login")
    _clear_cookie(response, SESSION_COOKIE)
    return response


async def _billing_redirect(
    request: Request,
    csrf_token: str,
    creator: Callable[[Any], Awaitable[str]],
) -> Response:
    session = await _require_session_csrf(request, csrf_token)
    if session is None:
        return _page("Expired form", "<h1>This billing form expired.</h1>", status_code=403)
    try:
        url = await creator(session.user)
    except billing.BillingUnavailable as exc:
        return _page("Billing unavailable", f"<h1>{html.escape(str(exc))}</h1>", status_code=503)
    return _redirect(url)


@router.post("/account/billing/checkout")
async def billing_checkout(request: Request, csrf_token: Annotated[str, Form()]) -> Response:
    return await _billing_redirect(request, csrf_token, billing.create_checkout_url)


@router.post("/account/billing/portal")
async def billing_portal(request: Request, csrf_token: Annotated[str, Form()]) -> Response:
    return await _billing_redirect(request, csrf_token, billing.create_portal_url)


@router.post("/billing/stripe/webhook")
async def stripe_webhook(
    request: Request,
    stripe_signature: Annotated[str | None, Header(alias="Stripe-Signature")] = None,
) -> Response:
    payload = await request.body()
    try:
        event = billing.verify_stripe_event(payload, stripe_signature)
    except Exception:
        return JSONResponse({"ok": False, "error": "invalid_signature"}, status_code=400)
    processed = await billing.process_stripe_event(event)
    return JSONResponse({"ok": True, "processed": processed})
