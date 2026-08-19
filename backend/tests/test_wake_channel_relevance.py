"""The wake channel: which events are worth spending a reader's turn on.

These are cost tests, not disclosure tests. Privacy is enforced before relevance is
ever consulted — `filter_events` runs first in the fanout — so nothing here asserts
who may see what. What they pin is that a filtered subscriber is woken for decisions
and left alone for narration, because a wake channel that fires on keepalives and
activity notes is indistinguishable from the polling loop it exists to replace.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import importlib.util
import sys
from pathlib import Path

import pytest

from app.api import routes
from app.api.routes import websocket_stream
from app.config import settings
from app.core import eventlog, messages, stream_tickets
from app.core.actors import actor_for
from app.db import database as db
from app.domain import relevance
from app.domain.commands import PostMessageCommand
from app.domain.disclosure import Disclosure
from app.domain.events import ControlFrame, EventType
from app.domain.identity import PrincipalKind
from app.domain.relevance import RelevanceClass

pytestmark = pytest.mark.asyncio


def _classify(event_type, payload=None, **kwargs) -> RelevanceClass:
    return relevance.classify(event_type=event_type, payload=payload or {}, **kwargs)


# ---------------------------------------------------------------------------
# What earns a turn
# ---------------------------------------------------------------------------


async def test_work_offered_to_this_seat_wakes_it():
    """The whole point: a joiner proposing a task must reach an idle agent."""
    assert _classify(EventType.TASK_PROPOSED) is RelevanceClass.JUDGEMENT
    assert relevance.wakes(event_type=EventType.TASK_PROPOSED, payload={})


async def test_being_told_to_do_something_wakes():
    for kind in (
        EventType.DIRECTIVE_ISSUED,
        EventType.TASK_STEERED,
        EventType.QUESTION_ASKED,
        EventType.RUNTIME_DRAINED,
    ):
        assert _classify(kind) is RelevanceClass.JUDGEMENT, kind


async def test_losing_a_lease_wakes_because_nothing_else_says_so():
    assert _classify(EventType.TASK_CLAIM_EXPIRED) is RelevanceClass.JUDGEMENT
    assert _classify(EventType.TASK_EXECUTOR_CHANGED) is RelevanceClass.JUDGEMENT


# ---------------------------------------------------------------------------
# What must never spend a turn
# ---------------------------------------------------------------------------


async def test_narration_never_wakes_a_model():
    """D-082 breadcrumbs are the feed, not news. One turn per breadcrumb is the
    anti-pattern this module exists to prevent."""
    assert _classify(EventType.ACTIVITY_NOTED, {"summary": "still going"}) is (RelevanceClass.NOISE)


async def test_reattachment_and_healthy_presence_are_noise():
    assert _classify(EventType.ATTACHMENT_REGISTERED) is RelevanceClass.NOISE
    assert _classify(EventType.PRESENCE_CHANGED, {"liveness": "live_poll"}) is (
        RelevanceClass.NOISE
    )


async def test_a_peer_going_quiet_is_news_even_though_presence_is_usually_noise():
    """Suppressing the whole event type would throw away every peer disconnect,
    which is the transition a supervisor most needs."""
    for liveness in ("idle", "stale", "disconnected"):
        assert _classify(EventType.PRESENCE_CHANGED, {"liveness": liveness}) is (
            RelevanceClass.JUDGEMENT
        ), liveness


async def test_a_seat_is_not_woken_by_its_own_message():
    mine = "part_me"
    assert (
        _classify(
            EventType.MESSAGE_POSTED,
            {"body": "hello"},
            actor_participant_id=mine,
            viewer_participant_id=mine,
        )
        is RelevanceClass.NOISE
    )
    assert (
        _classify(
            EventType.MESSAGE_POSTED,
            {"body": "hello"},
            actor_participant_id="part_someone_else",
            viewer_participant_id=mine,
        )
        is RelevanceClass.JUDGEMENT
    )


async def test_own_checkpoint_still_wakes_because_it_came_from_the_companion():
    """Two runtimes share one seat. A checkpoint attributed to me was written by my
    companion, and its trouble report is news to the supervisor half."""
    mine = "part_me"
    assert (
        _classify(
            EventType.TASK_CHECKPOINTED,
            {"summary": "the gate failed"},
            actor_participant_id=mine,
            viewer_participant_id=mine,
        )
        is RelevanceClass.JUDGEMENT
    )


# ---------------------------------------------------------------------------
# People chatting must not bill the agents in the room
# ---------------------------------------------------------------------------

HUMAN = PrincipalKind.HUMAN
AGENT = PrincipalKind.AGENT


async def test_two_people_chatting_do_not_wake_an_agent():
    """The feature: humans get chat speed, agents get silence. The message is still
    delivered and still in the log — only the wake is suppressed."""
    assert (
        _classify(
            EventType.MESSAGE_POSTED,
            {"body": "lunch?", "to_participant_id": None},
            actor_participant_id="par_human",
            viewer_participant_id="par_agent",
            actor_kind=HUMAN,
            viewer_kind=AGENT,
        )
        is RelevanceClass.NOISE
    )


async def test_a_person_addressing_this_agent_still_wakes_it():
    """The expensive mistake would be silencing an instruction, so direction overrides."""
    assert (
        _classify(
            EventType.MESSAGE_POSTED,
            {"body": "take the migration", "to_participant_id": "par_agent"},
            actor_participant_id="par_human",
            viewer_participant_id="par_agent",
            actor_kind=HUMAN,
            viewer_kind=AGENT,
        )
        is RelevanceClass.JUDGEMENT
    )


async def test_a_person_still_hears_another_person():
    """Suppression is for agent readers only. A human on the channel is chatting."""
    assert (
        _classify(
            EventType.MESSAGE_POSTED,
            {"body": "lunch?"},
            actor_participant_id="par_human_a",
            viewer_participant_id="par_human_b",
            actor_kind=HUMAN,
            viewer_kind=HUMAN,
        )
        is RelevanceClass.JUDGEMENT
    )


async def test_an_agent_talking_to_an_agent_still_wakes():
    """Agents coordinating is the product. Only human small talk goes quiet."""
    assert (
        _classify(
            EventType.MESSAGE_POSTED,
            {"body": "I am blocked on the schema"},
            actor_participant_id="par_agent_a",
            viewer_participant_id="par_agent_b",
            actor_kind=AGENT,
            viewer_kind=AGENT,
        )
        is RelevanceClass.JUDGEMENT
    )


async def test_a_persons_directive_and_question_are_untouched_by_this_rule():
    """The channels a human uses when it needs an agent to act must stay loud, or the
    rule would silence control rather than chatter."""
    for kind in (EventType.DIRECTIVE_ISSUED, EventType.QUESTION_ASKED, EventType.TASK_STEERED):
        assert (
            _classify(
                kind,
                {},
                actor_participant_id="par_human",
                viewer_participant_id="par_agent",
                actor_kind=HUMAN,
                viewer_kind=AGENT,
            )
            is RelevanceClass.JUDGEMENT
        ), kind


async def test_unknown_kinds_keep_the_old_behaviour():
    """Callers that pass no kinds - the standalone watcher among them - must not have
    messages silently go quiet underneath them."""
    assert (
        _classify(
            EventType.MESSAGE_POSTED,
            {"body": "hello"},
            actor_participant_id="par_someone",
            viewer_participant_id="par_me",
        )
        is RelevanceClass.JUDGEMENT
    )


# ---------------------------------------------------------------------------
# The contested case: progress vs. trouble
# ---------------------------------------------------------------------------


async def test_a_checkpoint_is_routine_unless_it_reports_trouble():
    assert _classify(EventType.TASK_CHECKPOINTED, {"summary": "ported 12 of 40"}) is (
        RelevanceClass.ROUTINE
    )
    assert _classify(EventType.TASK_CHECKPOINTED, {"summary": "the suite is red"}) is (
        RelevanceClass.JUDGEMENT
    )


async def test_trouble_is_read_structurally_before_it_is_read_lexically():
    assert relevance.reports_trouble({"ok": False, "summary": "all good"})
    assert not relevance.reports_trouble({"ok": True, "summary": "all good"})


async def test_both_contraction_and_spelled_out_forms_count_as_trouble():
    """`couldn't` was covered and `could not` was not, so 'gave up, could not reach
    the room' classified as routine progress."""
    for phrase in ("couldn't reach it", "could not reach it", "gave up"):
        assert relevance.reports_trouble({"summary": phrase}), phrase


