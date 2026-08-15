"""Class-level guarantees over the whole externally accepted command graph.

D-042 fixed `extra="forbid"` on four nested models that were known to be reachable
from a command. That is a list, and a list is what the next nested request model will
not be on. The reviewer's point: make a test provide the guarantee that inheritance
was mistakenly assumed to provide (D-043).
"""

from __future__ import annotations

import inspect
import typing

import pytest
from pydantic import BaseModel

from app.domain import commands as command_module
from app.domain.commands import CommandMeta

#: Models reachable from a command that deliberately do not forbid extras, with the
#: reason. Empty today. Anything added here needs a sentence saying why an unknown
#: field arriving from outside is safe to ignore in that specific model.
EXEMPT: dict[str, str] = {}


def _commands() -> list[type[BaseModel]]:
    return [
        obj
        for _, obj in inspect.getmembers(command_module, inspect.isclass)
        if issubclass(obj, CommandMeta) and obj is not CommandMeta
    ]


def _reachable_models(model: type[BaseModel], seen: set[type[BaseModel]]) -> None:
    """Every BaseModel reachable through this model's field annotations."""
    if model in seen:
        return
    seen.add(model)
    for field in model.model_fields.values():
        for annotation in _unwrap(field.annotation):
            if isinstance(annotation, type) and issubclass(annotation, BaseModel):
                _reachable_models(annotation, seen)


def _unwrap(annotation: object) -> list[object]:
    """Flatten Optional/list/dict/union wrappers down to the concrete types inside."""
    args = typing.get_args(annotation)
    if not args:
        return [annotation]
    out: list[object] = []
    for arg in args:
        out.extend(_unwrap(arg))
    return out


def test_every_command_forbids_unknown_fields():
    """The top level, which `CommandMeta` does give by inheritance."""
    for command in _commands():
        assert command.model_config.get("extra") == "forbid", command.__name__


def test_every_model_reachable_from_a_command_forbids_unknown_fields():
    """The level below, which inheritance does *not* give.

    Pydantic config is per class, not per object graph. So a command accepting a nested
    request model gets no protection from `CommandMeta`, and an unknown field inside
    that nested object is dropped silently — which for `Disclosure` meant a payload
    classified `org_internal` publishing as `room_public` (D-042).

    Walking the graph rather than listing the models is the whole point: the next
    nested model to be added is precisely the one nobody remembers to check.
    """
    reachable: set[type[BaseModel]] = set()
    for command in _commands():
        _reachable_models(command, reachable)

    offenders = sorted(
        m.__name__
        for m in reachable
        if m.model_config.get("extra") != "forbid" and m.__name__ not in EXEMPT
    )
    assert not offenders, (
        f"reachable from a command but silently ignoring unknown fields: {offenders}. "
        "Add model_config = ConfigDict(extra='forbid'), or add an entry to EXEMPT "
        "with the reason an unknown field is safe to drop there."
    )


@pytest.mark.parametrize("name", sorted(EXEMPT))
def test_exemptions_are_still_reachable(name):
    """An exemption for a model no longer in the graph is stale documentation."""
    reachable: set[type[BaseModel]] = set()
    for command in _commands():
        _reachable_models(command, reachable)
    assert name in {m.__name__ for m in reachable}
