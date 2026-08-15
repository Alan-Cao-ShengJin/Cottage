"""Adversarial B2B tenant-isolation matrix.

These tests use two unrelated organizations and two rooms.  The attacker always
holds a valid credential for tenant B; the protected records belong to tenant A.
That distinction matters: authentication failures are already covered elsewhere,
whereas B2B isolation fails when a *valid* customer can disclose or mutate another
customer's state by substituting a room or resource id.

The matrix deliberately exercises the HTTP boundary and then verifies the durable
rows.  A rejected response is not sufficient evidence if the forbidden mutation
was committed before the error was rendered.
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx
import pytest
import pytest_asyncio

from app.adapters.mcp import server as mcp_server
from app.core import checkpoints, rooms, store, tasks
from app.core.errors import InvalidCommand, NotFound
from app.db import database as db
from app.domain.commands import (
    ClaimTaskCommand,
    CreateRoomCommand,
    CreateTaskCommand,
    MintCredentialCommand,
)
from app.domain.room import Scope
from app.main import app

pytestmark = pytest.mark.asyncio


@dataclass(frozen=True)
class Tenant:
    org_id: str
    owner_user_id: str
    room_id: str
    owner_participant_id: str
    owner_token: str

    @property
    def auth(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.owner_token}"}


@dataclass(frozen=True)
class TenantPair:
    alpha: Tenant
    beta: Tenant


async def _create_tenant(*, slug: str, label: str) -> Tenant:
    org_id, user_id = await rooms.ensure_org_and_user(
        org_name=f"{label} Corp",
        org_slug=slug,
        email=f"owner@{slug}.test",
        display_name=f"{label} Owner",
    )
    user_row = await db.fetch_one("SELECT * FROM users WHERE id = ?", (user_id,))
    assert user_row is not None
    created = await rooms.create_room(
        user=store.to_user(user_row),
        command=CreateRoomCommand(name=f"{label} private room"),
        creator_display_name=f"{label} Owner",
    )
    return Tenant(
        org_id=org_id,
        owner_user_id=user_id,
        room_id=created.room.id,
        owner_participant_id=created.participant.id,
        owner_token=created.participant_token,
    )


@pytest_asyncio.fixture
async def tenants(fresh_db) -> TenantPair:
    return TenantPair(
        alpha=await _create_tenant(slug="alpha-isolation", label="Alpha"),
        beta=await _create_tenant(slug="beta-isolation", label="Beta"),
    )


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://tenant-isolation.test",
    )


async def _alpha_task(tenants: TenantPair, *, title: str = "ALPHA TASK CANARY"):
    participant = await store.load_participant(tenants.alpha.owner_participant_id)
    return await tasks.create(
        participant=participant,
        command=CreateTaskCommand(
            title=title,
            description="ALPHA TASK BODY CANARY",
            targets=["alpha/private.py"],
        ),
    )


async def _durable_coordination_state(tenants: TenantPair) -> tuple[int, ...]:
    """Everything a refused relationship-injection command could have changed."""
    return (
        int(await db.fetch_value("SELECT COUNT(*) FROM work_declarations") or 0),
        int(await db.fetch_value("SELECT COUNT(*) FROM questions") or 0),
        int(await db.fetch_value("SELECT COUNT(*) FROM directives") or 0),
        int(await db.fetch_value("SELECT COUNT(*) FROM task_checkpoints") or 0),
        int(await db.fetch_value("SELECT COUNT(*) FROM room_events") or 0),
        int(await db.fetch_value("SELECT COUNT(*) FROM command_receipts") or 0),
        int(
            await db.fetch_value(
                "SELECT event_seq FROM rooms WHERE id = ?", (tenants.alpha.room_id,)
            )
        ),
        int(
            await db.fetch_value(
                "SELECT event_seq FROM rooms WHERE id = ?", (tenants.beta.room_id,)
            )
        ),
    )


# ---------------------------------------------------------------------------
# Cross-tenant relationship injection
# ---------------------------------------------------------------------------


async def test_foreign_task_cannot_be_attached_to_work(tenants: TenantPair):
    """A valid B token must not create a B record referencing an A task.

    A plain foreign key only proves that the task exists globally.  The API must
    additionally prove ``work.room_id == task.room_id`` before persisting anything.
    """
    protected = await _alpha_task(tenants)
    before = await _durable_coordination_state(tenants)

    async with _client() as client:
        response = await client.post(
            f"/api/rooms/{tenants.beta.room_id}/work",
            headers=tenants.beta.auth,
            json={
                "headline": "BETA CROSS-TENANT WORK INJECTION",
                "task_id": protected.id,
                "targets": ["beta/ordinary.py"],
                "command_id": "cmd-foreign-work-task",
            },
        )

    injected = await db.fetch_all(
        "SELECT id, room_id, task_id FROM work_declarations WHERE task_id = ?",
        (protected.id,),
    )
    assert response.status_code == 404 and injected == [], (
        "tenant B linked a work declaration to tenant A's task; "
        f"status={response.status_code}, rows={[dict(row) for row in injected]}"
    )
    assert await _durable_coordination_state(tenants) == before


async def test_foreign_task_cannot_be_attached_to_nonblocking_question(tenants: TenantPair):
    """Non-blocking questions still need same-room validation for optional task ids."""
    protected = await _alpha_task(tenants)
    before = await _durable_coordination_state(tenants)

    async with _client() as client:
        response = await client.post(
            f"/api/rooms/{tenants.beta.room_id}/questions",
            headers=tenants.beta.auth,
            json={
                "body": "BETA CROSS-TENANT QUESTION INJECTION",
                "task_id": protected.id,
                "blocking": False,
                "command_id": "cmd-foreign-question-task",
            },
        )

    injected = await db.fetch_all(
        "SELECT id, room_id, task_id FROM questions WHERE task_id = ?",
        (protected.id,),
    )
    assert response.status_code == 404 and injected == [], (
        "tenant B linked a question to tenant A's task; "
        f"status={response.status_code}, rows={[dict(row) for row in injected]}"
    )
    assert await _durable_coordination_state(tenants) == before


async def test_foreign_task_cannot_be_attached_to_input_directive(tenants: TenantPair):
    """Even non-steering input must not make a cross-room task relationship."""
    protected = await _alpha_task(tenants)
    before = await _durable_coordination_state(tenants)

    async with _client() as client:
        response = await client.post(
            f"/api/rooms/{tenants.beta.room_id}/directives",
            headers=tenants.beta.auth,
            json={
                "target_participant_id": tenants.beta.owner_participant_id,
                "action": "input",
                "task_id": protected.id,
                "reason": "BETA CROSS-TENANT INPUT INJECTION",
                "command_id": "cmd-foreign-input-task",
            },
        )

    assert response.status_code == 404
    assert response.json()["error"] == "not_found"
    assert await _durable_coordination_state(tenants) == before


@pytest.mark.parametrize("surface", ["work", "question", "checkpoint", "directive", "claim"])
async def test_mcp_and_http_share_the_same_foreign_task_refusal(tenants: TenantPair, surface: str):
    """MCP is an adapter over the same room-scoped core command boundary."""
    protected = await _alpha_task(tenants)
    before = await _durable_coordination_state(tenants)

    if surface == "work":
        result = await mcp_server.declare_current_work(
            headline="BETA MCP INJECTION",
            task_id=protected.id,
            participant_token=tenants.beta.owner_token,
        )
    elif surface == "question":
        result = await mcp_server.ask_question(
            body="BETA MCP INJECTION",
            task_id=protected.id,
            participant_token=tenants.beta.owner_token,
        )
    elif surface == "checkpoint":
        result = await mcp_server.record_checkpoint(
            task_id=protected.id,
            fence=0,
            summary="BETA MCP INJECTION",
            participant_token=tenants.beta.owner_token,
        )
    elif surface == "directive":
        result = await mcp_server.steer_participant(
            target_participant_id=tenants.beta.owner_participant_id,
            action="input",
            task_id=protected.id,
            reason="BETA MCP INJECTION",
            participant_token=tenants.beta.owner_token,
        )
    else:
        result = await mcp_server.claim_task(
            task_id=protected.id,
            participant_token=tenants.beta.owner_token,
        )

    assert result["ok"] is False
    assert result["error"] == "not_found"
    assert await _durable_coordination_state(tenants) == before


async def test_core_foreign_claim_uses_the_same_not_found_boundary(tenants: TenantPair):
    protected = await _alpha_task(tenants)
    attacker = await store.load_participant(tenants.beta.owner_participant_id)
    before = await _durable_coordination_state(tenants)

    with pytest.raises(NotFound):
        await tasks.claim(
            participant=attacker,
            command=ClaimTaskCommand(command_id="cmd-core-foreign-claim", task_id=protected.id),
        )

    assert await _durable_coordination_state(tenants) == before


# ---------------------------------------------------------------------------
# Room substitution and read disclosure
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("method", "suffix", "payload"),
    [
        pytest.param("GET", "snapshot", None, id="participants_and_snapshot"),
        pytest.param("GET", "hydrate", None, id="hydration"),
        pytest.param("GET", "events?since_seq=0", None, id="events"),
        pytest.param("GET", "credentials", None, id="credentials"),
        pytest.param("GET", "directives", None, id="directives"),
        pytest.param("GET", "questions", None, id="questions"),
        pytest.param("POST", "tasks", {"title": "intrusion"}, id="task_mutation"),
        pytest.param("POST", "work", {"headline": "intrusion"}, id="work_mutation"),
        pytest.param("POST", "messages", {"body": "intrusion"}, id="message_mutation"),
    ],
)
async def test_beta_token_is_rejected_on_every_alpha_room_surface(
    tenants: TenantPair,
    method: str,
    suffix: str,
    payload: dict[str, object] | None,
):
    before = int(
        await db.fetch_value("SELECT event_seq FROM rooms WHERE id = ?", (tenants.alpha.room_id,))
    )
    async with _client() as client:
        response = await client.request(
            method,
            f"/api/rooms/{tenants.alpha.room_id}/{suffix}",
            headers=tenants.beta.auth,
            json=payload,
        )
    after = int(
        await db.fetch_value("SELECT event_seq FROM rooms WHERE id = ?", (tenants.alpha.room_id,))
    )

    assert response.status_code == 403
    assert response.json()["error"] == "forbidden"
    assert after == before, "a rejected cross-room request must append no event"


async def test_beta_snapshot_and_replay_contain_no_alpha_canaries(tenants: TenantPair):
    protected = await _alpha_task(tenants)
    alpha_participant = await store.load_participant(tenants.alpha.owner_participant_id)
    await tasks.create(
        participant=alpha_participant,
        command=CreateTaskCommand(
            title="ALPHA DUPLICATE CANARY",
            description="ALPHA CONFLICT DETAIL CANARY",
            targets=["alpha/conflict.py"],
        ),
    )
    await tasks.create(
        participant=alpha_participant,
        command=CreateTaskCommand(
            title="ALPHA DUPLICATE CANARY",
            description="ALPHA CONFLICT DETAIL CANARY",
            targets=["alpha/conflict.py"],
        ),
    )

    async with _client() as client:
        snapshot = await client.get(
            f"/api/rooms/{tenants.beta.room_id}/snapshot", headers=tenants.beta.auth
        )
        replay = await client.get(
            f"/api/rooms/{tenants.beta.room_id}/events?since_seq=0",
            headers=tenants.beta.auth,
        )

    assert snapshot.status_code == replay.status_code == 200
    rendered = f"{snapshot.text}\n{replay.text}"
    assert tenants.alpha.room_id not in rendered
    assert tenants.alpha.owner_participant_id not in rendered
    assert protected.id not in rendered
    assert "ALPHA" not in rendered


# ---------------------------------------------------------------------------
# Foreign resource-id substitution on a valid tenant-B route
# ---------------------------------------------------------------------------


async def test_foreign_participant_cannot_be_retargeted_or_rescoped(tenants: TenantPair):
    async with _client() as client:
        response = await client.post(
            f"/api/rooms/{tenants.beta.room_id}/participants/role",
            headers=tenants.beta.auth,
            json={
                "target_participant_id": tenants.alpha.owner_participant_id,
                "role": "observer",
                "scopes": ["room.read"],
                "reason": "cross-tenant probe",
            },
        )

    protected = await store.load_participant(tenants.alpha.owner_participant_id)
    assert response.status_code == 404
    assert protected.role.value == "owner"
    assert Scope.ROOM_ADMIN in protected.scopes


@pytest.mark.parametrize(
    ("suffix", "payload"),
    [
        pytest.param("claim", {}, id="claim"),
        pytest.param("update", {"title": "BETA OVERWRITE"}, id="update"),
        pytest.param("cancel", {"reason": "BETA CANCEL"}, id="cancel"),
        pytest.param("complete", {"fence": 0, "result": "BETA RESULT"}, id="complete"),
    ],
)
async def test_foreign_task_id_cannot_be_mutated(
    tenants: TenantPair, suffix: str, payload: dict[str, object]
):
    protected = await _alpha_task(tenants)
    payload = {
        "task_id": protected.id,
        "command_id": f"cmd-foreign-task-{suffix}",
        **payload,
    }
    before = await _durable_coordination_state(tenants)

    async with _client() as client:
        response = await client.post(
            f"/api/rooms/{tenants.beta.room_id}/tasks/{suffix}",
            headers=tenants.beta.auth,
            json=payload,
        )

    unchanged = await store.load_task(protected.id)
    assert response.status_code == 404
    assert unchanged.title == "ALPHA TASK CANARY"
    assert unchanged.status.value == "open"
    assert await _durable_coordination_state(tenants) == before


async def test_foreign_task_read_is_indistinguishable_from_missing(tenants: TenantPair):
    protected = await _alpha_task(tenants)
    async with _client() as client:
        foreign = await client.get(
            f"/api/rooms/{tenants.beta.room_id}/tasks/{protected.id}",
            headers=tenants.beta.auth,
        )
        missing = await client.get(
            f"/api/rooms/{tenants.beta.room_id}/tasks/tsk_missing",
            headers=tenants.beta.auth,
        )

    assert foreign.status_code == missing.status_code == 404
    assert foreign.json()["error"] == missing.json()["error"] == "not_found"


async def test_foreign_checkpoint_list_is_indistinguishable_from_missing(
    tenants: TenantPair,
):
    protected = await _alpha_task(tenants)
    await db.execute(
        """
        INSERT INTO task_checkpoints (
            id, room_id, task_id, participant_id, attachment_id, fence,
            summary, resume_state, seq, created_at
        ) VALUES (?,?,?,?,NULL,?,?,NULL,?,?)
        """,
        (
            "chk_alpha_private",
            tenants.alpha.room_id,
            protected.id,
            tenants.alpha.owner_participant_id,
            0,
            "ALPHA CHECKPOINT CANARY",
            1,
            "2026-08-16T00:00:00+00:00",
        ),
    )

    async with _client() as client:
        foreign = await client.get(
            f"/api/rooms/{tenants.beta.room_id}/tasks/{protected.id}/checkpoints",
            headers=tenants.beta.auth,
        )
        missing = await client.get(
            f"/api/rooms/{tenants.beta.room_id}/tasks/tsk_missing/checkpoints",
            headers=tenants.beta.auth,
        )

    assert foreign.status_code == missing.status_code == 404
    assert foreign.json()["error"] == missing.json()["error"] == "not_found"
    assert "ALPHA CHECKPOINT CANARY" not in foreign.text


async def test_direct_checkpoint_reads_are_room_bound_and_fail_closed(
    tenants: TenantPair,
):
    protected = await _alpha_task(tenants)
    await db.execute(
        """
        INSERT INTO task_checkpoints (
            id, room_id, task_id, participant_id, attachment_id, fence,
            summary, resume_state, seq, created_at
        ) VALUES (?,?,?,?,NULL,?,?,NULL,?,?)
        """,
        (
            "chk_alpha_direct_private",
            tenants.alpha.room_id,
            protected.id,
            tenants.alpha.owner_participant_id,
            0,
            "ALPHA DIRECT CHECKPOINT CANARY",
            1,
            "2026-08-16T00:00:00+00:00",
        ),
    )
    beta = await store.load_participant(tenants.beta.owner_participant_id)

    with pytest.raises(NotFound):
        await checkpoints.load("chk_alpha_direct_private", recipient=beta)
    with pytest.raises(NotFound):
        await checkpoints.load("chk_missing", recipient=beta)
    assert await checkpoints.latest_for_task(protected.id, recipient=beta) == []
    assert await checkpoints.latest_for_task("tsk_missing", recipient=beta) == []
    with pytest.raises(InvalidCommand, match="require a room or recipient"):
        await checkpoints.load("chk_alpha_direct_private")
    with pytest.raises(InvalidCommand, match="require a room or recipient"):
        await checkpoints.latest_for_task(protected.id)


async def test_foreign_work_id_cannot_be_updated_or_ended(tenants: TenantPair):
    async with _client() as client:
        created = await client.post(
            f"/api/rooms/{tenants.alpha.room_id}/work",
            headers=tenants.alpha.auth,
            json={"headline": "ALPHA WORK CANARY", "targets": ["alpha/work.py"]},
        )
        assert created.status_code == 201
        work_id = created.json()["work"]["id"]
        before = await _durable_coordination_state(tenants)

        update = await client.patch(
            f"/api/rooms/{tenants.beta.room_id}/work",
            headers=tenants.beta.auth,
            json={
                "work_id": work_id,
                "headline": "BETA OVERWRITE",
                "command_id": "cmd-foreign-work-update",
            },
        )
        end = await client.post(
            f"/api/rooms/{tenants.beta.room_id}/work/end",
            headers=tenants.beta.auth,
            json={
                "work_id": work_id,
                "note": "BETA END",
                "command_id": "cmd-foreign-work-end",
            },
        )

    row = await db.fetch_one("SELECT * FROM work_declarations WHERE id = ?", (work_id,))
    assert row is not None
    assert update.status_code == end.status_code == 404
    assert update.json()["error"] == end.json()["error"] == "not_found"
    assert row["headline"] == "ALPHA WORK CANARY"
    assert row["ended_at"] is None
    assert await _durable_coordination_state(tenants) == before


async def test_foreign_and_missing_work_ids_are_indistinguishable(tenants: TenantPair):
    async with _client() as client:
        created = await client.post(
            f"/api/rooms/{tenants.alpha.room_id}/work",
            headers=tenants.alpha.auth,
            json={"headline": "ALPHA WORK CANARY"},
        )
        assert created.status_code == 201
        foreign_id = created.json()["work"]["id"]

        foreign = await client.patch(
            f"/api/rooms/{tenants.beta.room_id}/work",
            headers=tenants.beta.auth,
            json={"work_id": foreign_id, "headline": "probe"},
        )
        missing = await client.patch(
            f"/api/rooms/{tenants.beta.room_id}/work",
            headers=tenants.beta.auth,
            json={"work_id": "wrk_missing", "headline": "probe"},
        )

    assert foreign.status_code == missing.status_code == 404
    assert foreign.json()["error"] == missing.json()["error"] == "not_found"


async def test_foreign_directive_and_question_cannot_be_acknowledged_or_answered(
    tenants: TenantPair,
):
    async with _client() as client:
        directive = await client.post(
            f"/api/rooms/{tenants.alpha.room_id}/directives",
            headers=tenants.alpha.auth,
            json={
                "target_participant_id": tenants.alpha.owner_participant_id,
                "action": "input",
                "reason": "ALPHA DIRECTIVE CANARY",
            },
        )
        question = await client.post(
            f"/api/rooms/{tenants.alpha.room_id}/questions",
            headers=tenants.alpha.auth,
            json={"body": "ALPHA QUESTION CANARY"},
        )
        assert directive.status_code == question.status_code == 201

        ack = await client.post(
            f"/api/rooms/{tenants.beta.room_id}/directives/acknowledge",
            headers=tenants.beta.auth,
            json={"directive_id": directive.json()["directive"]["id"]},
        )
        answer = await client.post(
            f"/api/rooms/{tenants.beta.room_id}/questions/answer",
            headers=tenants.beta.auth,
            json={
                "question_id": question.json()["question"]["id"],
                "body": "BETA ANSWER",
            },
        )

    assert ack.status_code == answer.status_code == 404
    assert (
        await db.fetch_value(
            "SELECT acknowledged_at FROM directives WHERE id = ?",
            (directive.json()["directive"]["id"],),
        )
        is None
    )
    assert (
        await db.fetch_value(
            "SELECT answered_at FROM questions WHERE id = ?",
            (question.json()["question"]["id"],),
        )
        is None
    )


# ---------------------------------------------------------------------------
# Credential tenancy and scope confinement
# ---------------------------------------------------------------------------


async def test_runtime_credential_is_room_scoped_and_cannot_escalate(tenants: TenantPair):
    async with _client() as client:
        minted = await client.post(
            f"/api/rooms/{tenants.beta.room_id}/credentials",
            headers=tenants.beta.auth,
            json={
                "label": "beta worker",
                "scopes": ["room.read", "task.claim", "room.admin", "artifact.write"],
                "ttl_seconds": 3600,
            },
        )
        assert minted.status_code == 201
        credential = minted.json()["credential"]
        runtime_auth = {"Authorization": f"Bearer {minted.json()['token']}"}

        alpha_read = await client.get(
            f"/api/rooms/{tenants.alpha.room_id}/snapshot", headers=runtime_auth
        )
        beta_admin = await client.post(
            f"/api/rooms/{tenants.beta.room_id}/participants/role",
            headers=runtime_auth,
            json={
                "target_participant_id": tenants.beta.owner_participant_id,
                "role": "observer",
                "reason": "scope escalation probe",
            },
        )

    assert set(credential["scopes"]) == {"room.read", "task.claim"}
    assert alpha_read.status_code == 403
    assert beta_admin.status_code == 403


async def test_foreign_credential_cannot_be_listed_or_revoked(tenants: TenantPair):
    async with _client() as client:
        minted = await client.post(
            f"/api/rooms/{tenants.alpha.room_id}/credentials",
            headers=tenants.alpha.auth,
            json={"label": "alpha worker", "scopes": ["room.read"], "ttl_seconds": 3600},
        )
        assert minted.status_code == 201
        credential_id = minted.json()["credential"]["id"]

        listed = await client.get(
            f"/api/rooms/{tenants.beta.room_id}/credentials", headers=tenants.beta.auth
        )
        revoked = await client.post(
            f"/api/rooms/{tenants.beta.room_id}/credentials/revoke",
            headers=tenants.beta.auth,
            json={"credential_id": credential_id, "reason": "cross-tenant probe"},
        )

    assert listed.status_code == 200
    assert credential_id not in {item["id"] for item in listed.json()["credentials"]}
    assert revoked.status_code == 404
    row = await db.fetch_one(
        "SELECT revoked_at FROM participant_credentials WHERE id = ?", (credential_id,)
    )
    assert row is not None and row["revoked_at"] is None


async def test_receipt_collision_cannot_replay_or_rotate_another_tenants_credential(
    tenants: TenantPair,
):
    alpha = await store.load_participant(tenants.alpha.owner_participant_id)
    beta = await store.load_participant(tenants.beta.owner_participant_id)
    command_id = "customer-local-credential-command"

    alpha_issued = await rooms.mint_runtime_credential(
        participant=alpha,
        command=MintCredentialCommand(command_id=command_id, label="alpha runtime"),
    )
    alpha_hash_before = await db.fetch_value(
        "SELECT token_hash FROM participant_credentials WHERE id = ?",
        (alpha_issued.credential.id,),
    )

    beta_issued = await rooms.mint_runtime_credential(
        participant=beta,
        command=MintCredentialCommand(command_id=command_id, label="beta runtime"),
    )

    assert beta_issued.credential.id != alpha_issued.credential.id
    assert beta_issued.credential.room_id == tenants.beta.room_id
    assert (
        await db.fetch_value(
            "SELECT token_hash FROM participant_credentials WHERE id = ?",
            (alpha_issued.credential.id,),
        )
        == alpha_hash_before
    )
    assert (
        await store.load_participant_by_token(alpha_issued.token)
    ).room_id == tenants.alpha.room_id
    assert (
        await store.load_participant_by_token(beta_issued.token)
    ).room_id == tenants.beta.room_id

    with pytest.raises(InvalidCommand, match="cannot be shown again"):
        await rooms.mint_runtime_credential(
            participant=alpha,
            command=MintCredentialCommand(command_id=command_id, label="replacement probe"),
        )
    assert (
        await db.fetch_value(
            "SELECT token_hash FROM participant_credentials WHERE id = ?",
            (alpha_issued.credential.id,),
        )
        == alpha_hash_before
    )
