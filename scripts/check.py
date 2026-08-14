#!/usr/bin/env python
"""One-shot gate: tests, typecheck, lint, frontend typecheck.

Run before every commit (`CLAUDE.md`). Runs every stage even when an earlier one
fails, then reports all failures together — a gate that stops at the first error makes
you re-run it once per problem.

Usage:
    python scripts/check.py              # everything
    python scripts/check.py --backend    # skip the frontend
    python scripts/check.py --fast       # tests only
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND = REPO_ROOT / "backend"
FRONTEND = REPO_ROOT / "frontend"


def _python() -> str:
    """Prefer the repo venv, so the gate does not silently use a different runtime."""
    for candidate in (
        BACKEND / ".venv" / "Scripts" / "python.exe",
        BACKEND / ".venv" / "bin" / "python",
        REPO_ROOT / ".venv" / "Scripts" / "python.exe",
        REPO_ROOT / ".venv" / "bin" / "python",
    ):
        if candidate.exists():
            return str(candidate)
    return sys.executable


PYTHON = _python()


@dataclass
class Stage:
    name: str
    command: list[str]
    cwd: Path
    #: True when the tool may be absent (e.g. no `npm install` yet). Absence is
    #: reported as a skip, not a failure, so a backend-only checkout still gates.
    optional: bool = False


def stages(*, include_frontend: bool, fast: bool) -> list[Stage]:
    out = [Stage("pytest", [PYTHON, "-m", "pytest", "-q"], BACKEND)]
    if fast:
        return out
    out += [
        Stage("mypy", [PYTHON, "-m", "mypy", "app"], BACKEND),
        Stage("ruff", [PYTHON, "-m", "ruff", "check", "."], BACKEND),
        Stage("ruff format", [PYTHON, "-m", "ruff", "format", "--check", "."], BACKEND),
    ]
    if include_frontend:
        npm = shutil.which("npm")
        if npm and (FRONTEND / "node_modules").exists():
            out.append(Stage("tsc", [npm, "run", "typecheck"], FRONTEND))
        else:
            out.append(
                Stage("tsc", [npm or "npm", "run", "typecheck"], FRONTEND, optional=True)
            )
    return out


def run(stage: Stage) -> tuple[str, bool, str]:
    print(f"── {stage.name} " + "─" * max(0, 60 - len(stage.name)), flush=True)
    try:
        completed = subprocess.run(
            stage.command,
            cwd=stage.cwd,
            capture_output=True,
            text=True,
            # `npm` on Windows is a shim, so a shell is needed for it to resolve.
            shell=os.name == "nt" and stage.command[0].endswith("npm"),
        )
    except FileNotFoundError:
        message = f"{stage.command[0]} not found"
        print(f"   skipped: {message}\n", flush=True)
        return stage.name, stage.optional, "skipped"

    output = (completed.stdout + completed.stderr).strip()
    ok = completed.returncode == 0

    if ok:
        tail = output.splitlines()[-1] if output else "ok"
        print(f"   {tail}\n", flush=True)
    else:
        if stage.optional:
            print(f"   skipped (not installed):\n{_indent(output)}\n", flush=True)
            return stage.name, True, "skipped"
        print(f"{_indent(output)}\n", flush=True)

    return stage.name, ok, "ok" if ok else "FAILED"


def _indent(text: str, prefix: str = "   ") -> str:
    return "\n".join(prefix + line for line in text.splitlines())


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backend", action="store_true", help="skip the frontend stage")
    parser.add_argument("--fast", action="store_true", help="tests only")
    args = parser.parse_args()

    print(f"Agent Rooms gate · python={PYTHON}\n")

    results = [run(stage) for stage in stages(include_frontend=not args.backend, fast=args.fast)]

    print("─" * 62)
    failed = [name for name, ok, _ in results if not ok]
    for name, ok, status in results:
        mark = "ok  " if ok and status == "ok" else "skip" if status == "skipped" else "FAIL"
        print(f"  [{mark}] {name}")

    if failed:
        print(f"\n{len(failed)} stage(s) failed: {', '.join(failed)}")
        return 1
    print("\nAll stages passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