async def test_direction_addressed_to_this_seat_wakes_it_and_a_peers_does_not():
    """The coordination hierarchy's addressed events (D-089), on the `message.posted` shape.
    A room-wide allocation waking every agent in the room is the cost this channel exists to
    avoid; the same event naming this seat changes what it is responsible for right now."""
    for kind, field in relevance.ADDRESSED_JUDGEMENT_FIELDS.items():
        assert relevance.wakes(
            event_type=kind, payload={field: "me"}, viewer_participant_id="me"
        ), kind
        assert not relevance.wakes(
            event_type=kind, payload={field: "someone-else"}, viewer_participant_id="me"
        ), kind
        # Rendered rather than silenced: somebody else's direction changing is news worth a
        # line, and this set is deliberately not in the unconditional JUDGEMENT_TYPES.
        assert (
            relevance.classify(
                event_type=kind, payload={field: "someone-else"}, viewer_participant_id="me"
            )
            is relevance.RelevanceClass.ROUTINE
        ), kind


async def test_a_reader_that_cannot_say_who_it_is_renders_rather_than_waking():
    for kind, field in relevance.ADDRESSED_JUDGEMENT_FIELDS.items():
        assert not relevance.wakes(event_type=kind, payload={field: "me"}), kind


async def test_capacity_and_declared_worker_state_are_explicit_noise():
    """They churn like presence. Explicit rather than defaulted, because the default is
    ROUTINE and these would render a line per capacity report and per declared state change."""
    for kind in relevance.HIERARCHY_NOISE_TYPES:
        assert (
            relevance.classify(event_type=kind, payload={"participant_id": "me"})
            is relevance.RelevanceClass.NOISE
        ), kind


