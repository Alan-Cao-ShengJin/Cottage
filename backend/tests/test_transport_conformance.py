"""Executable ARP transport matrix.

This is deliberately a surface gate, not a second interop suite. The mixed-room behavioural
invariants live in ``test_interop_conformance.py`` and in core tests. Here we make it difficult for
a transport to be called implemented after exposing only its easy operations.

The A2A cells are an executable roadmap. They skip while the optional A2A SDK is absent. If someone
adds that dependency, the cells become ordinary assertions and fail until the adapter explicitly
declares the full ARP semantic surface. Removing a skip therefore cannot silently overstate support.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from importlib.util import find_spec
from pathlib import Path

import pytest

from app.adapters.mcp import server as mcp
from app.api import routes as http


@dataclass(frozen=True)
class SemanticRow:
    concern: str
    http_callable: str
    mcp_callable: str
    a2a_capability: str


# One representative entry point per coordination concern. Adapters may expose more operations;
# passing this table means only that none of the hard, stateful concerns was omitted.
SEMANTIC_ROWS = (
    SemanticRow("identity_and_capabilities", "connect", "join_room", "identity_and_capabilities"),
    SemanticRow("visible_room_state", "get_snapshot", "get_room_state", "visible_room_state"),
    SemanticRow("current_work", "declare_work", "declare_current_work", "current_work"),
    SemanticRow(
        "runtime_operational_state",
        "set_runtime_state",
        "set_runtime_operational_state",
        "runtime_operational_state",
    ),
    SemanticRow("tasks_leases_and_fences", "claim_task", "claim_task", "tasks_leases_and_fences"),
    SemanticRow("checkpoints", "append_checkpoint", "record_checkpoint", "checkpoints"),
    SemanticRow("directives", "issue_directive", "steer_participant", "directives"),
    SemanticRow("questions", "ask_question", "ask_question", "questions"),
    SemanticRow("conflicts", "get_snapshot", "get_room_state", "conflicts"),
    SemanticRow("cursor_and_resume", "websocket_stream", "await_room_events", "cursor_and_resume"),
    SemanticRow("leave_and_peer_loss", "leave_room", "leave_room", "leave_and_peer_loss"),
)


@pytest.mark.parametrize(
    "transport,module,attribute",
    [
        pytest.param("http_sse", http, row.http_callable, id=f"http_sse-{row.concern}")
        for row in SEMANTIC_ROWS
    ]
    + [
        pytest.param("mcp", mcp, row.mcp_callable, id=f"mcp-{row.concern}") for row in SEMANTIC_ROWS
    ],
)
def test_implemented_transport_exposes_each_coordination_concern(transport, module, attribute):
    entry_point = getattr(module, attribute, None)
    assert callable(entry_point), f"{transport} is missing the {attribute} entry point"


A2A_SDK_AVAILABLE = find_spec("a2a") is not None


@pytest.mark.skipif(
    not A2A_SDK_AVAILABLE,
    reason="A2A SDK is not installed; adapter remains planned (M2.2)",
)
@pytest.mark.parametrize(
    "row",
    [pytest.param(row, id=f"a2a-{row.concern}") for row in SEMANTIC_ROWS],
)
def test_a2a_declares_every_arp_semantic_only_after_its_dependency_exists(row):
    """Installing an SDK is the trigger to make every planned A2A cell honestly red.

    The future adapter must publish this set only for semantics backed by conformance tests. A
    constant alone is not evidence; this gate prevents a partial adapter from being mistaken for a
    complete one while the behavioural tests are added alongside it.
    """
    from app.adapters import a2a

    supported = getattr(a2a, "CONFORMANT_ARP_SEMANTICS", frozenset())
    assert row.a2a_capability in supported, (
        f"A2A dependency is installed but {row.concern} has no declared conformance; "
        "keep the adapter status planned until its translation is implemented and tested"
    )


@pytest.mark.skipif(
    A2A_SDK_AVAILABLE,
    reason="A2A dependency exists; implementation conformance rows are now authoritative",
)
def test_a2a_placeholder_contains_no_business_logic_or_sdk_import():
    """The design task must not smuggle a partial adapter in with its contract."""
    from app.adapters import a2a

    public = {name for name in vars(a2a) if not name.startswith("_")}
    assert "CONFORMANT_ARP_SEMANTICS" not in public
    assert public == set(), "the planned package must remain documentation-only"

    source = Path(a2a.__file__).read_text(encoding="utf-8")
    imports = [
        node
        for node in ast.walk(ast.parse(source, filename=a2a.__file__))
        if isinstance(node, ast.Import | ast.ImportFrom)
    ]
    assert imports == [], "the placeholder must not import an A2A SDK before implementation"
