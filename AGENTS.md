# AGENTS.md

Entry point for any coding agent working **on** this repository, whichever vendor it comes
from. Cottage's whole claim is that no host is privileged, so its own instructions should
not be addressed to one either.

## Canonical rules live in CLAUDE.md

**Read [`CLAUDE.md`](CLAUDE.md) first and treat it as authoritative.** It holds the project
rules: the claim every change is judged against, the non-negotiable architectural
principles, the security and privacy boundary, the build and test commands, and the working
agreement.

This file deliberately does **not** restate them. Two copies of a rule diverge; one cannot
(D-025). If you find this file disagreeing with `CLAUDE.md`, `CLAUDE.md` wins and the
disagreement is a bug worth fixing.

## The five that bite hardest

Not a summary — the specific traps that have cost real time here.

1. **Every state change appends an event in the same transaction as the mutation.** The
   room event log is the single source of truth; projections are read models. There is no
   correct way to write state without an event.
2. **Adapters translate, they never decide.** `core/` must not import `adapters/` or
   `api/`, and no vendor SDK appears in `core/` or `domain/`. A test enforces it.
3. **Behaviour derives from negotiated capabilities, never from a label.** Not from
   `host_class`, not from a declared runtime role, not from a room role on its own.
   `derive_runtime_policy` takes no host class and a test asserts it never will.
4. **A green gate is not evidence for `adapters/`, `api/oauth.py` or `db/`.** Deploy and
   verify against the live instance too. Three bugs reached production-shaped failure while
   unit tests passed, and a fourth was invisible because the gate runs Python 3.10 while the
   container runs 3.12.
5. **Windows PowerShell 5.1 traps.** `&&` is a parser error; bare `pytest` / `uvicorn`
   resolve to the wrong Python; piping a file into a native command prepends a BOM;
   `Start-Process -ArgumentList` splits on spaces. `CLAUDE.md` has the exact workarounds.

## Before an architectural change

`CLAUDE.md` names the reading order and the end-of-phase checklist. The short version: read
the docs before the code, update `docs/ROADMAP.md` before implementing, run
`scripts/check.py` before committing, and append to `docs/DECISIONS.md` rather than
rewriting history — a superseded decision is recorded as superseded, never deleted.

## If you are an agent participating in a room

Different job, different document. Call `get_protocol_briefing` over MCP, then read
[`docs/COMPANION.md`](docs/COMPANION.md) for what a persistent runtime owes the room and
[`docs/COTTAGE_RUNTIME_ALIGNMENT.md`](docs/COTTAGE_RUNTIME_ALIGNMENT.md) for how durable
room direction reaches a runtime the room does not own.
