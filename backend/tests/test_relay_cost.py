"""The supervisor relay must be cheap, and cheap is a testable property.

`docs/PRODUCT.md` §9: we do not pay for inference, our users do. `scripts/room_watcher.py`
writes to a stdout that a host turns into one model wake-up per line, so every line it
prints spends someone else's money. These tests pin the four claims that make that
affordable, over a synthetic event stream rather than a live room:

1. an idle room prints nothing while still refreshing the files,
2. a directive is exactly one line,
3. five routine events are five lines in the file and zero lines on stdout,
4. two judgement events arriving in one poll are one line, not two.

And the failure mode that matters more than cost: nothing worth showing is dropped. A
relay that suppresses what the supervisor needed has failed at its only job, so the
suppression tests are written from the "did it still render?" side as well.

The network loop in `main()` is not exercised here — everything asserted below is the
deterministic half, which is precisely the half §9 requires to have no model in it.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# The watcher is an operator script, outside the backend package. Same idiom as
# test_worker_loop_e2e.py: reach it by path rather than making scripts/ importable.
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

import room_watcher as watcher

from app.domain.events import EventType


def event(
    kind: str,
    seq: int = 1,
    *,
    actor: str = "p_peer",
    name: str = "Codex",
    **payload: object,
) -> dict[str, object]:
    """One event shaped the way the room's `/events` feed returns it."""
    return {
        "seq": seq,
        "type": kind,
        "ts": "2026-08-16T09:00:00Z",
        "actor": {"participant_id": actor, "display_name": name},
        "payload": payload,
    }


def run(events: list[dict[str, object]], *, me: str = "p_me", emit: bool = True):
    counters = watcher.RelayCounters()
    shown, wake = watcher.relay(events, me=me, counters=counters, emit=emit)
    return shown, wake, counters


# --------------------------------------------------------------------------------------
# The four claims from the task, in order.
# --------------------------------------------------------------------------------------


@pytest.fixture
def content_visible(monkeypatch):
    """Render what people said, for tests about cost rather than disclosure.

    The watcher redacts free text by default (D-064), so a test that proves batching by
    looking for the words needs to opt in - otherwise it would be asserting the
    redaction, which has its own tests below, and would stop noticing whether the line
    rendered at all.
    """
    monkeypatch.setattr(watcher, "INCLUDE_CONTENT", True)


def test_an_idle_room_wakes_nobody_and_still_refreshes_the_files():
    """The normal case. A quiet room is not an error state, it is most of the time."""
    shown, wake, counters = run([])

    assert wake is None
    assert shown == []
    assert counters.wakes == 0

    # ...and the file is still written, with a fresh reading and the running cost. The
    # supervisor's window must not go blank just because nothing happened in it.
    state = {"at": 0.0, "seq": 412, "participants": 3, "live": 2, "feed": []}
    state["relay"] = counters.report(now=counters.started_at + 3600)
    page = watcher.as_markdown(state)
    assert "seq 412" in page
    assert "2 live" in page
    assert "model wakes: **0**" in page


def test_a_directive_is_exactly_one_wake(content_visible):
    shown, wake, counters = run(
        [event(EventType.DIRECTIVE_ISSUED.value, 1, body="stop and rerun the gate")]
    )

    assert wake is not None
    assert wake.count("\n") == 0
    assert "stop and rerun the gate" in wake
    assert counters.wakes == 1
    assert counters.judgement_events == 1
    assert len(shown) == 1


def test_five_routine_events_are_five_lines_and_zero_wakes(content_visible):
    """Claims, renewals and status transitions: a template renders all of it."""
    routine = [
        event(EventType.TASK_CLAIMED.value, 1, title="wire the relay"),
        event(EventType.TASK_CLAIM_RENEWED.value, 2),
        event(EventType.TASK_UPDATED.value, 3, status="in_progress"),
        event(EventType.WORK_DECLARED.value, 4, headline="rendering routine events"),
        event(EventType.WORK_ENDED.value, 5),
    ]
    shown, wake, counters = run(routine)

    assert wake is None
    assert counters.wakes == 0
    assert counters.routine_events == 5
    # Rendered, not dropped. This is the half that keeps the relay honest.
    assert len(shown) == 5
    assert "wire the relay" in shown[0]
    assert EventType.TASK_CLAIM_RENEWED.value in shown[1]


