"""The local room pulse stays truthful without spending a model turn."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

import room_statusline as statusline


def test_quiet_room_pulses_from_poll_timestamp_not_room_sequence():
    first = statusline.render({"at": 100.0, "poll_interval_s": 10, "seq": 41}, now=101.0)
    second = statusline.render({"at": 110.0, "poll_interval_s": 10, "seq": 41}, now=111.0)

    assert first.startswith("♥ room WATCHING")
    assert second.startswith("♡ room WATCHING")
    assert "seq 41" in first and "seq 41" in second


def test_missing_mode_defaults_to_watching_and_shows_relay_fields():
    line = statusline.render(
        {
            "at": 100.0,
            "poll_interval_s": 10,
            "cursor": 512,
            "workers": 2,
            "pending": 3,
            "delivery": "rendered",
        },
        now=104.9,
    )

    assert "room WATCHING" in line
    assert "cursor 512" in line
    assert "workers 2" in line
    assert "pending 3" in line
    assert "delivery rendered" in line
    assert "poll age 4s" in line


def test_draining_is_explicit_while_the_poller_is_fresh():
    line = statusline.render(
        {"at": 100.0, "poll_interval_s": 8, "mode": "DRAINING", "pending": 1},
        now=115.9,
    )

    assert "room DRAINING" in line
    assert "pending 1" in line
    assert "stale" not in line


def test_watcher_is_stale_just_after_two_declared_poll_periods():
    state = {"at": 100.0, "poll_interval_s": 8, "mode": "WATCHING"}

    assert "stale" not in statusline.render(state, now=116.0)
    stale = statusline.render(state, now=116.001)
    assert "room WATCHING stale" in stale
    assert "limit 16s" in stale


def test_stopped_is_terminal_not_misreported_as_a_dead_poller():
    line = statusline.render(
        {
            "at": 100.0,
            "poll_interval_s": 8,
            "mode": "STOPPED",
            "cursor": 519,
            "pending": 0,
            "delivery": {"acked": 519, "state": "flushed"},
        },
        now=10_000.0,
    )

    assert line.startswith("■ room STOPPED")
    assert "cursor 519" in line
    assert "pending 0" in line
    assert "delivery acked=519,state=flushed" in line
    assert "stale" not in line


def test_error_reports_mode_and_poll_age_without_hiding_the_failure():
    line = statusline.render(
        {"at": 100.0, "poll_interval_s": 10, "mode": "DRAINING", "error": "Timeout"},
        now=105.0,
    )

    assert line == "○ room DRAINING unreachable (Timeout) · poll age 5s"
