"""Prompt for an operator password and print only its Argon2id verifier.

The plaintext never enters argv, shell history, environment variables, or a file. Redirect
stdout only if you intentionally want to capture the verifier; treat it as a deployment
secret because it can be attacked offline.
"""

from __future__ import annotations

import getpass
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.core.accounts import hash_password  # noqa: E402


def main() -> int:
    first = getpass.getpass("New operator password (15+ characters): ")
    second = getpass.getpass("Confirm operator password: ")
    if first != second:
        print("Passwords do not match.", file=sys.stderr)
        return 2
    try:
        encoded = hash_password(first)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(encoded)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
