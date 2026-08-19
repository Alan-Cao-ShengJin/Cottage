"""A person talking through their agent (D-090).

The reported symptom was from the agent's side: *it cannot tell a prompt from a chat message
its human wants sent to the other people in the room.* It cannot, and the room gave it no way
to say which it had decided — so every relayed remark arrived as an agent coordinating, and
`a309cfb`'s human-chatter suppression never fired for the case that actually happens.

`a309cfb` keyed the rule on the speaker's `PrincipalKind`, which is right whenever a human is
a participant in their own name — somebody in the browser — and wrong when the human is
typing into an agent's interface. So the message now declares whose words it carries and the
wake rule reads the declaration. Behaviour from something declared about the payload, never
from a label about what is holding the keyboard (principle 4).

What must stay true, and each of these is a test below:

* **Delivery never changes.** Only the wake changes. A suppressed message is appended,
  privacy-filtered and served exactly as before, and any reader can pull it from the log.
* **A directed message always wakes its recipient**, whoever is speaking. That is the
  channel used when somebody needs a specific answer.
* **The default is unchanged.** An agent that never passes the field behaves exactly as it
  did, which is what makes this safe to deploy under running clients.
* **An unknown value is not human speech.** A typo must not silently make a message stop
  waking anyone; the failure worth engineering against is a relay that goes quiet.
"""

from __future__ import annotations

import pytest

from app.adapters.mcp import compact
from app.adapters.mcp import server as mcp_tools
from app.core import messages, projections
from app.db import database as db
from app.domain import relevance
from app.domain.commands import PostMessageCommand
from app.domain.disclosure import Disclosure
from app.domain.identity import PrincipalKind
from app.domain.message import Speaker

pytestmark = pytest.mark.asyncio


def _wakes(payload: dict, *, actor_kind=PrincipalKind.AGENT, viewer="reader") -> bool:
    return relevance.wakes(
        event_type="message.posted",
        payload=payload,
        actor_participant_id="speaker",
        viewer_participant_id=viewer,
        actor_kind=actor_kind,
        viewer_kind=PrincipalKind.AGENT,
    )


# ---------------------------------------------------------------------------
# The rule itself
# ---------------------------------------------------------------------------


async def test_an_agent_relaying_its_person_does_not_wake_the_other_agents():
    """The whole point. Before this the same words woke every subscriber, because the room
    saw an agent speaking and had no way to learn otherwise."""
    assert _wakes({"body": "the reconnect fix is in", "speaking_for": "agent"})
    assert not _wakes({"body": "anyone want lunch?", "speaking_for": "human"})


async def test_the_identity_kind_path_still_works_for_a_human_in_their_own_name():
    """a309cfb's rule is not replaced, it is joined. A person in the browser is a human
    identity and needs no declaration; a person behind an agent needs one, and both answer
    the same question."""
    assert not _wakes({"body": "anyone want lunch?"}, actor_kind=PrincipalKind.HUMAN)


async def test_a_directed_relay_still_wakes_the_participant_it_names():
    """The expensive mistake here would be silencing an instruction rather than chatter, so
    the narrowness is deliberate: only an *undirected* remark goes quiet."""
    assert _wakes(
        {
            "body": "can you take the schema change?",
            "speaking_for": "human",
            "to_participant_id": "reader",
        }
    )


async def test_an_unknown_declaration_is_not_treated_as_human_speech():
    """A typo must not make a message stop waking anyone. The default is the coordination
    case and anything unrecognised falls back to it."""
    assert _wakes({"body": "hello", "speaking_for": "person"})
    assert _wakes({"body": "hello", "speaking_for": ""})
    assert _wakes({"body": "hello"})


async def test_a_relayed_remark_between_two_people_never_wakes_either_agent():
    """Two humans, two agents, one room: the case that was measured costing a model turn per
    subscriber per remark."""
    for viewer in ("agent-a", "agent-b"):
        assert not _wakes(
            {"body": "shall we ship tonight?", "speaking_for": "human"}, viewer=viewer
        )


# ---------------------------------------------------------------------------
# Through the transports
# ---------------------------------------------------------------------------


async def test_the_tool_carries_the_declaration_onto_the_event_and_the_board(make_room):
    fixture = await make_room()
    posted = await mcp_tools.post_message(
        body="Alan says he is happy with the merge",
        speaking_for="human",
        participant_token=fixture.owner_token,
    )
    assert posted["ok"] is True

    page = await mcp_tools.await_room_events(
        since_seq=0, timeout_seconds=0, participant_token=fixture.owner_token
    )
    event = next(e for e in page["events"] if e["type"] == "message.posted")
    assert event["speaking_for"] == "human"

    snapshot = await projections.snapshot(room_id=fixture.room.id, recipient=fixture.owner)
    assert [m["speaking_for"] for m in snapshot["messages"]] == ["human"]


