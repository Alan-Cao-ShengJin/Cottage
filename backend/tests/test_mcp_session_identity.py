"""Session affinity must never become a way to inherit someone else's identity.

The MCP adapter remembers "who you are" between tool calls so a client does not have to
resend its participant token — pure context economy. That convenience touches the one
guarantee the product cannot lose: **attribution**. `docs/SECURITY.md` §1 names the primary
threat as a participant learning or influencing more than it was authorized to, and a
session map that hands out the wrong participant token does exactly that, silently, with
correct-looking provenance on every event it produces.

The original implementation keyed on `id(ctx.session)`. Two defects, one of which needs no
coincidence at all:

* `id()` is a memory address and CPython reuses addresses after garbage collection, so a
  fresh session could land on a finished session's address and inherit its entry;
* any call without a session fell into a single shared `"default"` bucket, so two such
  callers were the same caller as far as the map was concerned.

These tests assert the property directly rather than the implementation, so a future change
that reintroduces address-keying fails here.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.adapters.mcp import server as mcp_server


def ctx_for(session_id: str | None):
    """A stand-in for the MCP `Context` shaped like the real one.

    Mirrors the path the adapter actually reads — `ctx.request_context.request.headers` —
    because that is the only source that is reliably per-message (`adapters/mcp/auth.py`).
    """
    headers = {} if session_id is None else {"mcp-session-id": session_id}
    request = SimpleNamespace(headers=headers)
    return SimpleNamespace(request_context=SimpleNamespace(request=request), session=object())


@pytest.fixture(autouse=True)
def clean_session_map():
    mcp_server._session_tokens.clear()
    yield
    mcp_server._session_tokens.clear()


def test_distinct_sessions_never_share_a_slot() -> None:
    """The core property: one session's token is unreachable from another session."""
    a, b = ctx_for("11111111111111111111111111111111"), ctx_for("22222222222222222222222222222222")

    mcp_server._remember_session(a, "ptok_alice")

    assert mcp_server._session_tokens[mcp_server._session_key(a)] == "ptok_alice"
    assert mcp_server._session_key(b) not in mcp_server._session_tokens


def test_key_is_not_derived_from_object_identity() -> None:
    """Two contexts sharing a `session` object still key apart, and one session keys the
    same across requests even though each request is a different object.

    This is the regression guard: `id(ctx.session)` would collapse the first case into one
    slot, and address reuse would collapse the second across *different* sessions.
    """
    shared = object()
    a = ctx_for("aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa")
    b = ctx_for("bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb")
    a.session = b.session = shared
    assert mcp_server._session_key(a) != mcp_server._session_key(b)

    # Same session, two separate requests: stable key, so affinity still works.
    first = ctx_for("cccccccccccccccccccccccccccccccc")
    second = ctx_for("cccccccccccccccccccccccccccccccc")
    assert first.session is not second.session
    assert mcp_server._session_key(first) == mcp_server._session_key(second)


def test_a_session_less_call_gets_no_slot_at_all() -> None:
    """With nothing to key on, the answer is "identify yourself", not a shared bucket.

    A placeholder key would make every session-less caller the same caller. Refusing to
    store, and refusing to resolve, costs one explicit argument and cannot leak.
    """
    anonymous = ctx_for(None)
    assert mcp_server._session_key(anonymous) is None

    mcp_server._remember_session(anonymous, "ptok_nobody")
    assert mcp_server._session_tokens == {}

    mcp_server._remember_session(ctx_for("dddddddddddddddddddddddddddddddd"), "ptok_someone")
    assert "ptok_someone" not in _resolvable_by(anonymous)


def _resolvable_by(ctx) -> list[str]:
    key = mcp_server._session_key(ctx)
    return [] if key is None else [mcp_server._session_tokens.get(key, "")]


@pytest.mark.asyncio
async def test_unresolvable_caller_is_refused_rather_than_guessed() -> None:
    """No token and no session must raise, never fall back to whatever was stored last."""
    from app.core.errors import Unauthenticated

    mcp_server._remember_session(ctx_for("eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"), "ptok_established")

    with pytest.raises(Unauthenticated):
        await mcp_server._participant(ctx_for(None), None)


def test_the_map_is_bounded_and_evicts_oldest_first() -> None:
    """Sessions that vanish without leaving would otherwise accumulate forever.

    Eviction is safe by construction — it can only *remove* an affinity, so the worst case
    is a caller having to pass its own token — but it must be bounded, and it must not
    evict the session that just spoke.
    """
    limit = mcp_server._SESSION_TOKEN_LIMIT
    for i in range(limit + 10):
        mcp_server._remember_session(ctx_for(f"{i:032d}"), f"ptok_{i}")

    assert len(mcp_server._session_tokens) == limit
    # The oldest went first; the most recent survived.
    assert mcp_server._session_key(ctx_for(f"{0:032d}")) not in mcp_server._session_tokens
    newest = mcp_server._session_key(ctx_for(f"{limit + 9:032d}"))
    assert mcp_server._session_tokens[newest] == f"ptok_{limit + 9}"
