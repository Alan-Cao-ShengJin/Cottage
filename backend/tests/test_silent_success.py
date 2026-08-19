"""Three operations that reported success and did something else.

Grouped because they are one failure shape, and the codebase has a name for it:
"the control appears to work and does the opposite" (D-024, D-026, D-027, D-030). A
missing feature is visible. A feature that accepts your input, records it, and then
quietly discards it teaches you to trust something that is not true.

* A scope grant to an untrusted identity was accepted, logged, and stripped.
* A long poll asked for 240s got 25s with nothing in the response saying so.
* A lease-gated MCP call named no runtime, so the room guessed which one was calling.
"""

from __future__ import annotations

import dataclasses
from types import SimpleNamespace

import pytest

from app.adapters.mcp import server as mcp_server
from app.config import settings
from app.core import eventlog, rooms, tasks
from app.core.errors import Forbidden
from app.domain.commands import ClaimTaskCommand, CreateTaskCommand, SetParticipantRoleCommand
from app.domain.identity import TrustTier
from app.domain.room import (
    UNTRUSTED_DENIED_SCOPES,
    ParticipantRole,
    RoomVisibility,
    Scope,
)

pytestmark = pytest.mark.asyncio


def ctx_for(session_id: str):
    headers = {"mcp-session-id": session_id}
    request = SimpleNamespace(headers=headers)
    return SimpleNamespace(request_context=SimpleNamespace(request=request), session=object())


@pytest.fixture(autouse=True)
def clean_session_map():
    mcp_server._session_tokens.clear()
    mcp_server._session_connections.clear()
    yield
    mcp_server._session_tokens.clear()
    mcp_server._session_connections.clear()


def _low_ceiling(monkeypatch, seconds: int = 2) -> int:
    """Shrink the server-side long-poll ceiling.

    The real one is 25s and the test would otherwise block for all of it, since the
    behaviour under test is precisely that an empty poll runs to the cap. `Settings` is
    frozen, so the module reference is replaced rather than the field mutated.
    """
    monkeypatch.setattr(
        mcp_server, "settings", dataclasses.replace(settings, max_long_poll_seconds=seconds)
    )
    return seconds


@pytest.fixture
def guest_factory(join):
    """A foreign-org participant that landed untrusted, which is what a link invitation
    into a cross-org room actually produces."""

    async def _guest(room, name: str = "Guest"):
        other_org, _ = await rooms.ensure_org_and_user(
            org_name=f"Foreign {name}",
            org_slug=f"foreign-{name.lower()}",
            email=f"{name.lower()}@foreign.test",
            display_name=name,
        )
        return await join(room, display_name=name, org_id=other_org, trust=TrustTier.UNTRUSTED)

    return _guest


# ---------------------------------------------------------------------------
# A grant that cannot be honoured is refused, not recorded
# ---------------------------------------------------------------------------


async def test_granting_a_denied_scope_to_an_untrusted_identity_is_refused(
    make_room, join, guest_factory
):
    """Previously: accepted, `participant.scopes_changed` emitted naming task.claim, and
    the scope stripped on the way out. The admin was told yes and got no."""
    room = await make_room(visibility=RoomVisibility.CROSS_ORG)
    guest = await guest_factory(room)
    assert guest.participant.trust is TrustTier.UNTRUSTED

    with pytest.raises(Forbidden, match="task.claim") as caught:
        await rooms.set_participant_role(
            participant=room.owner,
            command=SetParticipantRoleCommand(
                target_participant_id=guest.participant.id,
                role=ParticipantRole.COLLABORATOR,
                scopes=[Scope.TASK_CLAIM],
                reason="let the guest take work",
            ),
        )
    assert "task.claim" in caught.value.details.get("refused_scopes", [])


async def test_the_refusal_names_every_denied_scope_asked_for(make_room, guest_factory):
    room = await make_room(visibility=RoomVisibility.CROSS_ORG)
    guest = await guest_factory(room)

    with pytest.raises(Forbidden) as caught:
        await rooms.set_participant_role(
            participant=room.owner,
            command=SetParticipantRoleCommand(
                target_participant_id=guest.participant.id,
                role=ParticipantRole.COLLABORATOR,
                scopes=[Scope.TASK_CLAIM, Scope.STATE_WRITE],
                reason="",
            ),
        )
    assert set(caught.value.details["refused_scopes"]) == {"task.claim", "state.write"}


async def test_a_grant_of_scopes_it_can_hold_still_works(make_room, guest_factory):
    """The refusal must not become a blanket ban on managing an untrusted participant."""
    room = await make_room(visibility=RoomVisibility.CROSS_ORG)
    guest = await guest_factory(room)

    updated = await rooms.set_participant_role(
        participant=room.owner,
        command=SetParticipantRoleCommand(
            target_participant_id=guest.participant.id,
            role=ParticipantRole.COLLABORATOR,
            scopes=[Scope.MESSAGE_POST],
            reason="chat only",
        ),
    )
    assert Scope.MESSAGE_POST in updated.scopes
    assert not set(updated.scopes) & UNTRUSTED_DENIED_SCOPES


