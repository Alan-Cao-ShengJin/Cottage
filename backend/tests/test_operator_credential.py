"""The instance operator's credential: rotation, and identity that cannot fork.

Both properties here were broken in ways that produce no error message at all, which is why
they are pinned rather than left to review. Found by auditing the first real deployment
(D-024).

* **Rotation must revoke.** `set_principal_token` was `INSERT OR REPLACE` keyed on the token
  hash, so configuring a new value merely *added* a row. Every token ever configured stayed
  valid, which means rotating a leaked `OPERATOR_TOKEN` accomplished nothing — and an
  instance that had once booted on the published default kept honouring it forever.
* **A rename must not fork the operator.** The org was resolved by slug, so changing
  `OPERATOR_ORG_NAME` created a second org while the existing user kept the first. Rooms are
  written under `user.org_id` but listed by the principal's `org_id`, so the operator's
  console went permanently empty with every room still present in the database.
"""

from __future__ import annotations

import pytest

from app.core import rooms
from app.core.errors import Unauthenticated


@pytest.mark.asyncio
async def test_rotating_the_operator_token_revokes_the_previous_one(fresh_db) -> None:
    """The old value is where the danger lives, so it is the value that must stop working."""
    org_id, user_id = await rooms.ensure_org_and_user(
        org_name="Acme", org_slug="acme", email="op@acme.test", display_name="Op"
    )

    await rooms.set_principal_token(
        token="leaked-original",
        subject_kind="user",
        subject_id=user_id,
        org_id=org_id,
        label="instance operator",
    )
    assert (await rooms.authenticate_principal("leaked-original")).user is not None

    await rooms.set_principal_token(
        token="freshly-rotated",
        subject_kind="user",
        subject_id=user_id,
        org_id=org_id,
        label="instance operator",
    )

    assert (await rooms.authenticate_principal("freshly-rotated")).user is not None
    with pytest.raises(Unauthenticated):
        await rooms.authenticate_principal("leaked-original")


@pytest.mark.asyncio
async def test_an_unchanged_token_keeps_working_across_restarts(fresh_db) -> None:
    """Every boot re-installs the configured token. That must be a no-op, not a lockout."""
    org_id, user_id = await rooms.ensure_org_and_user(
        org_name="Acme", org_slug="acme", email="op@acme.test", display_name="Op"
    )
    for _ in range(3):
        await rooms.set_principal_token(
            token="stable-secret",
            subject_kind="user",
            subject_id=user_id,
            org_id=org_id,
            label="instance operator",
        )
    assert (await rooms.authenticate_principal("stable-secret")).user is not None


@pytest.mark.asyncio
async def test_rotation_leaves_oauth_granted_agent_tokens_alone(fresh_db) -> None:
    """Rotating the operator's own credential must not sign every agent out.

    An access token exists because a human granted it at a consent screen; it is not this
    function's to revoke, and revoking it would make routine hygiene look like an outage.
    Provenance (`client_id`) is what separates the two, not the label.
    """
    from app.core import oauth

    org_id, user_id = await rooms.ensure_org_and_user(
        org_name="Acme", org_slug="acme", email="op@acme.test", display_name="Op"
    )
    identity = await rooms.ensure_identity(
        org_id=org_id, owner_user_id=user_id, display_name="Some Agent"
    )
    granted = await oauth._issue_tokens(
        client_id="cli_test",
        agent_identity_id=identity.id,
        org_id=org_id,
        scope="agent",
        audience="https://x.test/mcp",
    )

    await rooms.set_principal_token(
        token="rotated-operator-secret",
        subject_kind="user",
        subject_id=user_id,
        org_id=org_id,
        label="instance operator",
    )

    principal = await oauth.authenticate_access_token(
        granted.access_token, expected_audience="https://x.test/mcp"
    )
    assert principal.identity is not None
    assert principal.identity.id == identity.id


@pytest.mark.asyncio
async def test_renaming_the_operator_org_renames_rather_than_forking(fresh_db) -> None:
    """The person is the anchor: same email, same org, new display name.

    A second org here is the bug — it is what silently detached the operator from their own
    rooms.
    """
    from app.db import database as db

    first_org, first_user = await rooms.ensure_org_and_user(
        org_name="Dev Org", org_slug="dev-org", email="op@acme.test", display_name="Op"
    )
    second_org, second_user = await rooms.ensure_org_and_user(
        org_name="Arc Avenue", org_slug="arc-avenue", email="op@acme.test", display_name="Op"
    )

    assert second_org == first_org, "renaming must not create a second org"
    assert second_user == first_user

    orgs = await db.fetch_all("SELECT id, name FROM organizations")
    assert len(orgs) == 1
    assert orgs[0]["name"] == "Arc Avenue", "the new name should be applied to the same org"


@pytest.mark.asyncio
async def test_a_principal_reports_its_subjects_own_org(fresh_db) -> None:
    """One authority for "which org is this caller in", so the two cannot disagree.

    The token row carries a denormalised `org_id`. When it drifted from the user's, rooms
    were created in one org and listed from another — invisible, and total. The subject's own
    org wins.
    """
    org_id, user_id = await rooms.ensure_org_and_user(
        org_name="Acme", org_slug="acme", email="op@acme.test", display_name="Op"
    )
    await rooms.set_principal_token(
        token="operator-secret",
        subject_kind="user",
        subject_id=user_id,
        # A stale/incorrect org on the token row, which is the state the old bug produced.
        org_id="org_something_else_entirely",
        label="instance operator",
    )

    principal = await rooms.authenticate_principal("operator-secret")
    assert principal.user is not None
    assert principal.org_id == org_id == principal.user.org_id