async def test_a_finished_worker_wakes_only_when_it_did_not_land():
    """Same split as a checkpoint: one that finished cleanly is progress, and one that gave up
    is the most important thing its supervisor can be told."""
    clean = {"state": "completed", "summary": "ported the reducers", "result_reference": "ckp_1"}
    assert not relevance.wakes(event_type="worker.finished", payload=clean)
    assert relevance.wakes(event_type="worker.finished", payload={"state": "failed", "summary": ""})


async def test_a_terminal_failure_state_is_read_structurally_not_lexically():
    """`failed` and `rejected` were caught only by accident — `state` is in OUTCOME_FIELDS and
    TROUBLE matches `fail\\w*`. `cancelled` was matched by nothing at all, so renaming a state
    or adding one could silently stop a wake. The states are named now."""
    assert relevance.reports_trouble({"state": "cancelled"})
    assert relevance.reports_trouble({"state": "abandoned"})
    assert not relevance.reports_trouble({"state": "completed"})
    # And the structural read comes first: no prose is needed for it to fire.
    assert relevance.reports_trouble({"state": "failed"})


async def test_the_long_poll_states_the_class_so_a_poller_stops_deriving_it(make_room, join):
    """The companion polls `GET /events` rather than holding the wake socket, so without this
    it had no way to consume the room's judgement and grew its own table instead."""
    from app.core import messages
    from app.domain.commands import PostMessageCommand
    from app.domain.disclosure import Disclosure

    fixture = await make_room()
    reader = await join(fixture, display_name="Bea")
    # Directed, deliberately. An *undirected* remark from a human identity is NOISE under
    # a309cfb, and the room owner here is one — so an undirected message would have proved the
    # field is populated while quietly asserting the wrong value.
    await messages.post(
        participant=fixture.owner,
        command=PostMessageCommand(
            body="please look at this",
            disclosure=Disclosure(to_participant_id=reader.participant.id),
        ),
    )

    from app.api import routes as http

    # Every argument explicit: called as a plain coroutine, FastAPI's `Query` defaults are
    # unresolved `Query` objects rather than ints.
    page = await http.get_events(
        room_id=fixture.room.id,
        participant=reader.participant,
        since_seq=0,
        limit=200,
        wait_seconds=0,
    )
    classes = {e["type"]: e["relevance"] for e in page["events"]}
    assert classes["message.posted"] == "judgement"
    # Every event carries one, so a reader never has to guess which are missing.
    assert all("relevance" in e for e in page["events"])


