# ROADMAP — Agent Rooms

Ordered milestones. Update this file **before** implementing and **after** every meaningful phase.

_Last updated: 2026-08-14_

## Current milestone

**M2 — Authorized shared state & artifacts**

Status: **not started** — M1 is complete and the gate is green.

## Completed

### M1 — Vertical slice: CONNECT → SEE CURRENT WORK → COORDINATE → CLAIM → DISCONNECT ✅ (2026-08-14)

All exit criteria met; `python scripts/check.py` passes (97 tests, mypy, ruff, ruff format, tsc).

- [x] Removed conflicting V0 architecture: `agents/` (server-side OpenAI loop), `prompts.py`,
      `guardrails.py`, chat-centric room service, `openai` dependency (uninstalled; a layering test
      now forbids any provider SDK anywhere in `app/`).
- [x] `domain/` — ids, identity, **capabilities**, room/participant/invitation/connection,
      **disclosure**, work, task, events, commands.
- [x] `db/` — engine-neutral schema (orgs, users, agent identities, principal tokens, rooms,
      invitations, participants, connections, room_events, command_receipts, messages,
      work_declarations, tasks, dependencies, proposals, conflicts, tombstones) + a real
      transaction boundary.
- [x] `core/eventlog.py` — transactional `seq` allocation, `append`, `read_since`, cursor
      validation with `invalid_cursor` / `resume_gap`.
- [x] `core/bus.py` — notify-then-read fanout, `wait_for`.
- [x] `core/dispatch.py` — the single write path: `command_id` reservation, one transaction,
      publish only after commit.
- [x] `core/authz.py` + `core/privacy.py` — scopes, separate ownership checks, trust clamping, and
      the modeled disclosure boundary (authorization → policy → inspection).
- [x] `core/rooms.py` — create room, invitations, redeem/join, leave, close, TTL janitor.
- [x] `core/presence.py` — capability negotiation, connect, heartbeat, liveness grading, dead-
      connection reaper.
- [x] `core/work.py` — declare/update/end current work, presence-derived staleness.
- [x] `core/tasks.py` — create, claim (lease + fence), renew, release, complete, cancel, expiry
      reaper, `claim_race` conflict.
- [x] `core/conflicts.py` — duplicate-task and overlapping-work detection (pulled forward from M3;
      it fell out of target normalization for free), advisory and non-blocking.
- [x] `core/projections.py` — snapshot read model, `snapshot_seq` read in the same transaction as
      its content, per-recipient privacy filtering.
- [x] `api/` — ARP command surface + resumable SSE stream.
- [x] `adapters/mcp/` — 13 tools mapping onto ARP, incl. `await_room_events` long-poll and a
      protocol briefing.
- [x] Frontend work board: presence rail with capability chips, current-work cards with contested-
      target highlighting, task board with live lease countdowns, activity feed.
- [x] Tests (97): 41 protocol invariants, disclosure boundary, layering/architecture rules,
      end-to-end slice across both transports.
- [x] `scripts/check.py` gate; ruff + mypy config.

**Exit criteria, each pinned by a test:**
1. ✅ SSE and MCP long-poll participants in one room see each other's presence, negotiated
   capabilities, and current work — with honest grades (`live_push` vs `live_poll`).
   → `test_sse_participant_and_mcp_participant_see_each_other`
2. ✅ Reconnect from a stale cursor delivers exactly the missed events, ordered, no gaps.
   → `test_i4_reconnect_sees_every_authorized_event_it_missed`, `test_sse_resume_from_a_cursor_*`
3. ✅ Concurrent claim race yields exactly one winner; losers get `lease_conflict`; a `claim_race`
   conflict is recorded. → `test_i1_concurrent_claims_yield_exactly_one_winner`
4. ✅ A dead claimant's lease expires, the task reopens with `task.claim_expired`, and the revived
   claimant is refused with `stale_fence`. → `test_i7_*`, `test_i2_stale_fence_cannot_mutate_after_takeover`
5. ✅ Graceful disconnect releases claims and ends work declarations, and revokes the token.
   → `test_i7_graceful_leave_releases_immediately`
6. ✅ `python scripts/check.py` passes.

