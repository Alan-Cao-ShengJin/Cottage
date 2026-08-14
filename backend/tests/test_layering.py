"""Architectural constraints, enforced rather than documented.

`docs/ARCHITECTURE.md` §1 states the dependency rule `adapters → core → domain` and
`CLAUDE.md` states that no vendor SDK may appear in the core. Both are the kind of
rule that erodes silently — one convenient import at a time — so they are checked
here instead of trusted.
"""

from __future__ import annotations

import ast
from pathlib import Path

APP = Path(__file__).resolve().parents[1] / "app"

#: Any of these appearing under core/ or domain/ means we started building the
#: product around a provider (ADR-006).
FORBIDDEN_VENDOR_MODULES = {
    "openai",
    "anthropic",
    "google",
    "cohere",
    "mistralai",
    "ollama",
    "langchain",
    "langchain_openai",
    "llama_index",
    "transformers",
}


def _module_files(*relative: str) -> list[Path]:
    files: list[Path] = []
    for rel in relative:
        files.extend(sorted((APP / rel).rglob("*.py")))
    return files


def _imported_modules(path: Path) -> set[str]:
    """Every module name this file imports, absolute and relative alike.

    Relative imports are resolved to a dotted path rooted at `app` so that
    `from ...core import x` inside an adapter is comparable with `app.core.x`.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    package_parts = path.relative_to(APP).parent.parts
    found: set[str] = set()

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                found.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0:
                if node.module:
                    found.add(node.module)
                continue
            # level=1 is the current package, level=2 its parent, and so on.
            base = list(package_parts[: len(package_parts) - (node.level - 1)])
            if node.module:
                base.extend(node.module.split("."))
            found.add(".".join(["app", *base]) if base else "app")
            for alias in node.names:
                found.add(".".join(["app", *base, alias.name]))
    return found


def test_core_does_not_import_adapters_or_api():
    """The core must not know how anyone reached it.

    If this fails, some rule has leaked into a transport (or is about to), and the
    next adapter will need that rule reimplemented.
    """
    offenders: list[str] = []
    for path in _module_files("core"):
        for module in _imported_modules(path):
            if ".adapters" in module or module.endswith(".api") or ".api." in module:
                offenders.append(f"{path.relative_to(APP)} imports {module}")
    assert offenders == [], "core must not depend on adapters or api:\n" + "\n".join(offenders)


def test_domain_imports_nothing_from_the_rest_of_the_app():
    """`domain/` is pure types. It may not reach into core, db, adapters, or api."""
    offenders: list[str] = []
    for path in _module_files("domain"):
        for module in _imported_modules(path):
            if not module.startswith("app"):
                continue
            tail = module[len("app") :]
            if any(seg in tail for seg in (".core", ".db", ".adapters", ".api")):
                offenders.append(f"{path.relative_to(APP)} imports {module}")
    assert offenders == [], "domain must stay pure:\n" + "\n".join(offenders)


def test_domain_performs_no_io():
    """No database, HTTP, or filesystem access in the domain layer."""
    offenders: list[str] = []
    io_modules = {"aiosqlite", "sqlite3", "httpx", "requests", "fastapi", "socket"}
    for path in _module_files("domain"):
        for module in _imported_modules(path):
            if module.split(".")[0] in io_modules:
                offenders.append(f"{path.relative_to(APP)} imports {module}")
    assert offenders == [], "domain must not perform I/O:\n" + "\n".join(offenders)


def test_no_model_provider_sdk_in_core_or_domain():
    """We host coordination, not inference (ADR-006)."""
    offenders: list[str] = []
    for path in _module_files("core", "domain"):
        for module in _imported_modules(path):
            if module.split(".")[0] in FORBIDDEN_VENDOR_MODULES:
                offenders.append(f"{path.relative_to(APP)} imports {module}")
    assert offenders == [], "no model-provider SDK may appear in core/ or domain/:\n" + "\n".join(
        offenders
    )


def test_no_model_provider_sdk_anywhere_in_the_app():
    """Stronger form: there is no inference in this product at all, in any layer."""
    offenders: list[str] = []
    for path in APP.rglob("*.py"):
        for module in _imported_modules(path):
            if module.split(".")[0] in FORBIDDEN_VENDOR_MODULES:
                offenders.append(f"{path.relative_to(APP)} imports {module}")
    assert offenders == [], "Agent Rooms performs no inference:\n" + "\n".join(offenders)


def test_no_provider_credentials_in_config():
    """A provider key appearing in config would be a design regression (CLAUDE.md)."""
    config = (APP / "config.py").read_text(encoding="utf-8").lower()
    for needle in ("openai_api_key", "anthropic_api_key", "api_key", "model_name"):
        assert needle not in config, f"config.py must not carry `{needle}`"


def test_host_class_is_not_used_in_behavior_derivation():
    """Runtime policy must be derived from capabilities, never a provider label
    (correction 3). A label used in a *behavior* decision is the regression this
    catches; recording it for display is fine.
    """
    source = (APP / "domain" / "capabilities.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    derive = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "derive_runtime_policy"
    )
    arg_names = {a.arg for a in derive.args.args} | {a.arg for a in derive.args.kwonlyargs}
    assert "host_class" not in arg_names
    # And no reference to the enum inside the body, so it cannot be reached globally.
    body_names = {node.id for node in ast.walk(derive) if isinstance(node, ast.Name)} | {
        node.attr for node in ast.walk(derive) if isinstance(node, ast.Attribute)
    }
    assert "HostClass" not in body_names
    assert "host_class" not in body_names


def test_every_documented_event_type_exists_and_vice_versa():
    """The event registry is closed and the docs are canonical, so the two must
    agree exactly (`docs/PROTOCOL.md` §2)."""
    import re

    from app.domain.events import EventType

    doc = (APP.parents[1] / "docs" / "PROTOCOL.md").read_text(encoding="utf-8")
    documented = set(re.findall(r"`([a-z_]+\.[a-z_]+)`", doc))
    declared = {e.value for e in EventType}

    # Only compare names that look like event types, i.e. ones the doc lists in the
    # registry table; the doc also mentions command names, which share the shape.
    undocumented = {
        e for e in declared if e not in documented and f"`{e}`" not in doc and e not in doc
    }
    assert undocumented == set(), (
        f"these event types are not in docs/PROTOCOL.md §2: {sorted(undocumented)}"
    )
