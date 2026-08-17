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
    DEFAULT_OPERATOR_TOKEN,
    UNSAFE_PUBLIC_BILLING,
    UNSAFE_PUBLIC_OPERATOR,
    UNSAFE_PUBLIC_SIGNUP_EMAIL,
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
        public_base_url=base_url, bootstrap_operator=True, operator_token=DEFAULT_OPERATOR_TOKEN
    )
    assert config.is_publicly_reachable is True
    with pytest.raises(RuntimeError) as exc:
        check_public_safety(config)
    # The message must tell them how to fix it, not just that they are wrong.
    assert "BOOTSTRAP_OPERATOR" in str(exc.value)
    assert str(exc.value) == UNSAFE_PUBLIC_OPERATOR


def test_public_url_with_a_real_secret_is_allowed():
    """`mcp_require_auth` is set too: exposure needs *both* guards satisfied, and this
    test is about the bootstrap one specifically."""
    config = _settings(
        public_base_url="https://romantic-hippo.trycloudflare.com",
        bootstrap_operator=True,
        operator_token="s6xk2p9qw4m7v1t8z3r5n0h6j2c4b8d1f7g9k3l5",
        mcp_require_auth=True,
    )
    check_public_safety(config)


def test_public_url_with_bootstrap_disabled_is_allowed():
    config = _settings(
        public_base_url="https://agent-rooms.example.com",
        bootstrap_operator=False,
        operator_token=DEFAULT_OPERATOR_TOKEN,
        mcp_require_auth=True,
    )
    check_public_safety(config)


def test_public_signup_refuses_to_boot_without_outbound_email():
    config = _settings(
        public_base_url="https://agent-rooms.example.com",
        bootstrap_operator=False,
        mcp_require_auth=True,
        public_signup_enabled=True,
        resend_api_key="",
    )
    with pytest.raises(RuntimeError) as exc:
        check_public_safety(config)
    assert str(exc.value) == UNSAFE_PUBLIC_SIGNUP_EMAIL


def test_paid_creator_mode_refuses_incomplete_stripe_configuration():
    config = _settings(
        public_base_url="https://agent-rooms.example.com",
        bootstrap_operator=False,
        mcp_require_auth=True,
        enforce_creator_subscription=True,
        stripe_secret_key="sk_test_configured",
        stripe_webhook_secret="",
        stripe_creator_price_id="price_configured",
    )
    with pytest.raises(RuntimeError) as exc:
        check_public_safety(config)
    assert str(exc.value) == UNSAFE_PUBLIC_BILLING


def test_the_two_guards_are_independent():
    """Satisfying one must not excuse the other — otherwise flipping a single switch
    reopens the endpoint."""
    from app.config import UNSAFE_PUBLIC_MCP, UNSAFE_PUBLIC_OPERATOR

    public = "https://romantic-hippo.trycloudflare.com"

    # Real secret, but MCP auth off.
    with pytest.raises(RuntimeError) as exc:
        check_public_safety(
            _settings(
                public_base_url=public,
                bootstrap_operator=True,
                operator_token="s6xk2p9qw4m7v1t8z3r5n0h6j2c4b8d1f7g9k3l5",
                mcp_require_auth=False,
            )
        )
    assert str(exc.value) == UNSAFE_PUBLIC_MCP

    # MCP auth on, but the published default token still in play.
    with pytest.raises(RuntimeError) as exc:
        check_public_safety(
            _settings(
                public_base_url=public,
                bootstrap_operator=True,
                operator_token=DEFAULT_OPERATOR_TOKEN,
                mcp_require_auth=True,
            )
        )
    assert str(exc.value) == UNSAFE_PUBLIC_OPERATOR


# ---------------------------------------------------------------------------
# Failing closed when we do not know whether we are exposed
#
# The guards above all begin by asking `is_publicly_reachable`, which reads
# `PUBLIC_BASE_URL`. Behind a tunnel that was sound — the variable had to be set for the
# tunnel to work. On a hosting platform it inverts: the app is reachable at a hostname the
# platform assigns, so an *unset* variable leaves both guards disarmed while the instance is
# live. An audit of the first deployment found it (D-024). These tests cover the case where
# configuration does not tell us the truth.
# ---------------------------------------------------------------------------