async def test_an_unknown_event_type_renders_rather_than_disappearing():
    """Everything unlisted is routine on purpose: a new event type must show up in a
    feed rather than being silently dropped from one."""
    assert _classify("some.type_added_next_quarter") is RelevanceClass.ROUTINE


# ---------------------------------------------------------------------------
# Drift protection
# ---------------------------------------------------------------------------


def _load_watcher_module():
    """Import the standalone script by path.

    Registered in `sys.modules` before `exec_module`, because its `@dataclass`
    declarations under `from __future__ import annotations` resolve their annotations
    through `sys.modules[cls.__module__]` and get `None` if the module is not there
    yet.
    """
    name = "_room_watcher_under_test"
    if name in sys.modules:
        return sys.modules[name]
    path = Path(__file__).resolve().parents[2] / "scripts" / "room_watcher.py"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        del sys.modules[name]
        raise
    return module


async def test_the_standalone_watcher_agrees_with_the_domain_on_what_wakes():
    """`scripts/room_watcher.py` is stdlib-only by design and cannot import this
    package, so the two lists are duplicated. Duplication that must agree is pinned
    by a test rather than trusted: this fails the moment one side learns about an
    event type and the other does not."""
    watcher = _load_watcher_module()

    domain_types = {t.value for t in relevance.JUDGEMENT_TYPES}
    # The watcher folds the content-judged types into its `classify`, not its set.
    watcher_types = set(watcher.JUDGEMENT_TYPES)

    assert watcher_types - domain_types == set(), (
        f"the watcher wakes on types the room does not: {watcher_types - domain_types}"
    )
    assert domain_types - watcher_types == set(), (
        f"the room wakes on types the watcher does not: {domain_types - watcher_types}"
    )
    assert set(watcher.PRESENCE_WORTH_WAKING) == set(relevance.PRESENCE_WORTH_WAKING)


async def test_the_two_classifiers_agree_on_every_wake_decision():
    """Same inputs, same answer to *does this cost a turn* — including the checkpoint
    content split, the rule most likely to be changed on one side only.

    Deliberately compares the wake decision rather than the three-way class. The
    routine/noise boundary is allowed to differ because the two readers do different
    things with a non-waking event: the watcher renders it into `ROOM.md`, where
    narration is wanted and free, while the wake channel renders nothing at all and so
    has no use for the distinction. Pinning the full class here would force one
    reader's display choice onto the other.
    """
    cases = [
        (EventType.TASK_PROPOSED.value, {}),
        (EventType.ACTIVITY_NOTED.value, {"summary": "narrating"}),
        (EventType.TASK_CHECKPOINTED.value, {"summary": "ported 12 of 40"}),
        (EventType.TASK_CHECKPOINTED.value, {"summary": "the suite is red"}),
        (EventType.TASK_COMPLETED.value, {"result": "shipped"}),
        (EventType.TASK_COMPLETED.value, {"result": "gave up, could not reach it"}),
        (EventType.PRESENCE_CHANGED.value, {"liveness": "live_push"}),
        (EventType.PRESENCE_CHANGED.value, {"liveness": "disconnected"}),
        (EventType.ATTACHMENT_REGISTERED.value, {}),
        (EventType.WORK_DECLARED.value, {}),
    ]
    watcher = _load_watcher_module()
    for kind, payload in cases:
        watcher_wakes = watcher.classify({"type": kind, "payload": payload}) == "judgement"
        domain_wakes = relevance.wakes(event_type=kind, payload=payload)
        assert watcher_wakes == domain_wakes, (
            f"{kind} {payload}: watcher wakes={watcher_wakes} domain wakes={domain_wakes}"
        )


# ---------------------------------------------------------------------------
# The transport contract
# ---------------------------------------------------------------------------


