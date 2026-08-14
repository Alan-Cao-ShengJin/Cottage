# ROADMAP — Agent Rooms

Ordered milestones. Update **before** implementing and **after** every meaningful phase.

_Last updated: 2026-08-15._

---

## The claim we are building toward

> Anyone starts a room. They invite someone over the internet. Both ends have humans *and*
> agents. Any combination of hosts works — Claude Code ↔ ChatGPT, ChatGPT ↔ ChatGPT, Claude
> Code ↔ Gemini, Claude ↔ Grok, or all four in one room.

Everything below is judged against that sentence. `docs/INTEROP.md` is the accountability
record for it; `docs/DEPLOYMENT_MODES.md` distinguishes the laptop case (**Cottage**) from
the real one (**Hosted**).

---

## Course correction (2026-08-15)

We drifted. Recorded here rather than quietly fixed, because the drift was in *effort
allocation*, and effort allocation is the thing a roadmap is for.

**What went sideways.** Getting one hosted agent (ChatGPT) to reach a laptop consumed four
commits of tunnel plumbing: provider switching, port guards, reachability probes, URL
parsing. Every one of those solved a real bug, and none of them advances the product — they
serve **Cottage**, which is developer convenience. The signal we missed: ChatGPT's own
"Tunnel" feature is ChatGPT-specific, and building around it would have deepened a
single-vendor dependency while the cross-platform claim stayed untested.

**What was not drift**, and should not be re-litigated:

- the core — event log, leases with fencing, capability-derived presence, disclosure
  boundary, conflicts — all provider-neutral and needed for any host;
- **OAuth 2.1** — every hosted agent host needs it. Not ChatGPT-specific;
- the capability model (D-010) — precisely the abstraction that makes "any combination"
  expressible;
- compact tool payloads — context economy applies to every metered host.

**What the drift cost us:** the A2A adapter is still a 5-line placeholder, no second vendor's
client has ever joined a room, and no cross-org invitation has crossed the internet. Those
are the product; they are what M2 is now.

**The rule going in:** before building exposure, name which deployment mode it serves. If the
answer is Cottage, cap the effort.

---

## Current milestone

**M2 — Universal connectivity**

Status: **not started.** M1 is complete (below) and the gate is green.

Why this before shared state: the differentiator is the *cross-platform room*. Deepening a
room whose universality is unproven optimises the wrong axis, and shared state built against
one adapter would need revisiting once three more exist.

### M2.1 — Interop conformance harness
Put N host families in one room simultaneously and assert the six properties in
`docs/INTEROP.md` §3 — including the one that only appears in a mixed room: an
`unattended_loop` participant must never be led to assume an `attended` one is prompt.
Extend `test_three_execution_modes_coexist_with_honest_grades` from execution modes to
adapters. **This lands first**, so every later path has a bar to clear.

### M2.2 — A2A adapter
Agent card publication, inbound delivery, outbound push, untrusted trust tier with vouching,
SSRF-safe egress. Pulled forward from M4: it is how non-MCP agents join, so it is load-bearing
for the claim rather than a later nicety.

### M2.3 — Function-calling join path as first class
`/openapi-gpt.json` exists as a ChatGPT-Action shim. Generalise it: a documented
function-calling surface any host can import, per-agent credentials, and a briefing folded
into the schema description (an Action never gets `get_protocol_briefing`). Reframe as one
path among several rather than a vendor special case.

### M2.4 — Attended-paste path
For a host that cannot call tools at all: a digest read ("what changed, what needs you") a
human pastes in, and a compact command block accepted back. This is the difference between
universal and "universal if your vendor shipped an integration".

### M2.5 — Hosted deployment
Stable hostname, PostgreSQL, container image, real login (OIDC). Removes the tunnel from the
product path entirely and closes the D-011 Postgres blocker. **Cottage tooling is frozen at
this point** — no further investment.

### M2.6 — Cross-org invitation over the internet
Two orgs, two hosts, one room, exercised for real: identity minimisation across the boundary,
`org_internal` refused, untrusted tier applied, audit readable by both sides.

### M2 exit criteria
1. Three host families from **at least two vendors** in one room, each graded honestly.
2. A room created on a Hosted instance, invited by link, joined by a stranger's agent.
3. The conformance harness passes for every path marked `implemented` or better in
   `docs/INTEROP.md` — and every row's status reflects observed reality.
4. `python scripts/check.py` passes.

---

## Completed

### M1 — Vertical slice: CONNECT → SEE CURRENT WORK → COORDINATE → CLAIM → DISCONNECT ✅ (2026-08-14)

All exit criteria met, each pinned by a test. `python scripts/check.py` green.

- Removed the V0 chat-with-paid-agents architecture, including the `openai` dependency; a
  layering test now forbids any provider SDK anywhere in `app/`.
- `domain/` — ids, identity, **capabilities**, room/participant/invitation/connection,
  **disclosure**, work, task, events, commands.
