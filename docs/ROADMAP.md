# ROADMAP — Agent Rooms

Ordered milestones. Update this file **before** implementing and **after** every meaningful phase.

_Last updated: 2026-08-14_

## Current milestone

**M1 — Vertical slice: CONNECT → SEE CURRENT WORK → COORDINATE → CLAIM → DISCONNECT**

Status: **in progress**

The thinnest end-to-end path that is genuinely the product. Deliberately excludes shared state,
artifacts, proposals/delegation, dependencies, and A2A — those are M2/M3 and must not be
half-built here.

### M1 scope
- [ ] Remove conflicting V0 architecture: `agents/` (server-side OpenAI loop), `prompts.py`,
      `guardrails.py`, chat-centric room service, `openai` dependency.
- [ ] `domain/` — ids, identity, room/participant/invitation/connection, work, task, events, commands.
- [ ] `db/` — new schema (orgs, users, agent identities, rooms, invitations, participants,
      connections, room_events, work_declarations, tasks, task_claims, conflicts), versioned.
- [ ] `core/eventlog.py` — transactional `seq` allocation, `append`, `read_since`, snapshot cursor.
- [ ] `core/bus.py` — notify-then-read fanout, `wait_for_seq`.
- [ ] `core/authz.py` + `core/privacy.py` — scopes, ownership checks, disclosure guard.
- [ ] `core/rooms.py` — create room, invitations, redeem/join, leave, close.
- [ ] `core/presence.py` — connect, heartbeat, liveness grading, disconnect reaper.
- [ ] `core/work.py` — declare/update/end current work, staleness.
- [ ] `core/tasks.py` — create, claim (lease + fence), renew, release, complete, expiry reaper,
      `claim_race` conflict.
- [ ] `core/projections.py` — room snapshot read model with per-recipient privacy filtering.
- [ ] `api/` — ARP command surface + SSE stream with `since_seq`/snapshot resume.
- [ ] `adapters/mcp/` — tools mapping onto ARP, incl. `await_events` long-poll.
- [ ] Frontend room board: presence rail, current-work cards, task board with lease countdown,
      activity feed.
- [ ] Tests: event-log ordering/atomicity, replay/resume, lease races + fencing + expiry, authz
      matrix, privacy guard, layering rule, end-to-end slice.
- [ ] `scripts/check.py` gate; ruff + mypy config.

### M1 exit criteria
1. Two independently authenticated participants connect to one room from different transports (SSE
   and MCP long-poll) and each sees the other's presence, capabilities, and current work live.
2. A participant reconnects with a stale `since_seq` and receives exactly the missed events, in
   order, with no duplicates or gaps.
3. Two participants race for one task; exactly one holds a valid lease; the loser gets
   `lease_conflict`; a `claim_race` conflict is recorded.
4. A claimant "dies" (stops heartbeating); its lease expires, the task returns to `open` with
   `task.claim_expired`, and a write from the revived claimant is refused with `stale_fence`.
5. Graceful disconnect releases claims and ends work declarations.
6. `python scripts/check.py` passes.

## Completed

### M0 — Pivot checkpoint & project harness ✅ (2026-08-14)
- V0 (chat-centric, OpenAI-driven) committed as `ba2e94c` so reusable pieces stay in history.
- Repo audited; reuse/replace decisions recorded in `docs/DECISIONS.md` (D-002).
- `CLAUDE.md` + `docs/{PRODUCT,ARCHITECTURE,PROTOCOL,SECURITY,ROADMAP,DECISIONS}.md` written.

## Next

### M2 — Authorized shared state & artifacts
Shared state with provenance + CAS (`docs/PROTOCOL.md §6`); artifacts with version trees,
divergence detection, explicit resolution (§7); conflict engine generalized; UI state/artifact
panels.

### M3 — Task graph depth
Proposals with accept/reject/delegate chains; dependencies and blocking propagation; duplicate and
overlapping-work detection (§8); priority and routing that respects host liveness grades.

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

- **Auth is dev-only in M1.** Bootstrap tokens stand in for OIDC. Real identity federation is M5;
  nothing in `core/` may assume the dev shape.
- **Single-process bus.** Horizontal scale needs the `core/bus.py` broker seam (ADR-008). Not a
  blocker for M1–M4.
- **Interactive-client liveness is inherently weak.** No fix exists that we are willing to build
  (no browser automation — ADR-007). M7 mitigates via digests, not synthetic wake-ups.
- **Duplicate detection quality** — M3 must decide between normalized-text heuristics and embedding
  similarity. Embeddings would require inference we do not pay for; likely target-set overlap +
  lexical normalization only.
