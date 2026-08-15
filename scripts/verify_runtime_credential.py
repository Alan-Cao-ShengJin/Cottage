"""Adversarially verify a runtime credential against a live instance (D-048, gate 3).

Asked for by the ChatGPT participant, and the reasoning is the same one CLAUDE.md
already records: a green gate is not evidence for `adapters/`, `api/` or `db/`, and
three defects reached production-shaped failure while unit tests passed. A credential
is a security boundary, so "the tests say it is narrow" is the weakest form of the
claim available. This one asks the deployed server.

Each check is written as an *attempt that must fail*, because a permission test that
only exercises the allowed path proves the feature works and nothing about its edges.

    backend\\.venv\\Scripts\\python.exe scripts\\verify_runtime_credential.py \\
        https://agent-rooms.fly.dev <participant_token> <room_id>

The participant token is the seat that mints; it is never printed and never logged.
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from typing import Any

PASS = "  PASS"
FAIL = "  FAIL"

failures: list[str] = []


def call(
    base: str, token: str, method: str, path: str, payload: dict[str, Any] | None = None
) -> tuple[int, dict[str, Any]]:
    request = urllib.request.Request(
        f"{base}{path}",
        data=json.dumps(payload).encode() if payload is not None else None,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        try:
            return exc.code, json.loads(raw)
        except ValueError:
            return exc.code, {"raw": raw[:300]}


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"{PASS if ok else FAIL}  {label}{(' — ' + detail) if detail else ''}")
    if not ok:
        failures.append(label)


def main(argv: list[str]) -> int:
    if len(argv) < 4:
        print(__doc__)
        return 2
    base, seat_token, room_id = argv[1].rstrip("/"), argv[2], argv[3]

    print("\n=== 1. A credential is the same seat with less authority ===")
    status, minted = call(
        base,
        seat_token,
        "POST",
        f"/api/rooms/{room_id}/credentials",
        {"label": "verify-runtime", "ttl_seconds": 900},
    )
    if status >= 400:
        print(f"  could not mint: {status} {minted}")
        return 1
    token = minted["token"]
    credential_id = minted["credential"]["id"]

    _, seat_me = call(base, seat_token, "GET", f"/api/rooms/{room_id}/hydrate")
    _, runtime_me = call(base, token, "GET", f"/api/rooms/{room_id}/hydrate")
    check(
        "same participant id",
        seat_me["you"]["participant_id"] == runtime_me["you"]["participant_id"],
        runtime_me["you"]["participant_id"],
    )
    seat_scopes = set(seat_me["you"]["scopes"])
    runtime_scopes = set(runtime_me["you"]["scopes"])
    check("strictly narrower scopes", runtime_scopes < seat_scopes, f"{len(runtime_scopes)} of {len(seat_scopes)}")
    check("no room.admin", "room.admin" not in runtime_scopes)
    check("no task.propose", "task.propose" not in runtime_scopes)
    check("no state.write", "state.write" not in runtime_scopes)
    check("no artifact.write", "artifact.write" not in runtime_scopes)

    print("\n=== 2. What it must be refused ===")
    status, body = call(
        base, token, "POST", f"/api/rooms/{room_id}/credentials", {"label": "a sibling"}
    )
    check("cannot mint another credential", status >= 400, f"{status} {body.get('error')}")

    status, body = call(
        base,
        token,
        "POST",
        f"/api/rooms/{room_id}/tasks",
        {"title": "a task this runtime should not be able to create"},
    )
    check("cannot create tasks", status >= 400, f"{status} {body.get('error')}")

    status, body = call(
        base,
        token,
        "POST",
        f"/api/rooms/{room_id}/participants/role",
        {"target_participant_id": runtime_me["you"]["participant_id"], "role": "owner",
         "reason": "escalation attempt"},
    )
    check("cannot grant itself a role", status >= 400, f"{status} {body.get('error')}")

    status, body = call(base, token, "POST", "/api/rooms", {"name": "a room of its own"})
    check("cannot create a room", status >= 400, f"{status} {body.get('error')}")

    print("\n=== 3. But it can still do the work it exists for ===")
    status, created = call(
        base,
        seat_token,
        "POST",
        f"/api/rooms/{room_id}/tasks",
        {"title": "Runtime credential verification", "description": "Created by the seat."},
    )
    if status >= 400:
        print(f"  could not create a task to work on: {status} {created}")
        return 1
    task_id = created["task"]["id"]

    status, claimed = call(
        base,
        token,
        "POST",
        f"/api/rooms/{room_id}/tasks/claim",
        {"task_id": task_id, "requested_lease_seconds": 300},
    )
    check("can claim", status < 400, f"{status} {claimed.get('error', '')}")
    fence = (claimed.get("task") or {}).get("claim", {}).get("fence")

    status, _ = call(
        base,
        token,
        "POST",
        f"/api/rooms/{room_id}/tasks/checkpoint",
        {"task_id": task_id, "fence": fence, "summary": "Verifying the credential, live."},
    )
    check("can checkpoint", status < 400, str(status))

    print("\n=== 4. Revocation terminates a runtime that is holding a live lease ===")
    # The interesting case, and the one a unit test is least able to establish: the
    # credential is killed *while* it holds work, and the seat is untouched.
    status, _ = call(
        base,
        seat_token,
        "POST",
        f"/api/rooms/{room_id}/credentials/revoke",
        {"credential_id": credential_id, "reason": "verification"},
    )
    check("revocation accepted", status < 400, str(status))

    status, body = call(base, token, "GET", f"/api/rooms/{room_id}/hydrate")
    check("revoked credential is refused", status == 401, f"{status} {body.get('error')}")

    status, body = call(
        base,
        token,
        "POST",
        f"/api/rooms/{room_id}/tasks/complete",
        {"task_id": task_id, "fence": fence, "result": "should never land"},
    )
    check("revoked credential cannot finish its own work", status >= 400, str(status))

    status, still = call(base, seat_token, "GET", f"/api/rooms/{room_id}/hydrate")
    check("the seat still works", status < 400, str(status))
    check(
        "and still holds the lease the runtime took",
        any(lease["task_id"] == task_id for lease in still.get("your_leases", [])),
        "the lease belongs to the seat, not the credential",
    )

    # Leave nothing held.
    call(
        base,
        seat_token,
        "POST",
        f"/api/rooms/{room_id}/tasks/release",
        {"task_id": task_id, "fence": fence, "note": "verification complete", "force": True,
         "reason": "verification cleanup"},
    )
    call(base, seat_token, "POST", f"/api/rooms/{room_id}/tasks/cancel",
         {"task_id": task_id, "reason": "verification complete"})

    print()
    if failures:
        print(f"{len(failures)} check(s) failed: {', '.join(failures)}")
        return 1
    print("All checks passed against", base)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