- `db/` — engine-neutral schema + a real transaction boundary + additive-column migrations.
- `core/` — 16 modules: sequenced event log, notify-then-read bus, single write path with
  `command_id` idempotency, scopes + ownership + trust, the modeled disclosure boundary,
  rooms/invitations/join, capability negotiation and presence grading, current work, tasks
  with leases and fence tokens, conflict detection, per-recipient projections.
- `api/` — ARP command surface + resumable SSE stream.
- `adapters/mcp/` — 15 tools, `await_room_events` long-poll, protocol briefing.
- Frontend work board: presence rail with capability chips, current-work cards with
  contested-target highlighting, task board with live lease countdowns, activity feed.
- Tests: 41 protocol invariants, disclosure boundary, layering rules, end-to-end slice.

**Exit criteria:** ✅ two transports in one room seeing each other honestly · ✅ gapless
resume from a stale cursor · ✅ concurrent claim race yields exactly one winner + recorded
conflict · ✅ dead claimant's lease expires and its revival is refused with `stale_fence` ·
✅ graceful disconnect releases claims · ✅ gate green.

### M1.5 — Joining simplified, and real auth for hosted agents ✅ (2026-08-15)

Not a planned milestone; it emerged from trying to connect a hosted agent and is worth
keeping as a unit.

- **One-call room creation.** `room.create` joins the creator as owner and mints a reusable
  join token in the same transaction, ending a bootstrap paradox that both test files had
  been working around by hand (D-013).
- **A seat is `(owner, display_name)`.** One human brings Claude Code *and* Codex *and*
  ChatGPT as three independently graded participants (D-015).
- **`execution_mode` required at join**, no default — `unattended_loop` |
  `human_turn_only` | `observer`. Replaced four capability booleans, because defaults have to
  lean somewhere and over-claiming is the expensive direction (D-014).
- **OAuth 2.1** — RFC 9728 + RFC 8414 discovery, RFC 7591 dynamic registration, mandatory
  PKCE `S256`, single-use codes where a replay revokes what it bought, rotating refresh
  tokens, RFC 8707 audience binding, RFC 7009 revocation, and a consent screen where **a
  human binds the agent identity** so an agent cannot name itself (D-016).
- **Three bugs unit tests could not see**, each found by driving a real client against a real
  server: a cross-task ContextVar that made the principal invisible inside a tool; a spoofed
  display name surviving a correct identity resolution; and `421 Misdirected Request` from the
  MCP SDK's loopback-only Host allowlist (D-017).
- **Compact tool payloads** — `join_room` 3,743 → 359 tokens, `get_room_state` 3,426 → 834,
  a poll 5,067 → 1,465.

**Kept, but reclassified as Cottage tooling and now frozen:** `serve-public.ps1`,
`tunnel.ps1`, `dev.ps1`, and the port/reachability guards.

---

## Next

### M3 — Authorized shared state & artifacts
Shared state with provenance + CAS (`docs/PROTOCOL.md` §6); artifacts with version trees,
divergence detection, explicit resolution (§7); UI panels. Replace the I8 *contract* test with
a behavioral one — it fails deliberately the moment `core/artifacts.py` exists, so it cannot
be forgotten.

### M4 — Task graph depth
Proposals with accept/reject/delegate chains (schema, event types and `_propose_tx` exist;
resolution does not); dependencies and blocking propagation; capability-aware routing.

### M5 — Multi-tenancy & policy hardening
Org admin surfaces, room policies, rate limiting, per-recipient privacy filtering matrix.
Partly absorbed into M2.5, since Hosted requires real accounts.

### M6 — Retention, audit, deletion
TTL expiry and purge with tombstones, event-log truncation with `resume_gap`, audit export.

### M7 — Attended-host experience
Deepen M2.4: richer digests, pasteable turn output, lease tuning for `attended` seats.

---

## Known blockers / open questions

- **No second-vendor client has ever joined a room.** Every "verified" row in
  `docs/INTEROP.md` was verified by our own software. Until that changes, cross-platform is a
  design property, not an observed one. **This is the most important open item.**
- **PostgreSQL compatibility is argued, not demonstrated** (D-011). No invariant depends on
  SQLite locking, but that needs proving: a migration mechanism, a `TEXT` vs `timestamptz`
  review, and the concurrency invariants (I1, I3) run against Postgres. Folded into M2.5.
- **Hosted mode does not exist.** Everything runs on a laptop behind a rotating tunnel, so
  "invite someone over the internet" is currently false in practice.
- **A2A is a 5-line placeholder.**
- **Consent takes a pasted principal token, not a login.** Adequate for Cottage; blocking for
  Hosted.
- **Attended hosts are inherently weak on liveness.** No fix we are willing to build (no
  browser automation — ADR-007). M2.4/M7 mitigate with digests, not synthetic wake-ups.
- **Duplicate detection is lexical only.** Embeddings would require inference we do not pay
  for (ADR-006), so the quality ceiling here is deliberate.
- **Content inspection cannot catch deliberate paraphrase** (D-009). Accepted; the controls
  that work are authorization, privacy classes, provenance, and the audit log.
