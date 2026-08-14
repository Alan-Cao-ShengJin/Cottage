# DECISIONS — Agent Rooms

Append-only. Newest at the bottom. Never edit or delete an entry; supersede it with a new one.

---

## D-001 — Pivot from "temporary chat room for paid agents" to Agent Rooms
**Date:** 2026-08-14 · **Status:** accepted

The prior implementation (commit `ba2e94c`) was a temporary chat room in which the *server* ran
OpenAI-backed agents that took conversational turns, constrained by relevance scoring and turn
budgets. The product is now a provider-neutral live coordination network for agents that users
already own and pay for.

**Consequences.** Server-side inference is removed entirely, along with the `openai` dependency, the
prompt builder, and the conversational guardrails. Chat stops being the primary surface; live work
awareness and coordination take its place. The domain model grows the things coordination actually
needs — orgs/users/identities, invitations, presence with liveness grading, a sequenced event log,
task leases, provenance-stamped state — none of which V0 had.

---

## D-002 — Reuse/replace verdict on the V0 codebase
**Date:** 2026-08-14 · **Status:** accepted

Audited every file before changing anything, and committed V0 first so nothing is unrecoverable.

**Reused:** FastAPI + `aiosqlite` + SSE transport plumbing; the MCP streamable-HTTP mount into the
same ASGI app (one process to run, one service layer underneath); the thin non-ORM async DB
accessor; the typed-domain-error → HTTP mapping; the pytest fixture that gives each test a throwaway
SQLite file; the Next.js 15 / React 19 frontend shell; the pub/sub *seam* in `events.py`.

**Replaced:** `agents/` (server-side OpenAI turn loop), `agents/prompts.py`, `services/guardrails.py`
— these encode the abandoned product. `services/rooms.py` — chat-transcript-centric, single-tenant,
no identity model. `SharedMemoryData`'s free-text lists — replaced by keyed state with provenance and
CAS. The task model — no leases, no dependencies, no proposals. `events.py`'s unsequenced events —
without a per-room `seq` reconnect/replay is impossible, which is a product requirement, so the hub
is rebuilt as notify-then-read over a sequenced log.

**Rejected alternative:** rewriting in TypeScript for stack uniformity with the frontend. It would
discard working transport infrastructure for no product gain. See ADR-001.

---

## D-003 — The room event log is the system of record
**Date:** 2026-08-14 · **Status:** accepted

Reconnect/replay, the audit trail, and conflict detection are all explicit product requirements. All
three fall out of an ordered append-only log; all three are bespoke work under a
mutable-state-plus-notifications design. So: every mutation appends an event with a per-room
monotonic `seq` **in the same transaction**, and every other table is a projection.

**Cost accepted:** no mutation may bypass the log, which constrains how core services are written
and forces a single write path. **Benefit:** a dropped fanout notification can only cost latency,
never data, because consumers re-read the log rather than being fed by the bus (ADR-008).

**Rejected:** global sequencing across rooms (serializes unrelated rooms for no product meaning) and
per-recipient sequence numbers (privacy filtering would make `seq` non-authoritative; instead
recipients legitimately see gaps and are told to expect them).

---

## D-004 — Leases with fence tokens as the exclusivity primitive
**Date:** 2026-08-14 · **Status:** accepted

Participants are independently owned processes that crash, get closed mid-task, and lose network. A
lock without expiry parks work permanently, and a bare TTL lets a revived claimant that already lost
its lease keep writing. So claims are expiring leases carrying a monotonic per-task `fence`, and
every mutation of a claimed task must present the current fence.

Expiry is enforced both lazily on read and by a background reaper, idempotently — so correctness does
not depend on the reaper running on time.

---

## D-005 — Honest capability negotiation instead of synthetic liveness
**Date:** 2026-08-14 · **Status:** accepted

Hosts differ irreducibly: A2A agents can be pushed to, Claude Code can only long-poll (MCP has no
server-initiated wake channel that a client acts on), and ChatGPT-class clients act only when their
human engages. Rather than papering over this, presence carries an explicit liveness grade and
coordination decisions — lease TTL, whether a participant may claim at all, work routing — are
derived from it.

**Rejected:** browser automation to wake consumer AI clients (brittle, ToS-hostile, and it would
make the whole product's reliability contingent on scraping), and reporting long-poll clients as
pushable (would cause other participants to coordinate against a false assumption, which is worse
than a stated limitation).

---

## D-006 — MCP as client adapter, A2A as autonomous-agent adapter, ARP as canon
**Date:** 2026-08-14 · **Status:** accepted

Neither MCP nor A2A can express leases with fencing, provenance-stamped state, or liveness grading.
Adopting either as the internal model would deform the domain and couple our roadmap to theirs.
`ARP` (`docs/PROTOCOL.md`) is canonical; adapters translate inbound to ARP commands and outbound from
ARP events, own no business rules, and hold no state the core needs. Enforced by a layering test:
`core/` and `domain/` may not import `adapters/` or `api/`.

---

## D-007 — M1 slice boundary
**Date:** 2026-08-14 · **Status:** accepted

The first slice is CONNECT → SEE CURRENT WORK → COORDINATE → CLAIM → DISCONNECT, complete and tested
end-to-end across two transports. Shared state, artifacts, proposals/delegation, dependencies,
duplicate/overlap detection, and A2A are explicitly deferred to M2–M4.

Rationale: the differentiating, hardest-to-retrofit mechanics are the sequenced log, resumable
presence, and lease correctness. Building those properly on a narrow surface is worth more than
touching every capability shallowly — and a half-built shared-state layer would have to be redone
once provenance and CAS land for real. The protocol for the deferred pieces is nonetheless specified
now (`docs/PROTOCOL.md §6–8`) so M2/M3 add implementation, not contract.