async def test_a_role_default_still_narrows_but_says_so_on_the_event(make_room, guest_factory):
    """Asking for a role rather than a scope list is not a false claim about any single
    scope, so it succeeds — but the log records what was withheld, or the room would be
    quietly wrong about what the participant can do."""
    room = await make_room(visibility=RoomVisibility.CROSS_ORG)
    guest = await guest_factory(room)
    before = await eventlog.current_seq(room.room.id)

    updated = await rooms.set_participant_role(
        participant=room.owner,
        command=SetParticipantRoleCommand(
            target_participant_id=guest.participant.id,
            role=ParticipantRole.COLLABORATOR,
            scopes=None,
            reason="promote to collaborator",
        ),
    )
    assert not set(updated.scopes) & UNTRUSTED_DENIED_SCOPES

    after = await eventlog.read_since(room.room.id, before)
    changed = [e for e in after if e.type.value == "participant.scopes_changed"]
    assert len(changed) == 1
    withheld = changed[0].payload.get("withheld_scopes")
    assert withheld, "the narrowing must be visible in the log"
    assert "task.claim" in withheld


# ---------------------------------------------------------------------------
# A capped long poll says what it actually gave you
# ---------------------------------------------------------------------------


async def test_a_capped_long_poll_reports_the_timeout_it_honoured(make_room, monkeypatch):
    """Found live: a participant asked for 240s, got 25s, and ran ten times its intended
    wake rate for an hour without anything in the response revealing it."""
    ceiling = _low_ceiling(monkeypatch)
    room = await make_room()
    ctx = ctx_for("cdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcd")
    joined = await mcp_server.join_room(
        invitation_token=room.join_token,
        execution_mode="unattended_loop",
        display_name="Poller",
        ctx=ctx,
    )
    result = await mcp_server.await_room_events(
        since_seq=joined["cursor"], timeout_seconds=240, ctx=ctx
    )

    assert result["ok"] is True
    assert result["polled_seconds"] == ceiling
    capped = result["timeout_was_capped"]
    assert capped["requested_seconds"] == 240
    assert capped["granted_seconds"] == ceiling
    assert "polled_seconds" in capped["note"]


async def test_a_poll_within_the_ceiling_is_not_flagged(make_room):
    """No warning where there is nothing to warn about, or the field becomes noise and
    stops being read."""
    room = await make_room()
    ctx = ctx_for("efefefefefefefefefefefefefefefef")
    joined = await mcp_server.join_room(
        invitation_token=room.join_token,
        execution_mode="unattended_loop",
        display_name="Poller",
        ctx=ctx,
    )
    result = await mcp_server.await_room_events(
        since_seq=joined["cursor"], timeout_seconds=1, ctx=ctx
    )

    assert result["polled_seconds"] == 1
    assert "timeout_was_capped" not in result


# ---------------------------------------------------------------------------
# Every lease-gated MCP call names its own runtime
# ---------------------------------------------------------------------------


async def test_renew_release_and_checkpoint_name_the_calling_runtime(make_room, monkeypatch):
    """`claim` and `complete` already did. These three guessed, and the guess reads the
    newest open connection — so a sibling attaching after the claim wins it. D-035 uses
    release as its own example: a chat surface releasing a worker's lease is as dangerous
    as seizing it, because both end with two runtimes free to act."""
    seen: list[str | None] = []
    original = tasks.require_executor_or_dead

    async def recording(tx, row, **kwargs):
        seen.append(kwargs.get("connection_id"))
        return await original(tx, row, **kwargs)

    monkeypatch.setattr(tasks, "require_executor_or_dead", recording)

    room = await make_room()
    ctx = ctx_for("12121212121212121212121212121212")
    joined = await mcp_server.join_room(
        invitation_token=room.join_token,
        execution_mode="unattended_loop",
        display_name="Worker",
        ctx=ctx,
    )
    session_connection = joined["connection_id"]

    created = await mcp_server.create_task(title="Thread the runtime through", ctx=ctx)
    task_id = created["task"]["id"]
    claimed = await mcp_server.claim_task(task_id=task_id, ctx=ctx)
    fence = claimed["task"]["claim"]["fence"]

    checkpointed = await mcp_server.record_checkpoint(
        task_id=task_id, fence=fence, summary="halfway", ctx=ctx
    )
    assert checkpointed["ok"] is True, checkpointed
    renewed = await mcp_server.renew_task_claim(task_id=task_id, fence=fence, ctx=ctx)
    assert renewed["ok"] is True, renewed
    released = await mcp_server.release_task_claim(
        task_id=task_id, fence=renewed["task"]["claim"]["fence"], ctx=ctx
    )
    assert released["ok"] is True, released

    assert seen, "the affinity check never ran"
    assert all(c == session_connection for c in seen), seen


async def test_an_explicit_token_caller_still_passes_none(make_room, join):
    """There is genuinely no transport identity to offer, and inventing one would be
    worse than admitting the absence."""
    room = await make_room()
    member = await join(room, display_name="Direct")
    task = await tasks.create(
        participant=member.participant,
        command=CreateTaskCommand(title="No session here"),
    )
    claimed = await tasks.claim(
        participant=member.participant,
        command=ClaimTaskCommand(task_id=task.id),
    )
    assert claimed.claim is not None
    assert mcp_server._session_connection_id(None, member.participant) is None