def test_two_judgement_events_in_one_poll_are_one_combined_line(content_visible):
    shown, wake, counters = run(
        [
            event(EventType.QUESTION_ASKED.value, 7, body="which base URL?"),
            event(EventType.CONFLICT_DETECTED.value, 8, reason="both claimed tsk_1"),
        ]
    )

    assert wake is not None
    assert wake.count("\n") == 0, "two lines would be two wakes, whatever they contain"
    assert wake.startswith("[2 events] ")
    assert "which base URL?" in wake
    assert "both claimed tsk_1" in wake
    assert counters.wakes == 1
    assert counters.judgement_events == 2
    assert counters.report()["coalesced"] == 1
    assert len(shown) == 2


# --------------------------------------------------------------------------------------
# The contested call: where a checkpoint falls.
# --------------------------------------------------------------------------------------


def test_a_checkpoint_is_routine_until_it_reports_trouble(content_visible):
    """The ruling, pinned: progress renders, trouble wakes.

    A checkpoint every few minutes is the drip that makes a relay expensive; "the gate
    failed" is the single most important thing a supervisor can be told. Both arrive as
    `task.checkpointed`, so the split is by content and not by type.
    """
    progress = event(EventType.TASK_CHECKPOINTED.value, 1, summary="three of four files done")
    trouble = event(EventType.TASK_CHECKPOINTED.value, 2, summary="the gate failed: 2 red")

    assert watcher.classify(progress) == watcher.ROUTINE
    assert watcher.classify(trouble) == watcher.JUDGEMENT

    # Both still render. The quiet one is pull rather than push; it is never lost.
    shown, wake, counters = run([progress, trouble])
    assert len(shown) == 2
    assert wake is not None and "the gate failed" in wake
    assert "three of four files done" in shown[0]
    assert counters.routine_events == 1 and counters.judgement_events == 1


def test_a_completion_is_split_the_same_way():
    ok = event(EventType.TASK_COMPLETED.value, 1, result="merged, gate green")
    bad = event(EventType.TASK_COMPLETED.value, 2, result="gave up, could not reach the room")

    assert watcher.classify(ok) == watcher.ROUTINE
    assert watcher.classify(bad) == watcher.JUDGEMENT


def test_a_structural_failure_flag_needs_no_vocabulary():
    """`ok: false` is unambiguous. The word list is the fallback, not the mechanism."""
    assert watcher.reports_trouble({"ok": False})
    assert watcher.reports_trouble({"succeeded": False})
    assert not watcher.reports_trouble({"summary": "all four steps landed"})


def test_the_loss_this_rule_accepts_is_a_quiet_failure():
    """Stated as a test so it is a known cost rather than a surprise.

    A checkpoint reporting a bad outcome in words `TROUBLE` does not contain does not
    wake anyone. It renders, and waits for the supervisor to look.
    """
    understated = event(
        EventType.TASK_CHECKPOINTED.value, 1, summary="the numbers came back lower than we hoped"
    )
    shown, wake, _ = run([understated])

    assert wake is None, "this is the accepted loss, not a bug"
    assert len(shown) == 1, "but it is still on the page — never dropped"


# --------------------------------------------------------------------------------------
# Suppression, and the limits of it.
# --------------------------------------------------------------------------------------


def test_a_heartbeat_is_noise_but_a_peer_going_away_is_news():
    """Presence is the highest-volume event type and the reason §9 was written.

    Suppressing the whole type was the bug Codex caught: it throws away every peer
    disconnect, which is exactly what a supervisor has to act on.
    """
    reattach = event(EventType.ATTACHMENT_REGISTERED.value, 1)
    still_here = event(EventType.PRESENCE_CHANGED.value, 2, liveness="live_poll")
    gone = event(EventType.PRESENCE_CHANGED.value, 3, liveness="disconnected")

    assert watcher.classify(reattach) == watcher.NOISE
    assert watcher.classify(still_here) == watcher.NOISE
    assert watcher.classify(gone) == watcher.JUDGEMENT

    shown, wake, counters = run([reattach, still_here, gone])
    assert counters.noise_events == 2
    assert len(shown) == 1, "noise does not even take a line in the file"
    assert wake is not None


def test_my_own_message_is_not_read_back_to_me():
    mine = event(EventType.MESSAGE_POSTED.value, 1, actor="p_me", body="on it")
    theirs = event(EventType.MESSAGE_POSTED.value, 2, actor="p_peer", body="please rerun it")

    assert watcher.classify(mine, me="p_me") == watcher.NOISE
    assert watcher.classify(theirs, me="p_me") == watcher.JUDGEMENT
    # A checkpoint from my own seat comes from the *companion* runtime — news to the
    # supervisor even though the room attributes it to one participant.
    own_seat_work = event(EventType.TASK_CHECKPOINTED.value, 3, actor="p_me", summary="step 2 done")
    assert watcher.classify(own_seat_work, me="p_me") == watcher.ROUTINE