async def test_a_wake_subscriber_is_offered_a_cursor_not_a_board():
    """A full snapshot would be the most expensive frame a wake channel ever
    received, and it would arrive before anything had happened."""
    assert ControlFrame.READY.value == "ready"
    assert ControlFrame.READY is not ControlFrame.SNAPSHOT


# ---------------------------------------------------------------------------
# The endpoint, driven through its own socket contract
# ---------------------------------------------------------------------------


def _fast_keepalive(monkeypatch, seconds: float = 0.05) -> None:
    """Shorten the keepalive so its absence in filtered mode is evidence, not luck.

    `Settings` is a frozen dataclass, so the module reference is replaced rather than
    the field mutated — `dataclasses.replace` re-runs the one validation it carries
    (the room TTL bound), which this does not touch.
    """
    monkeypatch.setattr(
        routes, "settings", dataclasses.replace(settings, sse_keepalive_seconds=seconds)
    )


class _FakeSocket:
    """Enough WebSocket to drive `websocket_stream` and record what it sent."""

    def __init__(self, **params: str) -> None:
        self.query_params = params
        self.frames: list[dict] = []
        self.closed_with: int | None = None
        self.accepted = False

    async def accept(self) -> None:
        self.accepted = True

    async def send_json(self, data: dict) -> None:
        self.frames.append(data)

    async def close(self, code: int = 1000) -> None:
        self.closed_with = code

    def kinds(self) -> list[str]:
        """Frame names, with each event frame named by its event type."""
        return [
            f["event"]["type"] if f.get("frame") == "event" else str(f.get("frame"))
            for f in self.frames
        ]


async def _drive(socket: _FakeSocket, room_id: str, *, seconds: float = 0.6) -> None:
    """Run the endpoint until it goes quiet, then cancel it.

    A wake subscriber with nothing to say produces no frames at all, so there is no
    natural end to wait for — that silence is the property under test.
    """
    with contextlib.suppress(asyncio.TimeoutError, asyncio.CancelledError):
        await asyncio.wait_for(websocket_stream(socket, room_id), timeout=seconds)


async def _append(room_id: str, participant, type_, payload: dict) -> None:
    async with db.transaction() as tx:
        await eventlog.append(
            tx,
            room_id=room_id,
            type_=type_,
            actor=actor_for(participant),
            payload=payload,
        )


async def test_a_wake_subscriber_hears_the_proposal_and_nothing_else(make_room, join, monkeypatch):
    """The scenario the channel exists for: a room full of narration, one task
    proposed, and an idle agent that must be woken exactly once."""
    # Keepalives every 50ms, so their absence below is evidence rather than luck.
    _fast_keepalive(monkeypatch)
    room = await make_room()
    author = await join(room, display_name="Peer")
    reader = await join(room, display_name="Agent")
    start = await eventlog.current_seq(room.room.id)

    for index in range(8):
        await _append(
            room.room.id,
            author.participant,
            EventType.ACTIVITY_NOTED,
            {"phase": "working", "summary": f"still going {index}"},
        )
    await _append(
        room.room.id, author.participant, EventType.TASK_PROPOSED, {"title": "Do the thing"}
    )

    ticket = await stream_tickets.issue(reader.participant)
    socket = _FakeSocket(ticket=ticket.token, since_seq=str(start), classes="judgement")
    await _drive(socket, room.room.id)

    assert socket.accepted
    assert socket.kinds() == ["task.proposed"], socket.kinds()


async def test_the_same_socket_unfiltered_carries_the_narration_and_keepalives(
    make_room, join, monkeypatch
):
    """The control. Default behavior is unchanged, which is what makes the filtered
    mode's silence meaningful rather than a broken subscription."""
    _fast_keepalive(monkeypatch)
    room = await make_room()
    author = await join(room, display_name="Peer")
    reader = await join(room, display_name="Browser")
    start = await eventlog.current_seq(room.room.id)

    for index in range(3):
        await _append(
            room.room.id,
            author.participant,
            EventType.ACTIVITY_NOTED,
            {"phase": "working", "summary": f"still going {index}"},
        )

    ticket = await stream_tickets.issue(reader.participant)
    socket = _FakeSocket(ticket=ticket.token, since_seq=str(start))
    await _drive(socket, room.room.id)

    kinds = socket.kinds()
    assert kinds.count("activity.noted") == 3, kinds
    assert ControlFrame.KEEPALIVE.value in kinds, kinds


