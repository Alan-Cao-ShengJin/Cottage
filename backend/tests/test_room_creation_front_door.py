"""Who may open a room (D-046).

This is step one of the core loop — CONNECT — and it was closed to the only host
family we had actually verified. `create_room` required a *user* principal, so an
OAuth-connected assistant could join rooms all day and never start one. The product
sentence is "ask your assistant to start a room, share the key with a friend", and a
human had to go and find an organization credential first.

The gate that survives is the one that was doing the real work: **account
provenance**. An identity a human with an account created or consented to may open
rooms in that org; an identity minted by redeeming someone's join link may not.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.core import rooms, store
from app.core.errors import Forbidden, Unauthenticated
from app.db import database as db
from app.domain.capabilities import HostClass
from app.domain.commands import CreateRoomCommand
from app.domain.identity import IdentityProvenance, PrincipalKind, TrustTier
from app.domain.room import RoomVisibility, Scope

pytestmark = pytest.mark.asyncio


async def _identity(org_id, owner_user_id, **kwargs):
    return await rooms.create_identity(
        org_id=org_id,
        owner_user_id=owner_user_id,
        display_name=kwargs.pop("display_name", "ChatGPT (Alan)"),
        kind=PrincipalKind.AGENT,
        host_class=HostClass.INTERACTIVE_CLIENT,
        capabilities=[],
        **kwargs,
    )


async def _principal_for(identity):
    return rooms.Principal(kind="agent_identity", org_id=identity.org_id, identity=identity)


async def test_an_agent_identity_can_start_a_room_and_owns_it(fresh_db, org):
    """The product's front door, from the host we verified.

    Owning it matters as much as creating it: a creator without `room.admin` could
    not steer anything in the room it just made, which is the authority blocker B
    ran into from the other direction.
    """
    org_id, user_id = org
    identity = await _identity(org_id, user_id)

    created = await rooms.create_room(
        principal=await _principal_for(identity),
        command=CreateRoomCommand(name="Alan and a friend"),
    )

    assert created.room.org_id == org_id
    assert Scope.ROOM_ADMIN in created.participant.scopes
    assert created.participant.identity.identity_id == identity.id
    assert created.join_token, "the one thing you hand to a friend"
    assert created.participant_token


async def test_the_key_it_hands_out_actually_lets_a_stranger_in(fresh_db, org):
    """The whole flow in one test: start a room, share the key, someone else joins.

    Written end-to-end on purpose. Each half passing separately is what let the
    combination stay broken — creating worked for humans, joining worked for agents,
    and nobody could do both.
    """
    org_id, user_id = org
    host = await _identity(org_id, user_id, display_name="ChatGPT (Alan)")
    created = await rooms.create_room(
        principal=await _principal_for(host),
        command=CreateRoomCommand(name="Two agents and a key"),
    )

    friend = await _identity(org_id, user_id, display_name="Claude (a friend)")
    from app.domain.commands import JoinRoomCommand

    joined = await rooms.join_room(
        identity=friend,
        command=JoinRoomCommand(
            invitation_token=created.join_token, display_name="Claude (a friend)"
        ),
    )
    assert joined.participant.room_id == created.room.id
    assert Scope.TASK_CLAIM in joined.participant.scopes
    assert Scope.ROOM_ADMIN not in joined.participant.scopes, "a guest is not an owner"


async def test_a_guest_identity_cannot_open_rooms_in_the_org_that_invited_it(fresh_db, org):
    """The tenancy hole the old gate was actually protecting against.

    Redeeming a link into one room must not become the ability to create rooms in
    the organization that sent it. This is the check worth keeping, and it is about
    provenance rather than about being human.
    """
    org_id, user_id = org
    guest = await _identity(
        org_id,
        user_id,
        display_name="Someone with a link",
        provenance=IdentityProvenance.INVITATION,
    )

    with pytest.raises(Forbidden):
        await rooms.create_room(
            principal=await _principal_for(guest),
            command=CreateRoomCommand(name="Not yours to make"),
        )


async def test_an_untrusted_identity_cannot_open_rooms(fresh_db, org):
    org_id, user_id = org
    stranger = await _identity(org_id, user_id, trust=TrustTier.UNTRUSTED)

    with pytest.raises(Forbidden):
        await rooms.create_room(
            principal=await _principal_for(stranger),
            command=CreateRoomCommand(name="Not yours either"),
        )


async def test_a_human_principal_still_works_unchanged(fresh_db, org):
    """The old path is widened, not replaced."""
    org_id, user_id = org
    user_row = await db.fetch_one("SELECT * FROM users WHERE id = ?", (user_id,))
    created = await rooms.create_room(
        user=store.to_user(user_row),
        command=CreateRoomCommand(name="The old way"),
        creator_display_name="Room Owner",
    )
    assert Scope.ROOM_ADMIN in created.participant.scopes
    assert created.room.org_id == org_id


async def test_room_create_retry_uses_stable_creator_binding_and_rotates_tokens(fresh_db, org):
    org_id, user_id = org
    identity = await _identity(org_id, user_id)
    principal = await _principal_for(identity)
    command = CreateRoomCommand(command_id="cmd-room-create-retry", name="Only one room")

    first = await rooms.create_room(principal=principal, command=command)
    seq_after_first = first.room.event_seq
    second = await rooms.create_room(principal=principal, command=command)

    assert second.room.id == first.room.id
    assert second.participant.id == first.participant.id
    assert second.invitation_id == first.invitation_id
    assert second.room.event_seq == seq_after_first
    assert await db.fetch_value("SELECT COUNT(*) FROM rooms WHERE name = 'Only one room'") == 1
    receipt = await db.fetch_one(
        "SELECT * FROM command_receipts WHERE command_type = 'room.create'"
    )
    assert receipt is not None
    assert receipt["room_id"] == org_id
    assert receipt["participant_id"] == identity.id

    with pytest.raises(Unauthenticated):
        await store.load_participant_by_token(first.participant_token)
    with pytest.raises(Unauthenticated):
        await rooms.authenticate_invitation(first.join_token)
    assert (
        await store.load_participant_by_token(second.participant_token)
    ).id == first.participant.id
    assert (
        await rooms.authenticate_invitation(second.join_token)
    ).invitation.id == first.invitation_id


async def test_room_create_matching_legacy_receipt_replays_for_same_creator(fresh_db, org):
    org_id, user_id = org
    identity = await _identity(org_id, user_id)
    principal = await _principal_for(identity)
    first = await rooms.create_room(
        principal=principal, command=CreateRoomCommand(name="Legacy room")
    )
    await db.execute(
        """
        INSERT INTO command_receipts (
            command_id, room_id, participant_id, command_type, seq, result, created_at
        ) VALUES (?,?,?,?,?,?,?)
        """,
        (
            "legacy-room-create",
            first.room.id,
            first.participant.id,
            "room.create",
            first.room.event_seq,
            db.dumps(
                {
                    "room_id": first.room.id,
                    "participant_id": first.participant.id,
                    "invitation_id": first.invitation_id,
                }
            ),
            "2026-08-16T00:00:00+00:00",
        ),
    )

    replayed = await rooms.create_room(
        principal=principal,
        command=CreateRoomCommand(command_id="legacy-room-create", name="must not exist"),
    )

    assert replayed.room.id == first.room.id
    assert replayed.participant.id == first.participant.id
    assert await db.fetch_value("SELECT COUNT(*) FROM rooms") == 1
    assert await db.fetch_value("SELECT COUNT(*) FROM command_receipts") == 1
    with pytest.raises(Unauthenticated):
        await store.load_participant_by_token(first.participant_token)
    assert (
        await store.load_participant_by_token(replayed.participant_token)
    ).id == first.participant.id


async def test_the_room_is_attributed_to_the_human_behind_the_agent(fresh_db, org):
    """`created_by_user_id` follows the identity's owner, not the caller's kind.

    An agent identity always belongs to a person; a room it opens is that person's
    room in every report that asks who owns what.
    """
    org_id, user_id = org
    identity = await _identity(org_id, user_id)
    created = await rooms.create_room(
        principal=await _principal_for(identity),
        command=CreateRoomCommand(name="Attributed"),
    )
    row = await db.fetch_one("SELECT * FROM rooms WHERE id = ?", (created.room.id,))
    assert row["created_by_user_id"] == user_id


async def test_an_oauth_caller_needs_no_credential_argument(fresh_db, org, monkeypatch):
    """The last mile: asking for a token the caller already presented.

    An assistant that authenticated with OAuth had to be handed an "organization
    principal token" to create a room — a credential its human has to go and find,
    which is the step the product exists to remove. It is also redundant: the server
    resolved that caller's identity to let the call through in the first place.
    """
    from app.adapters.mcp import server as mcp_server
    from app.core.oauth import TokenPrincipal

    org_id, user_id = org
    identity = await _identity(org_id, user_id)

    async def fake_caller(ctx, audience):
        return TokenPrincipal(
            subject_kind="agent_identity",
            org_id=org_id,
            identity=identity,
            user_id=None,
            scope="agent",
            client_id="cli_test",
        )

    monkeypatch.setattr(mcp_server, "principal_for_tool", fake_caller)

    for supplied in (None, "", "   "):
        principal = await mcp_server._creating_principal(None, supplied)
        assert principal.kind == "agent_identity"
        assert principal.identity is not None
        assert principal.identity.id == identity.id


async def test_a_supplied_token_that_is_wrong_is_refused_not_ignored(fresh_db, org, monkeypatch):
    """Falling back to the session would succeed as somebody else.

    That is the worst of the three outcomes: the caller believes it acted as the
    principal it named, and it did not.
    """
    from app.adapters.mcp import server as mcp_server
    from app.core.errors import Unauthenticated
    from app.core.oauth import TokenPrincipal

    org_id, user_id = org
    identity = await _identity(org_id, user_id)

    async def fake_caller(ctx, audience):
        return TokenPrincipal(
            subject_kind="agent_identity",
            org_id=org_id,
            identity=identity,
            user_id=None,
            scope="agent",
            client_id="cli_test",
        )

    monkeypatch.setattr(mcp_server, "principal_for_tool", fake_caller)

    with pytest.raises(Unauthenticated) as exc:
        await mcp_server._creating_principal(None, "not-a-real-token")
    # And the refusal has to tell a client with a cached schema what to send instead.
    assert "empty string" in str(exc.value)


async def test_the_mcp_tool_itself_creates_a_room_for_an_oauth_agent(fresh_db, org, monkeypatch):
    """Calls the tool, not the service under it.

    Written because the gate missed a whole class of bug twice in one hour: the
    service was widened to accept an agent identity, and the adapter kept passing
    `principal.user` — `None` for exactly the callers being enabled. Every service
    test passed and the only host that matters got "needs an authenticated
    principal" one second after authenticating.

    The lesson is about where the seam is: `core` had tests, the adapter had none,
    and the adapter is the half a real client actually touches.
    """
    from app.adapters.mcp import server as mcp_server
    from app.core.oauth import TokenPrincipal

    org_id, user_id = org
    identity = await _identity(org_id, user_id, display_name="ChatGPT (Alan)")

    async def fake_caller(ctx, audience):
        return TokenPrincipal(
            subject_kind="agent_identity",
            org_id=org_id,
            identity=identity,
            user_id=None,
            scope="agent",
            client_id="cli_test",
        )

    monkeypatch.setattr(mcp_server, "principal_for_tool", fake_caller)

    request = SimpleNamespace(headers={"mcp-session-id": "55555555555555555555555555555550"})
    ctx = SimpleNamespace(request_context=SimpleNamespace(request=request), session=object())
    result = await mcp_server.create_room(
        name="unattended proof",
        principal_token="",
        display_name="Caller-controlled spoof",
        execution_mode="unattended_loop",
        # `full`, because what this test is about — the identity the adapter used and the
        # name it refused to take from the caller — is exactly the plumbing the compact
        # response now hides from a human (D-085).
        detail="full",
        ctx=ctx,
    )
    assert result["ok"] is True, result
    assert result["join_token"], "the one thing a human hands to a friend"
    assert result["participant_token"]
    assert result["execution_mode"] == "unattended_loop"
    assert result["display_name"] == "ChatGPT (Alan)"
    assert result["display_name_was_overridden"] is True

    participant = await store.load_participant(result["participant_id"])
    assert Scope.ROOM_ADMIN in participant.scopes, "the creator must be able to steer"
    # The bound name, not the tool's "Room creator" default: the room an agent creates
    # must not be the one place it can call itself anything.
    assert participant.identity.display_name == "ChatGPT (Alan)"

    # Declaring before the first long poll is valid: create_room already connected
    # this exact MCP session, and every subsequent tool call heartbeats it first.
    declared = await mcp_server.declare_current_work(
        headline="Start immediately", targets=["room setup"], ctx=ctx
    )
    assert declared["ok"] is True
    assert declared["work"]["status"] == "active"
    assert declared["work"]["ended_at"] is None


async def test_a_new_room_is_cross_org_by_default(fresh_db, org):
    """The default has to match the sentence the product is judged against.

    `internal` was the default, and an internal room refuses a foreign-org identity
    outright at join — so "invite someone over the internet" failed on the one path
    where nobody passes an argument, which is every path an assistant takes when a
    human just says "make me a room". A single-org room is still available; it is now
    the deliberate choice rather than the silent one.
    """
    org_id, user_id = org
    identity = await _identity(org_id, user_id)

    created = await rooms.create_room(
        principal=await _principal_for(identity),
        command=CreateRoomCommand(name="Whoever you invite"),
    )

    assert created.room.visibility is RoomVisibility.CROSS_ORG


async def test_the_default_room_admits_someone_from_another_organization(fresh_db, org):
    """What the default is *for*, asserted through the join path that enforces it.

    Asserting the enum alone would pass while `join_room` still turned the stranger
    away, and that rejection is the failure this default exists to remove.
    """
    org_id, user_id = org
    host = await _identity(org_id, user_id, display_name="Claude Code (Alan)")
    created = await rooms.create_room(
        principal=await _principal_for(host),
        command=CreateRoomCommand(name="Two companies, one room"),
    )

    other_org_id, other_user_id = await rooms.ensure_org_and_user(
        org_name="Beta Co", org_slug="beta-co", email="owner@beta.test", display_name="Beta Owner"
    )
    assert other_org_id != org_id
    guest = await _identity(other_org_id, other_user_id, display_name="ChatGPT (a stranger)")

    from app.domain.commands import JoinRoomCommand

    joined = await rooms.join_room(
        identity=guest,
        command=JoinRoomCommand(
            invitation_token=created.join_token, display_name="ChatGPT (a stranger)"
        ),
    )

    assert joined.participant.room_id == created.room.id
    # Admitted, not trusted: a foreign-org identity arriving on a link invitation is
    # untrusted until vouched for, so it may contribute `room_public` content only.
    assert joined.participant.trust is TrustTier.UNTRUSTED


async def test_an_internal_room_is_still_available_and_still_closed(fresh_db, org):
    """The old behavior remains reachable, which is what makes the new default safe."""
    org_id, user_id = org
    host = await _identity(org_id, user_id)
    created = await rooms.create_room(
        principal=await _principal_for(host),
        command=CreateRoomCommand(name="Us only", visibility=RoomVisibility.INTERNAL),
    )
    assert created.room.visibility is RoomVisibility.INTERNAL

    other_org_id, other_user_id = await rooms.ensure_org_and_user(
        org_name="Beta Co", org_slug="beta-co", email="owner@beta.test", display_name="Beta Owner"
    )
    outsider = await _identity(other_org_id, other_user_id, display_name="Not invited in")

    from app.domain.commands import JoinRoomCommand

    with pytest.raises(Forbidden):
        await rooms.join_room(
            identity=outsider,
            command=JoinRoomCommand(
                invitation_token=created.join_token, display_name="Not invited in"
            ),
        )


async def _mcp_create(org, monkeypatch, session_suffix="1", **kwargs):
    """Call the MCP tool as an OAuth-authenticated agent identity."""
    from app.adapters.mcp import server as mcp_server
    from app.core.oauth import TokenPrincipal

    org_id, user_id = org
    identity = await _identity(org_id, user_id, display_name="Claude Code (Alan)")

    async def fake_caller(ctx, audience):
        return TokenPrincipal(
            subject_kind="agent_identity",
            org_id=org_id,
            identity=identity,
            user_id=None,
            scope="agent",
            client_id="cli_test",
        )

    monkeypatch.setattr(mcp_server, "principal_for_tool", fake_caller)
    request = SimpleNamespace(headers={"mcp-session-id": session_suffix.rjust(32, "9")})
    ctx = SimpleNamespace(request_context=SimpleNamespace(request=request), session=object())
    return await mcp_server.create_room(ctx=ctx, **kwargs)


async def test_create_room_returns_a_welcome_sheet_the_client_prints_verbatim(
    fresh_db, org, monkeypatch
):
    """What a person sees when they make a room is product behavior, not a rendering
    accident (D-085).

    Written because the sheet existed only in one assistant's prose: the tool returned
    eighteen flat fields, so a second client dumped the plumbing and a third invented a
    join snippet of its own. Asserting the *text* is the point — a structured field that
    each client re-renders is the bug, not the fix.
    """
    result = await _mcp_create(org, monkeypatch, name="Lantern Hour")
    assert result["ok"] is True, result

    sheet = result["welcome"]
    assert sheet.startswith("Welcome to Cottage")
    assert "Room:          Lantern Hour" in sheet
    assert "Owner:         You" in sheet
    assert "Orchestrator:  Your AI" in sheet
    assert "anyone you invite, including people outside your organization" in sheet
    assert "Status:        open" in sheet
    assert "24 hours" in sheet, "the room's real window, not a hardcoded one"
    assert "up to 50 seats" in sheet
    # Last line, after a blank one: it is the only line anyone acts on.
    lines = sheet.splitlines()
    assert lines[-1] == "Invitation:    " + result["join_token"]
    assert lines[-2] == "", "a blank line above it, so it reads as the thing to copy"


async def test_the_welcome_sheet_says_internal_when_the_room_is_internal(
    fresh_db, org, monkeypatch
):
    """The sheet describes the room it was given, not the default."""
    result = await _mcp_create(org, monkeypatch, name="Us only", cross_org=False)
    assert "people inside your organization only" in result["welcome"]
    assert "outside your organization" not in result["welcome"]


async def test_create_room_reports_both_expiries_and_the_seat_count(fresh_db, org, monkeypatch):
    """The gap a creator used to discover by the room lapsing."""
    result = await _mcp_create(org, monkeypatch, name="Windows stated")

    assert result["expires_at"], "the room's window"
    assert result["join_expires_at"], "the link's window"
    assert result["join_seats"] == 50
    # A link that outlives its room is useless, so it is capped by the room.
    assert result["join_expires_at"] <= result["expires_at"]


async def test_the_compact_response_hides_connection_plumbing(fresh_db, org, monkeypatch):
    """A response is spent context — the same rule every other tool here follows."""
    result = await _mcp_create(org, monkeypatch, name="Quiet by default")

    for field in (
        "negotiated_capabilities",
        "delivery_mode",
        "may_claim",
        "connection_id",
        "display_name_was_overridden",
        "share_this",
        "charter",
    ):
        assert field not in result, f"{field} is plumbing; it belongs behind detail=full"

    # Still everything a client needs to keep working. `participant_id` stays: it is how
    # a client finds its own card in a room read, and it costs one short string.
    assert result["participant_token"]
    assert result["participant_id"]
    assert result["join_token"]
    assert isinstance(result["cursor"], int)


async def test_detail_full_restores_every_field(fresh_db, org, monkeypatch):
    result = await _mcp_create(org, monkeypatch, name="Everything", detail="full")

    assert result["welcome"], "the sheet is not withheld by asking for more"
    assert result["negotiated_capabilities"]
    assert result["delivery_mode"] == "long_poll"
    assert result["may_claim"] is True
    assert result["connection_id"]
    assert result["participant_id"]
    assert result["execution_mode"] == "unattended_loop"
    assert result["display_name"] == "Claude Code (Alan)"


# ---------------------------------------------------------------------------
# The arrival sheet
# ---------------------------------------------------------------------------


async def _mcp_join(org, monkeypatch, invitation_token, *, display_name, execution_mode):
    """Join as a *different* identity from the creator.

    Worth the extra fixture: leaving the creator's patched principal in place makes the
    join a rejoin of the same seat, so the room reports "nobody else yet" and a test about
    seeing other participants quietly stops testing that.
    """
    from app.adapters.mcp import server as mcp_server
    from app.core.oauth import TokenPrincipal

    org_id, user_id = org
    joiner = await _identity(org_id, user_id, display_name=display_name)

    async def fake_caller(ctx, audience):
        return TokenPrincipal(
            subject_kind="agent_identity",
            org_id=org_id,
            identity=joiner,
            user_id=None,
            scope="agent",
            client_id="cli_joiner",
        )

    monkeypatch.setattr(mcp_server, "principal_for_tool", fake_caller)
    return await mcp_server.join_room(
        invitation_token=invitation_token,
        execution_mode=execution_mode,
        display_name=display_name,
    )


async def test_a_browser_assistant_is_told_what_it_cannot_do_on_arrival(fresh_db, org, monkeypatch):
    """The honesty rule, applied to the first thing a person reads (D-085).

    A chat assistant is genuinely unreachable between its human's messages. Principle 5
    is usually read as a constraint on server behavior, but an arrival sheet that lets
    someone believe their chat window is a live participant breaks it just as effectively:
    the room will be expected to wake something that cannot be woken.
    """
    created = await _mcp_create(org, monkeypatch, name="Tester", session_suffix="7")
    joined = await _mcp_join(
        org,
        monkeypatch,
        created["join_token"],
        display_name="ChatGPT",
        execution_mode="human_turn_only",
    )
    assert joined["ok"] is True, joined

    sheet = joined["welcome"]
    assert sheet.startswith("Welcome to Cottage")
    assert "Room:                      Tester" in sheet
    assert "Your Display Name:         ChatGPT" in sheet
    # Describes what the mode claims, not where it is usually claimed from. The first
    # version said "You are in a web browser session", which reads as false to a Claude Code
    # session driven turn by turn — honestly `human_turn_only`, and not a browser. Found by
    # a session reading its own arrival sheet and not recognising itself in it.
    assert "You act only when your person prompts you" in sheet
    assert "live room updates cannot" in sheet
    assert "web browser" not in sheet, "the mode is not a claim about the host (principle 4)"
    # The counterweight, and the reason this line is not simply a warning: "limited"
    # invites the reading that little of what you say arrives, when the truth is the
    # opposite — only inbound liveness is limited.
    assert "fully visible to everyone in the room" in sheet
    # And the remedy stated as the capability rather than as a product name. Asserted
    # within one line: the sheet wraps at a fixed column, so a phrase that spans the wrap
    # carries the injected newline and indent with it.
    assert "polling on its own clock stays live" in sheet
    # Lease policy is not what a person needs in their first four lines; it ships in the
    # structured fields for the agent instead.
    assert "Claiming tasks" not in sheet
    assert "allow_attended_claims" not in sheet, "the technical reason belongs in the fields"
    assert joined["may_claim"] is False, "still told to the agent"
    assert "allow_attended_claims" in joined["claim_denied_reason"]


async def test_a_looping_agent_gets_no_invented_caveat(fresh_db, org, monkeypatch):
    """No warning where there is nothing to warn about.

    Filling the line for every host would teach people to skim past the one host where it
    matters.
    """
    created = await _mcp_create(org, monkeypatch, name="Tester", session_suffix="8")
    joined = await _mcp_join(
        org,
        monkeypatch,
        created["join_token"],
        display_name="Codex",
        execution_mode="unattended_loop",
    )

    sheet = joined["welcome"]
    assert "Heads up:" not in sheet
    assert "web browser session" not in sheet
    assert joined["may_claim"] is True


async def test_the_arrival_sheet_names_who_is_already_here(fresh_db, org, monkeypatch):
    """Seeing the room is the point of arriving in it, so it is on the sheet by name."""
    created = await _mcp_create(org, monkeypatch, name="Tester", session_suffix="9")
    joined = await _mcp_join(
        org,
        monkeypatch,
        created["join_token"],
        display_name="ChatGPT",
        execution_mode="human_turn_only",
    )

    # By name, and the joiner does not list itself among the others.
    assert "Also here:                 Claude Code (Alan)" in joined["welcome"]
    assert "ChatGPT" not in joined["welcome"].split("Also here:")[1].splitlines()[0]
    assert "Current work in the room:  nothing yet" in joined["welcome"]


async def test_a_crowded_room_counts_the_rest_instead_of_listing_everyone(
    fresh_db, org, monkeypatch
):
    """Three names read as people; past that the count is the more useful fact."""
    created = await _mcp_create(org, monkeypatch, name="Busy", session_suffix="6")
    for who in ("Alan", "Codex", "Gemini", "Grok"):
        await _mcp_join(
            org,
            monkeypatch,
            created["join_token"],
            display_name=who,
            execution_mode="unattended_loop",
        )
    joined = await _mcp_join(
        org,
        monkeypatch,
        created["join_token"],
        display_name="ChatGPT",
        execution_mode="human_turn_only",
    )

    others = joined["welcome"].split("Also here:")[1].splitlines()[0]
    assert others.count(",") == 2, f"three names, then a count: {others}"
    assert "& 2 others" in others