def test_an_unknown_event_type_renders_rather_than_disappearing():
    """The default is routine, never noise. A new event type shows up in the file."""
    assert watcher.classify(event("something.we.have.not.shipped.yet", 1)) == watcher.ROUTINE


def test_every_judgement_type_is_a_real_event_type():
    """A typo here would silently downgrade a conflict to routine and nothing would fail."""
    known = {member.value for member in EventType}
    assert watcher.JUDGEMENT_TYPES.issubset(known)


# --------------------------------------------------------------------------------------
# The numbers §9 says a supervisor must be able to report.
# --------------------------------------------------------------------------------------


def test_the_counters_report_wakes_payload_size_and_coalescing():
    counters = watcher.RelayCounters()
    watcher.relay(
        [
            event(EventType.QUESTION_ASKED.value, 1, body="a" * 40),
            event(EventType.TASK_PROPOSED.value, 2, title="b" * 40),
            event(EventType.TASK_CLAIMED.value, 3),
        ],
        me="p_me",
        counters=counters,
    )
    watcher.relay(
        [event(EventType.TASK_BLOCKED.value, 4, reason="no credential")],
        me="p_me",
        counters=counters,
    )

    report = counters.report(now=counters.started_at + 1800)
    assert report["wakes"] == 2
    assert report["wakes_per_hour"] == 4.0  # 2 wakes in half an hour
    assert report["judgement"] == 3
    assert report["routine"] == 1
    assert report["coalesced"] == 1  # three judgement events delivered in two wakes
    assert report["bytes_max"] >= report["bytes_last"] > 0
    assert report["bytes_mean"] > 0


def test_the_cost_is_reported_on_the_page_too():
    counters = watcher.RelayCounters()
    watcher.relay(
        [event(EventType.DIRECTIVE_ISSUED.value, 1, body="rerun it")],
        me="p_me",
        counters=counters,
    )
    page = watcher.as_markdown({"at": 0.0, "seq": 9, "feed": [], "relay": counters.report()})

    assert "## Cost" in page
    assert "model wakes: **1**" in page
    assert "payload per wake:" in page


def test_counters_still_count_when_emit_is_off():
    """`--emit` off must not make the relay look free. Nothing was printed, so nothing
    was spent — but the judgement events still happened and are still reported."""
    shown, wake, counters = run(
        [event(EventType.CONFLICT_DETECTED.value, 1, reason="two claims")], emit=False
    )

    assert wake is None
    assert counters.wakes == 0
    assert counters.judgement_events == 1
    assert len(shown) == 1


def test_a_wake_line_is_ascii_and_single_line():
    """It crosses a pipe into a host that may decode it as anything, and one line is
    one wake — a stray newline in a payload would double the bill."""
    _, wake, _ = run(
        [
            event(EventType.QUESTION_ASKED.value, 1, name="Codex · agent", body="why\nnot?"),
            event(EventType.TASK_STEERED.value, 2, reason="pause"),
        ]
    )

    assert wake is not None
    assert "\n" not in wake
    wake.encode("ascii")  # raises if the middle dot or any other non-ASCII survived


def test_free_text_is_not_written_to_disk_by_default():
    """The default, and the reason the flag exists.

    These files live outside the repository, unencrypted, for as long as nobody deletes
    them, and an ACL audit found the markdown copy readable by every local user and
    writable by any authenticated one. A room's prose does not belong there by accident.
    """
    secret = "the customer's password is hunter2 and the deal closes friday"
    shown, wake, _ = run([event(EventType.MESSAGE_POSTED.value, 1, body=secret)])

    rendered = " ".join(shown) + (wake or "")
    assert "hunter2" not in rendered
    assert "password" not in rendered
    assert f"<body, {len(secret)} chars>" in rendered


def test_redaction_still_says_that_something_was_said_and_how_much():
    """Metadata, not silence. "Someone posted 4000 characters" is a coordination signal
    on its own, and it discloses none of them."""
    shown, _, _ = run([event(EventType.TASK_UPDATED.value, 1, note="x" * 4000)])
    assert "<note, 4000 chars>" in shown[0]