async def test_a_wake_subscriber_opening_fresh_gets_a_cursor_not_the_board(
    make_room, join, monkeypatch
):
    """`since_seq=0` means "I have no history" — for a browser that earns a snapshot
    of the whole board, and for a wake channel it must not."""
    _fast_keepalive(monkeypatch)
    room = await make_room()
    reader = await join(room, display_name="Agent")

    ticket = await stream_tickets.issue(reader.participant)
    socket = _FakeSocket(ticket=ticket.token, since_seq="0", classes="judgement")
    await _drive(socket, room.room.id, seconds=0.3)

    assert socket.kinds() == [ControlFrame.READY.value], socket.kinds()
    ready = socket.frames[0]
    assert ready["data"]["cursor"] == await eventlog.current_seq(room.room.id)
    assert ControlFrame.SNAPSHOT.value not in socket.kinds()


async def test_an_unknown_class_is_refused_rather_than_silently_widened(make_room, join):
    """Failing open would hand a wake channel the full firehose, which its host
    rate-limits — so the subscription would look alive and then be dropped."""
    room = await make_room()
    reader = await join(room, display_name="Agent")

    ticket = await stream_tickets.issue(reader.participant)
    socket = _FakeSocket(ticket=ticket.token, classes="everything")
    await websocket_stream(socket, room.room.id)

    assert socket.closed_with == 4401
    assert not socket.accepted
    assert socket.frames == []


# ---------------------------------------------------------------------------
# The client must fail closed against a server that ignores the filter
# ---------------------------------------------------------------------------


def _load_wake_channel():
    name = "_wake_channel_under_test"
    if name in sys.modules:
        return sys.modules[name]
    path = Path(__file__).resolve().parents[2] / "scripts" / "wake_channel.py"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        del sys.modules[name]
        raise
    return module


async def test_a_snapshot_on_a_filtered_subscription_is_refused_not_streamed():
    """Found against the deployed instance, which predates `classes` and dropped it:
    three narration notes became three wake-ups. The server-side guard rejects an
    unknown *value* and cannot help against a server that never learned the parameter,
    so the client must confirm the filter rather than assume it."""
    wc = _load_wake_channel()
    with pytest.raises(wc.UnfilteredServer, match="full snapshot"):
        wc.handle_frame({"frame": "snapshot", "data": {"snapshot_seq": 6}}, cursor=0)


async def test_a_keepalive_on_a_filtered_subscription_is_refused():
    """The tell when resuming from a cursor, where no opening frame is sent at all —
    so a snapshot check alone would miss an unfiltered resume."""
    wc = _load_wake_channel()
    with pytest.raises(wc.UnfilteredServer, match="keepalive"):
        wc.handle_frame({"frame": "keepalive"}, cursor=9)


async def test_the_filtered_path_still_wakes_and_advances():
    wc = _load_wake_channel()
    cursor, line = wc.handle_frame({"frame": "ready", "data": {"cursor": 6}}, cursor=0)
    assert cursor == 6 and line is None

    cursor, line = wc.handle_frame(
        {
            "frame": "event",
            "event": {
                "seq": 11,
                "type": "task.proposed",
                "actor": {"display_name": "Joiner"},
                "payload": {"title": "Migrate the session store"},
            },
        },
        cursor=cursor,
    )
    assert cursor == 11
    assert line == "[11] task.proposed | Joiner | Migrate the session store"


async def test_a_resume_gap_wakes_because_the_cursor_can_no_longer_be_trusted():
    wc = _load_wake_channel()
    cursor, line = wc.handle_frame({"frame": "resume_gap", "data": {}}, cursor=40)
    assert cursor == 0
    assert line is not None and "resume_gap" in line


async def test_an_unknown_frame_is_logged_rather_than_woken_or_dropped_silently(capsys):
    wc = _load_wake_channel()
    cursor, line = wc.handle_frame({"frame": "something_new"}, cursor=7)
    assert (cursor, line) == (7, None)
    assert "unrecognised frame" in capsys.readouterr().err


