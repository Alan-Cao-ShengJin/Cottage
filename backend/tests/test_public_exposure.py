"""Guards for exposing this instance beyond the local machine.

Two separate concerns, both of which only matter the moment someone tunnels the server:

* **The startup guard** — a publicly reachable instance must not be protected by a
  credential published in this repo. A warning is not enough; the failure is silent,
  total, and only discovered afterwards.
* **The ChatGPT Action schema** — must be valid OpenAPI 3.0, name a public server, and
  expose the coordination surface rather than the admin surface.
"""

from __future__ import annotations

import dataclasses

import pytest

from app.config import (
    DEFAULT_DEV_TOKEN,
    UNSAFE_PUBLIC_BOOTSTRAP,
    Settings,
    check_public_safety,
)


def _settings(**overrides) -> Settings:
    return dataclasses.replace(Settings(), **overrides)


# ---------------------------------------------------------------------------
# Startup guard
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "base_url",
    [
        "http://localhost:8000",
        "http://127.0.0.1:8000",
        "http://0.0.0.0:8000",
        "http://[::1]:8000",
    ],
)
def test_local_urls_are_not_treated_as_public(base_url):
    config = _settings(public_base_url=base_url)
    assert config.is_publicly_reachable is False
    # The default dev token is fine locally — that is the whole point of it.
    check_public_safety(config)


@pytest.mark.parametrize(
    "base_url",
    [
        "https://romantic-hippo.trycloudflare.com",
        "https://agent-rooms.example.com",
        "http://203.0.113.10:8000",
        "https://foo.loca.lt",
    ],
)
def test_public_url_with_the_published_default_token_refuses_to_start(base_url):
    config = _settings(
        public_base_url=base_url, dev_bootstrap=True, dev_bootstrap_token=DEFAULT_DEV_TOKEN
    )
    assert config.is_publicly_reachable is True
    with pytest.raises(RuntimeError) as exc:
        check_public_safety(config)
    # The message must tell them how to fix it, not just that they are wrong.
    assert "DEV_BOOTSTRAP" in str(exc.value)
    assert str(exc.value) == UNSAFE_PUBLIC_BOOTSTRAP


def test_public_url_with_a_real_secret_is_allowed():
    config = _settings(
        public_base_url="https://romantic-hippo.trycloudflare.com",
        dev_bootstrap=True,
        dev_bootstrap_token="s6xk2p9qw4m7v1t8z3r5n0h6j2c4b8d1f7g9k3l5",
    )
    check_public_safety(config)


def test_public_url_with_bootstrap_disabled_is_allowed():
    config = _settings(
        public_base_url="https://agent-rooms.example.com",
        dev_bootstrap=False,
        dev_bootstrap_token=DEFAULT_DEV_TOKEN,
    )
    check_public_safety(config)


# ---------------------------------------------------------------------------
# ChatGPT Action schema
# ---------------------------------------------------------------------------


@pytest.fixture
def gpt_schema():
    from app.api.gpt_schema import build_gpt_schema
    from app.main import app

    return build_gpt_schema(
        app.openapi(), public_base_url="https://romantic-hippo.trycloudflare.com"
    )


def test_schema_declares_openapi_30_not_31(gpt_schema):
    """ChatGPT's Action importer rejects 3.1."""
    assert gpt_schema["openapi"].startswith("3.0")


def test_schema_names_a_public_server(gpt_schema):
    """Without `servers`, ChatGPT has no idea where to send the request."""
    assert gpt_schema["servers"] == [{"url": "https://romantic-hippo.trycloudflare.com"}]


def test_schema_contains_no_31_only_constructs(gpt_schema):
    """`anyOf: [X, null]`, list-valued `type`, and `const` all break a 3.0 importer."""
    offenders: list[str] = []

    def walk(node, path="$"):
        if isinstance(node, dict):
            if "const" in node:
                offenders.append(f"{path}.const")
            if isinstance(node.get("type"), list):
                offenders.append(f"{path}.type[]")
            if isinstance(node.get("examples"), list):
                offenders.append(f"{path}.examples[]")
            any_of = node.get("anyOf")
            if isinstance(any_of, list) and any(
                isinstance(s, dict) and s.get("type") == "null" for s in any_of
            ):
                offenders.append(f"{path}.anyOf-null")
            for key, value in node.items():
                walk(value, f"{path}.{key}")
        elif isinstance(node, list):
            for i, item in enumerate(node):
                walk(item, f"{path}[{i}]")

    walk(gpt_schema)
    assert offenders == [], f"3.1-only constructs remain: {offenders[:10]}"


def test_nullable_fields_survive_the_downgrade(gpt_schema):
    """The rewrite must preserve the *meaning* of an optional field, not just delete it."""
    schemas = gpt_schema["components"]["schemas"]
    claim = schemas["ClaimTaskCommand"]["properties"]["requested_lease_seconds"]
    assert claim.get("nullable") is True
    assert claim.get("type") == "integer", claim


def test_schema_exposes_the_coordination_surface(gpt_schema):
    """An Action is a participant. It needs to join, see, declare, claim, and finish."""
    paths = gpt_schema["paths"]
    for required in (
        "/api/rooms/join",
        "/api/rooms/{room_id}/connect",
        "/api/rooms/{room_id}/snapshot",
        "/api/rooms/{room_id}/events",
        "/api/rooms/{room_id}/work",
        "/api/rooms/{room_id}/tasks/claim",
        "/api/rooms/{room_id}/tasks/complete",
        "/api/rooms/{room_id}/leave",
    ):
        assert required in paths, f"missing {required}"


def test_schema_omits_administrative_routes(gpt_schema):
    """An Action is not an administrator: no room listing, no close, no purge."""
    paths = gpt_schema["paths"]
    assert "/api/rooms/{room_id}/close" not in paths
    assert "get" not in paths.get("/api/rooms", {}), "room listing must not be exposed"


def test_every_operation_has_an_operation_id_and_summary(gpt_schema):
    """ChatGPT names the tool from these, so a missing one becomes an unusable action."""
    missing: list[str] = []
    for path, methods in gpt_schema["paths"].items():
        for method, operation in methods.items():
            if not operation.get("operationId"):
                missing.append(f"{method.upper()} {path}: operationId")
            if not (operation.get("summary") or operation.get("description")):
                missing.append(f"{method.upper()} {path}: summary/description")
    assert missing == [], missing


def test_schema_declares_bearer_auth(gpt_schema):
    scheme = gpt_schema["components"]["securitySchemes"]["participantToken"]
    assert scheme["type"] == "http"
    assert scheme["scheme"] == "bearer"
    assert gpt_schema["security"] == [{"participantToken": []}]


def test_description_warns_against_leaking_private_context(gpt_schema):
    """The Action description is the only briefing a GPT gets before it starts writing,
    so the disclosure rule has to be in it."""
    description = gpt_schema["info"]["description"].lower()
    assert "credential" in description
    assert "reasoning" in description or "system prompt" in description


def test_operation_count_stays_within_a_workable_action_budget(gpt_schema):
    """ChatGPT degrades with large Actions; keep the surface deliberate."""
    count = sum(len(methods) for methods in gpt_schema["paths"].values())
    assert count <= 30, f"{count} operations is too many for a usable Action"