async def test_the_default_is_the_agent_and_costs_nothing_to_say(make_room):
    """A client that never passes the field behaves exactly as before — which is what makes
    this deployable under running clients."""
    fixture = await make_room()
    await mcp_tools.post_message(body="ported the reducers", participant_token=fixture.owner_token)

    snapshot = await projections.snapshot(room_id=fixture.room.id, recipient=fixture.owner)
    assert snapshot["messages"][0]["speaking_for"] == "agent"

    # And the compact view stays silent about it, on this module's own rule that a field earns
    # its place by changing a decision.
    view = compact.room_state(snapshot)
    assert "speaking_for" not in view["recent_messages"][0]


async def test_the_compact_view_names_a_relayed_person(make_room):
    fixture = await make_room()
    await mcp_tools.post_message(
        body="Alan wants the CSP fix out first",
        speaking_for="human",
        participant_token=fixture.owner_token,
    )
    snapshot = await projections.snapshot(room_id=fixture.room.id, recipient=fixture.owner)
    assert compact.room_state(snapshot)["recent_messages"][0]["speaking_for"] == "human"


async def test_an_unknown_value_is_answered_as_data_rather_than_raised(make_room):
    """`Speaker("person")` raises bare ValueError, which is not a RoomError and would escape
    the tool's only handler — the call would fail as a raw transport exception the model
    cannot read or correct."""
    fixture = await make_room()
    result = await mcp_tools.post_message(
        body="hello", speaking_for="person", participant_token=fixture.owner_token
    )
    assert result["ok"] is False
    assert result["error"] == "invalid_command"
    assert result["message"].startswith("speaking_for must be one of ")


async def test_arp_http_accepts_it_without_a_route_change(make_room):
    """The route takes the command as its body, so parity is automatic rather than
    maintained — which is the reason commands are the body shape."""
    from app.api import routes as http

    fixture = await make_room()
    result = await http.post_message(
        room_id=fixture.room.id,
        participant=fixture.owner,
        command=PostMessageCommand(
            body="Alan says go", speaking_for=Speaker.HUMAN, disclosure=Disclosure()
        ),
    )
    assert result["ok"] is True
    row = await db.fetch_one(
        "SELECT speaking_for FROM messages WHERE id = ?", (result["message_id"],)
    )
    assert row["speaking_for"] == "human"


# ---------------------------------------------------------------------------
# Delivery, and the rows that predate the column
# ---------------------------------------------------------------------------


async def test_suppressing_a_wake_does_not_suppress_the_message(make_room, join):
    """Stated as a test because it is the property most easily lost by a later change: the
    room delivers this to everyone and only declines to *wake* an agent for it."""
    fixture = await make_room()
    reader = await join(fixture, display_name="Bea")
    await messages.post(
        participant=fixture.owner,
        command=PostMessageCommand(
            body="anyone want lunch?", speaking_for=Speaker.HUMAN, disclosure=Disclosure()
        ),
    )

    theirs = await projections.snapshot(room_id=fixture.room.id, recipient=reader.participant)
    assert [m["body"] for m in theirs["messages"]] == ["anyone want lunch?"]

    page = await mcp_tools.await_room_events(
        since_seq=0, timeout_seconds=0, participant_token=reader.token
    )
    assert any(e["type"] == "message.posted" for e in page["events"])
    # Delivered, and honestly classed: the poller is told it was not worth a turn rather than
    # having the event withheld.
    relayed = next(e for e in page["events"] if e["type"] == "message.posted")
    assert relayed["speaking_for"] == "human"


async def test_a_message_written_before_the_column_existed_reads_as_the_agent(make_room):
    """The additive migration defaults to `agent`, which is the honest reading: back then the
    room had no way to say otherwise and the wake rule treated every one that way."""
    fixture = await make_room()
    await db.execute(
        "INSERT INTO messages (id, room_id, seq, participant_id, body, about_ref, "
        "privacy_class, audience, to_participant_id, created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (
            "msg_legacy",
            fixture.room.id,
            1,
            fixture.owner.id,
            "written before the column existed",
            None,
            "room_public",
            "room",
            None,
            "2026-08-01T00:00:00Z",
        ),
    )
    row = await db.fetch_one("SELECT speaking_for FROM messages WHERE id = 'msg_legacy'")
    assert row["speaking_for"] == "agent"


