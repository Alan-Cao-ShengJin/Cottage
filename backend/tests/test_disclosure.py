"""The disclosure boundary.

Correction 2: domain shape narrows the surface but cannot be the control, because
messages, task descriptions, and state values are free text. These tests exercise the
three gates in `core/privacy.py` — authorization, policy, inspection — and pin the
"reject, never scrub" rule.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.core import messages, privacy, tasks, work
from app.core.errors import PrivacyViolation
from app.db import database as db
from app.domain.commands import (
    CreateTaskCommand,
    DeclareWorkCommand,
    PostMessageCommand,
)
from app.domain.disclosure import Audience, Disclosure
from app.domain.identity import TrustTier
from app.domain.room import PrivacyClass, RoomPolicy, RoomVisibility

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Inspection: content that must never enter a room
# ---------------------------------------------------------------------------


# Every value below is fake, and deliberately credential-*shaped*: the inspector matches
# on shape, so fixtures that do not look real would not test it. Two are assembled from
# fragments rather than written as literals — GitHub's push protection scans for exactly
# these prefixes and blocked a push over them. The string handed to the inspector is
# byte-identical either way; only the on-disk literal is gone.
#
# Worth recording rather than quietly patching: the pre-push scan run here looked for the
# specific tokens known to be live and found nothing, while GitHub looked for token
# *shapes* and found these. Searching for known values misses what searching for patterns
# catches, which is the same lesson as D-030 arriving from the opposite direction.
CREDENTIAL_SHAPED = [
    pytest.param("sk-abcdefghijklmnopqrstuvwxyz012345", id="openai_key"),
    pytest.param("sk-ant-api03-abcdefghijklmnopqrstuvwxyz0123456789", id="anthropic_key"),
    pytest.param("AKIAIOSFODNN7EXAMPLE", id="aws_access_key"),
    pytest.param("ghp" + "_" + "abcdefghijklmnopqrstuvwxyz0123456789", id="github_token"),
    pytest.param("xoxb" + "-1234567890-abcdefghijklmno", id="slack_token"),
    pytest.param(
        "-----BEGIN RSA PRIVATE KEY-----\nMIIEow==\n-----END RSA PRIVATE KEY-----",
        id="private_key_block",
    ),
    pytest.param(
        "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dBjftJeZ4CVPmB92K27uhbUJU1p1r",
        id="jwt",
    ),
    pytest.param("Authorization: Bearer abcdef0123456789abcdef", id="bearer_header"),
    pytest.param("db_password = hunter2hunter2", id="password_assignment"),
    pytest.param("postgres://user:s3cretpw@db.internal:5432/app", id="connection_string"),
]


@pytest.mark.parametrize("payload", CREDENTIAL_SHAPED)
async def test_credential_shaped_content_is_rejected_in_a_message(make_room, join, payload):
    room = await make_room()
    alice = await join(room, display_name="Alice")

    with pytest.raises(PrivacyViolation):
        await messages.post(participant=alice.participant, command=PostMessageCommand(body=payload))

    rows = await db.fetch_all("SELECT id FROM messages WHERE room_id = ?", (room.room.id,))
    assert rows == [], "rejected content must not be stored in any form"


@pytest.mark.parametrize("payload", CREDENTIAL_SHAPED)
def test_credential_shaped_content_is_rejected_anywhere_in_a_nested_payload(payload):
    """Inspection walks nested structures, so burying a secret in a JSON value does
    not evade it."""
    with pytest.raises(PrivacyViolation):
        privacy.inspect_content({"outer": [{"inner": {"note": payload}}]})


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param("System prompt: you are a helpful assistant that...", id="system_prompt"),
        pytest.param("<|im_start|>system\nYou are...<|im_end|>", id="chat_transcript"),
        pytest.param("<thinking>the user probably wants...</thinking>", id="reasoning_block"),
    ],
)
async def test_private_context_shaped_content_is_rejected(make_room, join, payload):
    """An agent's own prompt/reasoning has no field to live in *and* is refused if
    someone pastes it into one that exists."""
    room = await make_room()
    alice = await join(room, display_name="Alice")

    with pytest.raises(PrivacyViolation) as exc:
        await messages.post(participant=alice.participant, command=PostMessageCommand(body=payload))
    assert exc.value.code == "privacy_violation"


async def test_rejection_is_not_a_silent_scrub(make_room, join):
    """A scrub would teach the caller the channel accepted that content."""
    room = await make_room()
    alice = await join(room, display_name="Alice")
    body = "here is the key sk-abcdefghijklmnopqrstuvwxyz012345 for you"

    with pytest.raises(PrivacyViolation):
        await messages.post(participant=alice.participant, command=PostMessageCommand(body=body))

    rows = await db.fetch_all("SELECT body FROM messages WHERE room_id = ?", (room.room.id,))
    assert rows == []
    events = await db.fetch_all(
        "SELECT type FROM room_events WHERE room_id = ? AND type = 'message.posted'",
        (room.room.id,),
    )
    assert events == [], "no event may be appended for refused content"


def test_the_error_never_echoes_the_detected_secret():
    """Echoing it back through an error channel would defeat the check."""
    secret = "sk-abcdefghijklmnopqrstuvwxyz012345"
    with pytest.raises(PrivacyViolation) as exc:
        privacy.inspect_content(f"leaking {secret} here")
    rendered = f"{exc.value.message} {exc.value.details}"
    assert secret not in rendered
    assert "openai_key" in rendered, "the rule name is useful; the match is not"


def test_ordinary_coordination_content_passes():
    """A guard that blocks real work is worse than no guard, so the false-positive
    surface is tested explicitly."""
    privacy.inspect_content(
        "Refactoring src/api/routes.py to split the stream handler.",
        'Traceback (most recent call last):\n  File "app/main.py", line 42, in <module>',
        ["src/api/routes.py", "docs/PROTOCOL.md", "ticket-4192"],
        {"decision": "use per-room seq", "confidence": 0.8},
        "See https://github.com/example/repo/pull/1234 for context",
        "The commit is a1b2c3d4e5f6 on branch feature/lease-fencing",
    )


def test_oversized_text_is_refused():
    with pytest.raises(PrivacyViolation) as exc:
        privacy.inspect_content("x" * 20_000, max_text_chars=8_000)
    assert exc.value.details["rule"] == "max_text_chars"


def test_high_entropy_token_is_refused_but_prose_is_not():
    """The entropy screen catches an opaque credential with no known vendor prefix."""
    with pytest.raises(PrivacyViolation):
        privacy.inspect_content("token=" + "aZ3kQ9mT7xL2pR8wN4vB6yH1jS5dF0gC3eK7uI9oP2aZ")

    # A long path, a long sentence, and a hash are all fine.
    privacy.inspect_content(
        "src/very/deeply/nested/module/with/a/long/path/implementation_details.py"
    )
    privacy.inspect_content("a" * 60)


# ---------------------------------------------------------------------------
# Authorization gate
# ---------------------------------------------------------------------------


async def test_untrusted_participant_may_only_assert_room_public(make_room, join):
    room = await make_room(visibility=RoomVisibility.CROSS_ORG)
    from app.core import rooms as room_service

    other_org, _ = await room_service.ensure_org_and_user(
        org_name="Zeta", org_slug="zeta", email="z@zeta.test", display_name="Z"
    )
    outsider = await join(
        room, display_name="Untrusted", org_id=other_org, trust=TrustTier.UNTRUSTED
    )

    # room_public is fine.
    await messages.post(participant=outsider.participant, command=PostMessageCommand(body="hello"))
    # Anything else is refused.
    for cls in (PrivacyClass.ORG_INTERNAL, PrivacyClass.PARTICIPANT_PRIVATE):
        with pytest.raises(PrivacyViolation):
            await messages.post(
                participant=outsider.participant,
                command=PostMessageCommand(
                    body="hello again", disclosure=Disclosure(privacy_class=cls)
                ),
            )


async def test_provenance_marks_an_untrusted_assertion_unverified(make_room, join):
    """Attribution is the integrity control, so an unverified claim must be labeled."""
    room = await make_room(visibility=RoomVisibility.CROSS_ORG)
    from app.core import rooms as room_service

    other_org, _ = await room_service.ensure_org_and_user(
        org_name="Eta", org_slug="eta", email="e2@eta.test", display_name="E2"
    )
    outsider = await join(
        room, display_name="Untrusted", org_id=other_org, trust=TrustTier.UNTRUSTED
    )
    trusted = await join(room, display_name="Insider")

    untrusted_prov = privacy.build_provenance(outsider.participant, source="its own analysis")
    trusted_prov = privacy.build_provenance(trusted.participant, source="the repo")

    assert untrusted_prov.unverified is True
    assert trusted_prov.unverified is False
    # And the author cannot forge who asserted it.
    assert untrusted_prov.asserted_by_participant_id == outsider.participant.id


# ---------------------------------------------------------------------------
# Policy gate
# ---------------------------------------------------------------------------


async def test_org_audience_is_meaningless_in_a_cross_org_room(make_room, join):
    room = await make_room(visibility=RoomVisibility.CROSS_ORG)
    alice = await join(room, display_name="Alice")

    with pytest.raises(PrivacyViolation):
        await messages.post(
            participant=alice.participant,
            command=PostMessageCommand(
                body="team only", disclosure=Disclosure(audience=Audience.ORG)
            ),
        )


async def test_directed_message_to_a_non_member_is_refused(make_room, join):
    from app.core.errors import InvalidCommand

    room = await make_room()
    alice = await join(room, display_name="Alice")

    with pytest.raises(InvalidCommand):
        await messages.post(
            participant=alice.participant,
            command=PostMessageCommand(
                body="hello?",
                disclosure=Disclosure(
                    audience=Audience.PARTICIPANT, to_participant_id="par_DOESNOTEXIST"
                ),
            ),
        )


async def test_room_message_cap_is_enforced_from_room_policy(make_room, join):
    from app.domain.room import RoomPolicy

    room = await make_room(policy=RoomPolicy(max_message_chars=50))
    alice = await join(room, display_name="Alice")

    await messages.post(participant=alice.participant, command=PostMessageCommand(body="ok"))
    with pytest.raises(PrivacyViolation) as exc:
        await messages.post(
            participant=alice.participant, command=PostMessageCommand(body="y" * 200)
        )
    assert exc.value.details["rule"] == "max_text_chars"


# ---------------------------------------------------------------------------
# The gate applies to every content-bearing surface, not just messages
# ---------------------------------------------------------------------------


async def test_task_description_goes_through_the_same_gate(make_room, join):
    room = await make_room()
    alice = await join(room, display_name="Alice")

    with pytest.raises(PrivacyViolation):
        await tasks.create(
            participant=alice.participant,
            command=CreateTaskCommand(
                title="Rotate the key",
                description="use AKIAIOSFODNN7EXAMPLE to do it",
            ),
        )
    rows = await db.fetch_all("SELECT id FROM tasks WHERE room_id = ?", (room.room.id,))
    assert rows == []


async def test_work_declaration_goes_through_the_same_gate(make_room, join):
    room = await make_room()
    alice = await join(room, display_name="Alice")

    with pytest.raises(PrivacyViolation):
        await work.declare(
            participant=alice.participant,
            command=DeclareWorkCommand(
                headline="Deploying with the prod token",
                note="ghp_abcdefghijklmnopqrstuvwxyz0123456789",
                targets=["deploy.sh"],
            ),
        )
    rows = await db.fetch_all("SELECT id FROM work_declarations WHERE room_id = ?", (room.room.id,))
    assert rows == []


async def test_targets_are_inspected_too(make_room, join):
    """Targets are structured-looking, which makes them a tempting hiding place."""
    room = await make_room()
    alice = await join(room, display_name="Alice")

    with pytest.raises(PrivacyViolation):
        await work.declare(
            participant=alice.participant,
            command=DeclareWorkCommand(
                headline="normal looking work",
                targets=["src/api.py", "sk-abcdefghijklmnopqrstuvwxyz012345"],
            ),
        )


async def test_the_disclosure_decision_is_recorded_on_the_event(make_room, join):
    """What was disclosed, by whom, to whom, under what class — permanently auditable."""
    room = await make_room()
    alice = await join(room, display_name="Alice")
    bob = await join(room, display_name="Bob")

    await messages.post(
        participant=alice.participant,
        command=PostMessageCommand(
            body="for you only",
            disclosure=Disclosure(
                audience=Audience.PARTICIPANT, to_participant_id=bob.participant.id
            ),
        ),
    )

    row = await db.fetch_one(
        "SELECT * FROM room_events WHERE room_id = ? AND type = 'message.posted'",
        (room.room.id,),
    )
    assert row["audience"] == "participant"
    assert row["privacy_class"] == "room_public"
    assert row["actor_participant_id"] == alice.participant.id
    restricted = db.str_list(row["restricted_to_participant_ids"])
    assert set(restricted) == {alice.participant.id, bob.participant.id}


async def test_a_misspelled_addressee_is_rejected_rather_than_published(make_room, join):
    """An ignored field is a leak; a rejected command is a bad request.

    `to_participant_id` is a real field name three places in this system — on
    `Disclosure`, on messages, on task proposals — just not directly on
    `PostMessageCommand`. So writing it there is the natural mistake, and Pydantic's
    default of dropping unknown fields turns it into a silent privacy downgrade: the
    author asked for one recipient and the room publishes to everyone, with no error
    anywhere. That is the same shape as D-024, D-026, D-027 and D-030 — a control that
    appears to work and does the opposite.
    """
    room = await make_room()
    alice = await join(room, display_name="Alice")
    bob = await join(room, display_name="Bob")

    with pytest.raises(ValidationError) as rejected:
        PostMessageCommand(body="salary figures", to_participant_id=bob.participant.id)
    assert "to_participant_id" in str(rejected.value)

    # The correct form still works, and lands as a restricted event rather than a
    # room-wide one — so the rule rejects the mistake without narrowing the feature.
    await messages.post(
        participant=alice.participant,
        command=PostMessageCommand(
            body="salary figures",
            disclosure=Disclosure(
                audience=Audience.PARTICIPANT, to_participant_id=bob.participant.id
            ),
        ),
    )
    row = await db.fetch_one(
        "SELECT * FROM room_events WHERE room_id = ? AND type = 'message.posted'",
        (room.room.id,),
    )
    assert db.str_list(row["restricted_to_participant_ids"])


async def test_a_misspelled_field_inside_a_nested_disclosure_is_rejected_too():
    """`extra="forbid"` on CommandMeta does not reach nested models (D-042).

    Pydantic config is per class, not per object graph, so the command-level fix left
    the level below it untouched — and that level is the one that decides who may see
    the content. Before this, `Disclosure(privacy_clas="org_internal")` produced
    `room_public` and published to the whole room something the author had classified as
    internal: the identical silent downgrade, one layer down, found by review rather
    than by the suite that had just been written for the layer above.
    """
    with pytest.raises(ValidationError):
        Disclosure(privacy_clas="org_internal")

    with pytest.raises(ValidationError):
        PostMessageCommand(
            body="internal only",
            disclosure={"audience": "participant", "to_participant_i": "par_typo"},
        )

    # RoomPolicy is inbound on CreateRoomCommand, where a dropped field silently
    # selects a different policy than the one asked for.
    with pytest.raises(ValidationError):
        RoomPolicy(allow_attended_claim=True)


def test_our_own_ids_are_not_credentials():
    """The screen refused a participant for saying where a thing lives.

    Found live: a message quoting `/api/rooms/room_.../directives` was rejected as
    an opaque high-entropy token. An id in a URL path is a long unbroken run that
    mixes character classes, so it looked exactly like a leaked key — while every
    event payload in the room already carries the same value.

    Coordinating means naming the task or participant you mean. A rule that blocks
    that is not protecting anything; it is stopping the room from being used.
    """
    privacy.inspect_content(
        "the route is POST /api/rooms/room_01M022GNSYC29CSPWDDYBC/directives",
        "target par_01M022JPYA051P6HJEK6V8 on task tsk_01M01ZNVTF0NVM9X0T429K",
    )


def test_masking_ids_does_not_weaken_the_screen():
    """Only the id is neutralised, so a credential beside one still trips.

    The failure mode to avoid was fixing a false positive by widening it into a
    hole — 'ids are fine' becoming 'anything near an id is fine'.
    """
    opaque = "Zx9Qw8Er7Ty6Ui5Op4As3Df2Gh1Jk0Lz9Xc8Vb7Nm6Q"
    with pytest.raises(PrivacyViolation):
        privacy.inspect_content(f"room_01M022GNSYC29CSPWDDYBC and also {opaque}")
    with pytest.raises(PrivacyViolation):
        privacy.inspect_content(opaque)