def test_undeclared_base_url_on_a_platform_refuses_to_start():
    """The exact shape of a forgotten `fly secrets set PUBLIC_BASE_URL=...`.

    Everything here looks locked down to the old checks: the URL *reads* as localhost, so
    `is_publicly_reachable` is False and both guards return early — while the published
    default token guards a public hostname.
    """
    from app.config import UNDECLARED_PUBLIC_BASE_URL

    config = _settings(
        public_base_url="http://localhost:8000",
        public_base_url_declared=False,
        hosting_platform="fly.io",
        bootstrap_operator=True,
        operator_token=DEFAULT_OPERATOR_TOKEN,
        mcp_require_auth=True,
    )
    assert config.is_publicly_reachable is False, "the premise: config claims to be local"

    with pytest.raises(RuntimeError) as exc:
        check_public_safety(config)
    assert str(exc.value) == UNDECLARED_PUBLIC_BASE_URL.format(platform="fly.io")
    # Must name the variable and the fix, since this fires during a deploy someone is
    # watching scroll past.
    assert "PUBLIC_BASE_URL" in str(exc.value)
    assert "fly secrets set" in str(exc.value)


def test_the_published_default_token_is_refused_on_a_platform_whatever_the_url_says():
    """Independent of the URL check, so a wrong-but-parseable URL cannot buy it a pass.

    Belt and braces on purpose: the URL guard depends on the deployer having configured one
    thing correctly, and this is the case where they configured it *incorrectly*.
    """
    from app.config import UNSAFE_DEFAULT_OPERATOR_ON_PLATFORM

    config = _settings(
        # Declared, parseable, and wrong — points at some other deployment.
        public_base_url="https://someone-elses-app.fly.dev",
        public_base_url_declared=True,
        hosting_platform="fly.io",
        bootstrap_operator=True,
        operator_token=DEFAULT_OPERATOR_TOKEN,
        mcp_require_auth=True,
    )
    with pytest.raises(RuntimeError) as exc:
        check_public_safety(config)
    assert str(exc.value) == UNSAFE_DEFAULT_OPERATOR_ON_PLATFORM.format(platform="fly.io")


def test_a_scheme_less_base_url_is_a_configuration_error_not_a_local_instance():
    """`PUBLIC_BASE_URL=agent-rooms.fly.dev` parses to *no* hostname.

    The empty host then matches `LOCAL_HOSTS`, so the typo silently disarms every guard.
    Refusing is the only safe reading: we were told to be public and cannot tell where.
    """
    from app.config import UNPARSEABLE_PUBLIC_BASE_URL

    config = _settings(
        public_base_url="agent-rooms.fly.dev",
        public_base_url_declared=True,
        bootstrap_operator=True,
        operator_token=DEFAULT_OPERATOR_TOKEN,
    )
    assert config.public_base_url_is_parseable is False
    with pytest.raises(RuntimeError) as exc:
        check_public_safety(config)
    assert str(exc.value) == UNPARSEABLE_PUBLIC_BASE_URL.format(value="agent-rooms.fly.dev")


def test_platform_detection_only_ever_tightens():
    """A recognised platform must never *permit* something the old checks refused.

    Stated as a test because the mechanism is env-var sniffing, which is exactly the kind of
    thing that grows an accidental escape hatch. For every configuration, adding a platform
    marker may turn accept into refuse, never refuse into accept.
    """
    configurations = [
        {"public_base_url": "https://rooms.example.com", "operator_token": DEFAULT_OPERATOR_TOKEN},
        {"public_base_url": "https://rooms.example.com", "mcp_require_auth": False},
        {"public_base_url": "http://localhost:8000", "operator_token": DEFAULT_OPERATOR_TOKEN},
        {"public_base_url": "https://rooms.example.com", "operator_token": "a-real-secret-value"},
    ]
    for overrides in configurations:
        base: dict[str, object] = {
            "bootstrap_operator": True,
            "mcp_require_auth": True,
            "public_base_url_declared": True,
        }
        base.update(overrides)

        def refused(config: dict[str, object], platform: str | None) -> bool:
            try:
                check_public_safety(_settings(**{**config, "hosting_platform": platform}))
                return False
            except RuntimeError:
                return True

        without = refused(base, None)
        with_platform = refused(base, "fly.io")
        assert with_platform or not without, (
            f"platform detection made {overrides} *more* permissive: "
            f"refused={without} without a platform, refused={with_platform} with one"
        )


def test_local_development_still_boots_with_nothing_configured():
    """The convenience this whole mechanism must not destroy.

    A fresh clone, no `.env`, `uvicorn` — no platform marker, no declared URL, published
    default token. That has to keep working, or the guard has bought safety by making the
    project unusable.
    """
    check_public_safety(
        _settings(
            public_base_url="http://localhost:8000",
            public_base_url_declared=False,
            hosting_platform=None,
            bootstrap_operator=True,
            operator_token=DEFAULT_OPERATOR_TOKEN,
            mcp_require_auth=False,
        )
    )


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
