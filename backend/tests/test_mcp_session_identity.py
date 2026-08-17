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
    mcp_server._session_connections.clear()
    yield
    mcp_server._session_tokens.clear()
    mcp_server._session_connections.clear()


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


@pytest.mark.asyncio
async def test_reaped_session_reconnects_its_exact_declared_runtime(make_room) -> None:
    """A returning tool call revives this session, not an arbitrary sibling connection.

    This is the live failure that prompted the guard: one seat first joined attended and
    then rejoined unattended. The old implementation heartbeated ``LIMIT 1`` (the attended
    row), so the unattended row was reaped and the board contradicted the join response.
    """
    from app.core import presence, store
    from app.db import database as db
    from app.domain.room import Liveness
    from app.util import iso_in

    room = await make_room()
    ctx = ctx_for("ffffffffffffffffffffffffffffffff")
    attended = await mcp_server.join_room(
        invitation_token=room.join_token,
        execution_mode="human_turn_only",
        display_name="Switchable agent",
        ctx=ctx,
    )
    unattended = await mcp_server.join_room(
        invitation_token=room.join_token,
        execution_mode="unattended_loop",
        display_name="Switchable agent",
        ctx=ctx,
    )
    assert attended["ok"] and unattended["ok"]
    assert attended["participant_id"] == unattended["participant_id"]

    key = mcp_server._session_key(ctx)
    assert key is not None
    binding = mcp_server._session_connections[key]
    assert binding.connection_id == unattended["connection_id"]

    participant = await store.load_participant(unattended["participant_id"])
    same_old_beat = iso_in(-30)
    await db.execute(
        "UPDATE connections SET last_heartbeat_at = ? WHERE id IN (?, ?)",
        (same_old_beat, attended["connection_id"], unattended["connection_id"]),
    )

    # Poll completion must beat the connection opened by this session. The attended
    # sibling remains untouched rather than winning an unordered LIMIT 1.
    await mcp_server._touch(participant, unattended["cursor"], ctx)
    attended_row = await db.fetch_one(
        "SELECT last_heartbeat_at FROM connections WHERE id = ?", (attended["connection_id"],)
    )
    unattended_row = await db.fetch_one(
        "SELECT last_heartbeat_at FROM connections WHERE id = ?", (unattended["connection_id"],)
    )
    assert attended_row is not None and attended_row["last_heartbeat_at"] == same_old_beat
    assert unattended_row is not None and unattended_row["last_heartbeat_at"] > same_old_beat

    # Once that exact runtime really lapses, the reaper may close it. Membership remains,
    # and the next tool call recreates the same unattended declaration automatically.
    await db.execute(
        "UPDATE connections SET last_heartbeat_at = ? WHERE id = ?",
        (iso_in(-90), unattended["connection_id"]),
    )
    reaped = await presence.reap_dead_connections()
    assert reaped
    closed = await store.load_connection(unattended["connection_id"])
    assert closed.closed_at is not None

    resumed = await mcp_server.get_room_state(ctx=ctx)
    assert resumed["ok"] is True
    assert resumed["you"] == participant.id
    assert binding.connection_id != unattended["connection_id"]
    replacement = await store.load_connection(binding.connection_id)
    assert replacement.closed_at is None
    assert replacement.host_class.value == "persistent_local"

    view = (await presence.presence_for_room(await room.refresh()))[participant.id]
    assert view.liveness == Liveness.LIVE_POLL
    assert view.runtime is not None and view.runtime.may_claim is True