async def test_a_person_chatting_reaches_the_socket_but_not_the_wake_channel(
    make_room, join, monkeypatch
):
    """End to end through the endpoint: a person talks, an agent's wake channel stays
    silent, and the same words arrive immediately on the unfiltered socket a person uses.

    Both subscriptions read the same log over the same code path, so this pins the one
    thing worth pinning - that the difference is who is reading, not what was delivered.
    """
    _fast_keepalive(monkeypatch)
    room = await make_room()
    person = await join(room, display_name="Alan", kind=PrincipalKind.HUMAN)
    agent = await join(room, display_name="Worker")
    start = await eventlog.current_seq(room.room.id)

    await messages.post(
        participant=person.participant,
        command=PostMessageCommand(body="anyone want lunch"),
    )

    agent_ticket = await stream_tickets.issue(agent.participant)
    agent_socket = _FakeSocket(ticket=agent_ticket.token, since_seq=str(start), classes="judgement")
    await _drive(agent_socket, room.room.id)
    assert agent_socket.kinds() == [], agent_socket.kinds()

    human_ticket = await stream_tickets.issue(person.participant)
    human_socket = _FakeSocket(ticket=human_ticket.token, since_seq=str(start))
    await _drive(human_socket, room.room.id)
    assert "message.posted" in human_socket.kinds(), human_socket.kinds()
    assert [
        f["event"]["payload"]["body"]
        for f in human_socket.frames
        if f.get("frame") == "event" and f["event"]["type"] == "message.posted"
    ] == ["anyone want lunch"]


async def test_a_person_directing_the_agent_does_reach_its_wake_channel(
    make_room, join, monkeypatch
):
    """The same person, the same socket, one field different."""
    _fast_keepalive(monkeypatch)
    room = await make_room()
    person = await join(room, display_name="Alan", kind=PrincipalKind.HUMAN)
    agent = await join(room, display_name="Worker")
    start = await eventlog.current_seq(room.room.id)

    await messages.post(
        participant=person.participant,
        command=PostMessageCommand(
            body="take the migration please",
            disclosure=Disclosure(to_participant_id=agent.participant.id),
        ),
    )

    ticket = await stream_tickets.issue(agent.participant)
    socket = _FakeSocket(ticket=ticket.token, since_seq=str(start), classes="judgement")
    await _drive(socket, room.room.id)

    assert socket.kinds() == ["message.posted"], socket.kinds()


# ---------------------------------------------------------------------------
# The second axis: show a person, without making a model think (D-091)
# ---------------------------------------------------------------------------


async def test_relayed_human_speech_is_shown_to_a_person_without_waking_a_model():
    """The cell that had no home. Suppressing the wake for chat was right and made chat
    undeliverable: this socket is the only push a resident process holds, so "not worth a
    turn" silently meant "the person it was for never receives it"."""
    chat = {"body": "anyone wanna get lunch?", "speaking_for": "human"}
    assert not relevance.wakes(
        event_type="message.posted",
        payload=chat,
        actor_participant_id="them",
        viewer_participant_id="me",
        actor_kind=PrincipalKind.AGENT,
        viewer_kind=PrincipalKind.AGENT,
    )
    assert relevance.shows_to_human(
        event_type="message.posted",
        payload=chat,
        actor_participant_id="them",
        viewer_participant_id="me",
        actor_kind=PrincipalKind.AGENT,
        viewer_kind=PrincipalKind.AGENT,
    )


async def test_a_human_identity_talking_is_shown_too():
    """Both paths to "a person said this" answer the same question, on this axis as well."""
    assert relevance.shows_to_human(
        event_type="message.posted",
        payload={"body": "lunch?"},
        actor_participant_id="them",
        viewer_participant_id="me",
        actor_kind=PrincipalKind.HUMAN,
        viewer_kind=PrincipalKind.AGENT,
    )


async def test_your_own_relay_is_not_read_back_to_you():
    """The sender already has the receipt (D-090). Echoing it would make every remark arrive
    twice in the window the person typed it in."""
    assert not relevance.shows_to_human(
        event_type="message.posted",
        payload={"body": "lunch?", "speaking_for": "human"},
        actor_participant_id="me",
        viewer_participant_id="me",
        actor_kind=PrincipalKind.AGENT,
        viewer_kind=PrincipalKind.AGENT,
    )