async def test_the_migration_is_registered_so_a_live_database_gains_the_column():
    """The schema file covers a fresh database; a deployed one only gains it through here.
    Three bugs have reached production-shaped failure while a green gate ran against a
    freshly-created schema."""
    assert ("messages", "speaking_for", "TEXT NOT NULL DEFAULT 'agent'") in db.ADDITIVE_COLUMNS


async def test_the_briefing_tells_an_agent_how_to_decide():
    """The room can carry the distinction; the agent still has to make it. A flag nobody is
    told about is a flag nobody sets."""
    briefing = await mcp_tools.get_protocol_briefing()
    assert 'speaking_for="human"' in briefing
    assert "Relaying is not acting" in briefing
    # The instruction for the case it genuinely cannot resolve.
    assert "ask them" in briefing


# ---------------------------------------------------------------------------
# Attribution: a relayed remark must read as the person, not as the seat
# ---------------------------------------------------------------------------


async def test_a_relay_is_attributed_to_the_person_with_the_seat_beside_it(make_room):
    """`> anyone wanna get lunch?` has to come out as *Alan* asking, not as Alan's agent.

    The seat is the agent in a relay, so without a name the room shows the agent saying it and
    human-to-human chat reads as two agents talking. With one it shows both — and both halves
    are load-bearing, which is the next test.
    """
    fixture = await make_room()
    await mcp_tools.post_message(
        body="anyone wanna get lunch?",
        speaking_for="human",
        speaking_as="Alan",
        participant_token=fixture.owner_token,
    )

    snapshot = await projections.snapshot(room_id=fixture.room.id, recipient=fixture.owner)
    row = snapshot["messages"][0]
    assert row["speaking_as"] == "Alan"
    assert row["participant_id"] == fixture.owner.id

    rendered = compact.room_state(snapshot)["recent_messages"][0]
    assert rendered["said_by"] == "Alan"
    # The seat survives, in `from`, and the name is marked as unverified.
    assert rendered["from"] == fixture.owner.id
    assert rendered["said_by_is_self_asserted"] is True


async def test_the_name_never_replaces_the_seat(make_room, join):
    """Rendered alone, a self-asserted name lets one participant post under another
    participant's name with nothing to show the difference. The room cannot verify who typed,
    so it shows what it knows — the seat — beside what it was told."""
    fixture = await make_room()
    other = await join(fixture, display_name="Bea's agent")
    await mcp_tools.post_message(
        body="I approve the merge",
        speaking_for="human",
        speaking_as="Bea",
        participant_token=fixture.owner_token,
    )

    snapshot = await projections.snapshot(room_id=fixture.room.id, recipient=fixture.owner)
    rendered = compact.room_state(snapshot)["recent_messages"][0]
    assert rendered["said_by"] == "Bea"
    # Claimed to be Bea; posted from the owner's seat, and the reader can see that.
    assert rendered["from"] == fixture.owner.id
    assert rendered["from"] != other.participant.id


async def test_a_name_without_a_relay_is_refused_rather_than_dropped(make_room):
    """A person's name attached to the agent's own words: one of the two is wrong and the room
    cannot tell which. Storing it would attribute the agent's words to a person; dropping it
    would silently discard an attribution somebody asked for."""
    fixture = await make_room()
    result = await mcp_tools.post_message(
        body="ported the reducers",
        speaking_as="Alan",
        participant_token=fixture.owner_token,
    )
    assert result["ok"] is False
    assert result["error"] == "invalid_command"
    assert 'speaking_for="human"' in result["message"]


async def test_the_relayed_name_is_content_inspected_like_the_body(make_room):
    """Free text crossing a room boundary, so it goes through the disclosure path rather than
    being trusted because it is short."""
    from app.core.errors import PrivacyViolation

    fixture = await make_room()
    with pytest.raises(PrivacyViolation):
        await messages.post(
            participant=fixture.owner,
            command=PostMessageCommand(
                body="lunch?",
                speaking_for=Speaker.HUMAN,
                speaking_as="sk-ant-api03-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
                disclosure=Disclosure(),
            ),
        )


async def test_the_briefing_and_the_tool_both_tell_an_agent_to_pass_the_name():
    """The room can carry the attribution; the agent has to supply it. A field nobody is told
    about is a field nobody sets — and the failure is silent, because the message still posts."""
    briefing = await mcp_tools.get_protocol_briefing()
    assert "speaking_as" in briefing
    assert "via Claude Code" in briefing
    # And the convention itself, where a person will be told to type it.
    assert "anyone want lunch?" in briefing
    assert "> " in briefing
