"""Test fixtures: every test gets its own throwaway SQLite file and a clean hub."""

from __future__ import annotations

from pathlib import Path

import pytest_asyncio

from app.db import database as db
from app.events import hub
from app.services import guardrails


@pytest_asyncio.fixture
async def fresh_db(tmp_path: Path):
    """Point the process at a clean database and reset all in-memory state.

    The event hub and guardrail timers are module-level singletons, so they are
    cleared rather than replaced — otherwise modules that imported them by name
    would keep pointing at the old object.
    """
    original = db.get_database_path()
    db.set_database_path(tmp_path / "test.db")
    await db.init_db()

    guardrails.reset()
    hub.clear()

    yield

    hub.clear()
    guardrails.reset()
    db.set_database_path(original)