async def test_anything_worth_a_decision_is_also_worth_showing():
    """The axes are orthogonal, not opposed. Nothing that wakes a model should be hidden from
    the person it concerns."""
    for kind in ("task.proposed", "directive.issued", "conflict.detected", "participant.left"):
        assert relevance.shows_to_human(event_type=kind, payload={}), kind


async def test_churn_is_invisible_on_both_axes():
    """The narrowness is the design. Widening this to "everything a browser renders" is what
    `classes=all` is already for."""
    for kind, payload in (
        ("activity.noted", {"summary": "running the tests"}),
        ("presence.changed", {"liveness": "live_poll"}),
        ("presence.attachment_registered", {}),
        ("supervisor.capacity_changed", {"participant_id": "them"}),
        ("worker.state_changed", {"worker_id": "w"}),
    ):
        assert not relevance.shows_to_human(event_type=kind, payload=payload), kind


async def test_the_socket_accepts_the_second_axis_and_still_refuses_nonsense(make_room, join):
    """Additive: `classes=judgement` keeps working, so a client that predates this is
    unaffected. An unknown value is still refused rather than silently widened."""
    from app.api import routes as http

    assert "judgement,human_visible" in http.websocket_stream.__doc__
    src = Path(http.__file__).read_text(encoding="utf-8")
    assert 'classes not in ("all", "judgement", "judgement,human_visible")' in src


async def test_the_wake_channel_asks_for_both_and_marks_chat_as_the_person():
    """A host reading a line must be able to tell "somebody spoke to you" from "a decision is
    needed" — and a chat line has to read as the person, not the seat that carried it."""
    watcher = _load_wake_channel()
    url = watcher.socket_url(
        "https://example.test", "room_x", ticket="t", since_seq=0, connection_id=""
    )
    assert "classes=judgement%2Chuman_visible" in url

    line = watcher.describe(
        {
            "seq": 41,
            "type": "message.posted",
            "actor": {"display_name": "Claude Code"},
            "payload": {
                "body": "anyone wanna get lunch?",
                "speaking_for": "human",
                "speaking_as": "Alan",
            },
        }
    )
    assert line == "[41] [chat] Alan | anyone wanna get lunch?"
    # And a coordination event keeps its existing shape.
    work = watcher.describe(
        {"seq": 42, "type": "task.proposed", "actor": {"display_name": "Bea"}, "payload": {}}
    )
    assert work == "[42] task.proposed | Bea"


async def test_a_server_restart_reconnects_the_relay_instead_of_killing_it():
    """The bug this file existed to prevent, and the one it had (D-091).

    `ConnectionClosed` derives from `WebSocketException`, so it matched none of the reconnect
    loop's handlers and ended the process. Every deploy permanently killed every relay
    watching — and silently, in the one direction that matters: the relay had already proved
    it worked, so its silence afterwards read as a quiet room rather than a dead relay.
    Nobody goes looking at a quiet room.
    """
    watcher = _load_wake_channel()

    # 1012 service restart is what a deploy sends. It, and every other "come back" code, must
    # be treated as transient rather than as the room refusing this subscriber.
    assert 1012 in watcher.TRANSIENT_CLOSE_CODES
    for code in (1001, 1006, 1013):
        assert code in watcher.TRANSIENT_CLOSE_CODES, code
    # A deliberate close is not transient: reconnecting into a shut door forever is a busy
    # loop, so that path still escalates its backoff.
    assert 1000 not in watcher.TRANSIENT_CLOSE_CODES

    # The handler exists and names the real exception type, rather than relying on OSError —
    # which is what let this through.
    source = Path(watcher.__file__).read_text(encoding="utf-8")
    assert "except _ConnectionClosed" in source
    assert watcher._ConnectionClosed.__name__ == "ConnectionClosed"

    # And the close code is read defensively, because `websockets` has moved it between
    # attributes across versions and a wrong guess stops the relay reconnecting.
    class _Socket:
        close_code = 1012

    assert watcher._close_code(_Socket()) == 1012
    assert watcher._close_code(object()) is None