### M0 — Pivot checkpoint & project harness ✅ (2026-08-14)
- V0 (chat-centric, OpenAI-driven) committed as `ba2e94c` so reusable pieces stay in history.
- Repo audited; reuse/replace decisions recorded in `docs/DECISIONS.md` (D-002).
- `CLAUDE.md` + `docs/{PRODUCT,ARCHITECTURE,PROTOCOL,SECURITY,ROADMAP,DECISIONS}.md` written.
- Four corrections applied before the domain hardened, recorded as D-008…D-011: coordination-
  orchestrator framing, disclosure boundary over domain shape, capabilities over provider labels,
  persistence portability.

## Next

### M2 — Authorized shared state & artifacts (next up)
Shared state with provenance + CAS (`docs/PROTOCOL.md §6`); artifacts with version trees,
divergence detection, explicit resolution (§7); UI state/artifact panels.

First tasks, in order:
1. `core/state.py` — `state.set` with required provenance and CAS on `expected_revision`; no
   last-writer-wins path. `state_cas_failure` conflict on collision. The command contract
   (`SetStateCommand`) already exists.
2. `core/artifacts.py` — version tree, fast-forward vs divergence, `artifact.resolve_divergence`.
3. Replace the I8 *contract* test
   (`test_i8_artifact_divergence_contract_is_specified_and_not_yet_implemented`) with a behavioral
   one: a divergent publish is accepted, does **not** move head, and raises
   `artifact.divergence_detected`. That test fails deliberately the moment `core/artifacts.py`
   exists, so it cannot be forgotten.
4. Frontend: shared-state panel showing provenance and `unverified` labelling; artifact version graph.

### M3 — Task graph depth
Proposals with accept/reject/delegate chains — the schema, event types, and `_propose_tx` already
exist; resolution does not. Dependencies and blocking propagation; priority and capability-aware
routing. Duplicate and overlapping-work detection (§8) already landed in M1.

### M4 — A2A adapter
Agent card publication, inbound delivery, outbound push, untrusted-agent trust tiers and vouching,
SSRF-safe egress.

### M5 — Multi-tenancy & policy hardening
Real auth (OIDC), org admin surfaces, cross-org invitation flows, room policies, rate limiting,
per-recipient privacy filtering test matrix.

### M6 — Retention, audit, deletion
TTL expiry and purge with tombstones, event-log truncation with `resume_gap`, audit export.

### M7 — Interactive-client experience
The digest read for ChatGPT-class hosts ("what changed and what needs you"), pasteable turn output,
interactive lease policy tuning.

## Known blockers / open questions

- **PostgreSQL compatibility must be established before external beta** (D-011). No invariant
  depends on SQLite locking today, but that is currently argued rather than demonstrated. Needs: a
  migration mechanism, a `TEXT` vs `timestamptz` review, and the concurrency invariants (I1, I3) run
  against Postgres.
- **Auth is dev-only in M1.** `DEV_BOOTSTRAP_TOKEN` stands in for OIDC. Real identity federation is
  M5; nothing in `core/` may assume the dev shape.
- ~~**The room creator is not automatically a participant.**~~ **Resolved (D-013).** `room.create`
  now joins the creator as owner and mints the default join token in the same transaction. The
  hand-seeded owner row is gone from both test files and the MCP adapter gained `create_room`, so an
  agent host never needs the browser.
- **SSE carries the participant token as a query parameter**, because `EventSource` cannot set
  headers. Room-scoped and revoked on leave, but it reaches access logs. A cookie-based stream
  session is an M5 item. Lower priority than it looks: SSE is the console's transport, and every
  agent uses MCP with header auth.
- **Single-process bus.** Horizontal scale needs the `core/bus.py` broker seam (ADR-008). Not a
  blocker for M1–M4.
- **Interactive-host liveness is inherently weak** for any host that declares
  `requires_human_presence`. No fix exists that we are willing to build (no browser automation —
  ADR-007). M7 mitigates via digests, not synthetic wake-ups.
- **Duplicate detection is lexical only** — normalized title match, plus containment when a target is
  shared. Embedding similarity would require inference we do not pay for (ADR-006), so the ceiling on
  quality here is deliberate. Revisit only if false negatives prove costly in practice.
- **Content inspection cannot catch deliberate paraphrase** (D-009). Accepted limitation; the
  controls that work against it are authorization, privacy classes, provenance, and the audit log.
