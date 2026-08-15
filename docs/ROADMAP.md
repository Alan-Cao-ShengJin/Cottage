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

Status: **in progress.** M2.0 and M2.0b are done and verified live; M2.1 is next. The claim
"anyone starts a room and invites someone over the internet" is true for the first time — a
stranger with only a join token can join `agent-rooms.fly.dev` and do work. What remains
unproven is the *cross-vendor* half: every client that has ever joined was ours.

Why this before shared state: the differentiator is the *cross-platform room*. Deepening a
room whose universality is unproven optimises the wrong axis, and shared state built against
one adapter would need revisiting once three more exist.

### M2.0 — Hosted-lite: a stable URL, today ✅ (2026-08-15)
Everything already built is unreachable by a stranger, which makes the central claim false in
practice regardless of how good the core is. So this lands first, scoped for speed rather than
scale (D-020):

- one portable container image — Node stage builds the console to static files, Python stage
  serves both the API and the console from a single origin;
- SQLite on a mounted volume, `DATABASE_PATH` pointed at it;
- `/healthz` for platform health checks;
- `fly.toml` as the concrete fast path, with the image kept host-agnostic so Railway, Render,
  or a plain Docker VPS work identically;
- `docs/DEPLOY.md`: exact commands, and the honest limits.

**Deliberately not in scope:** PostgreSQL, OIDC login, horizontal scale. Each is a scale
concern, and none is needed for a stranger's agent to join a room. The invitee path already
requires no account — an invitation token is the credential. Deferred to M5 (below) and
reachable without redesign, because every invariant is already engine-neutral (D-011).

**Honest limits this ships with**, stated in `docs/DEPLOYMENT_MODES.md` rather than
discovered: one instance only (SQLite on a volume does not survive horizontal scale-out), and
one operator (rooms are created by whoever holds the instance's owner credential; anyone can
be *invited*).

**Where it stands: done and live.** `agent-rooms.fly.dev`, region `sin`, deployed 2026-08-15.
Verified over the public internet — `/healthz` honest about its own configuration, the console
and the API on one origin, the full OAuth 2.1 + MCP flow green including PKCE rejection and
code-replay revocation, a room created by the operator, and an idempotent `command_id` replay
returning the same room. `docs/DEPLOY.md` §0 lists the observations; `docs/INTEROP.md` §0 marks
Hosted-lite `verified`.

The first deploy **failed**, which is the useful part: Python 3.12 in the container enforces
sqlite3's same-thread check on the `isolation_level` setter and Python 3.10 in the dev venv does
not, so `aiosqlite`'s worker thread made 179 green local tests meaningless for that line
(D-022). Deploying is now part of how this project verifies things, not the last step after it.

Also resolved here, because a deploy guide would otherwise have contradicted the code:
`DEV_BOOTSTRAP*` became `BOOTSTRAP_OPERATOR` / `OPERATOR_TOKEN`. The old name asserted "never
enable in a deployed environment" while the deploy path requires exactly that. The real rule
was never about environment but about secrecy — a published default token must not guard a
reachable instance — and `check_public_safety` already enforced it.

### M2.0b — An invitation must be a credential ✅ (2026-08-15)
Deploying M2.0 disproved a claim this roadmap had been making (D-023). An invitation token names
a *room*; it authenticates *nobody*. A public instance must run `MCP_REQUIRE_AUTH=true`, so the
only way through `/mcp` is an OAuth token, and consent requires a principal token that only the
operator holds. Verified against the live instance: the join token is refused as an MCP bearer
(401), at OAuth consent, and on `/api/rooms/join`.

So **no stranger has ever been able to join, on any deployment, by any path** — and every join
we have observed was our own operator's. Until this is fixed, "invite someone over the internet"
is false, and M2.1–M2.6 would all be testing a room only one person can enter.

**Done and verified live** (D-025). `scripts/verify_stranger_join.py` now plays both roles
against the deployed instance — operator, then stranger — and is the standing guard. What
shipped, against the constraints set out below:

- ✅ Presenting a valid invitation authorizes **exactly one** operation — `join_room` for the
  room it names. Verified refused, live: creating a room, listing the org's rooms, reading a
  room it has not joined, and redeeming a *different* room's invitation.
- ✅ The identity it provisions is a **guest**, carrying `provenance=invitation`, and the room
  shows `name_is_self_asserted` beside it. It is `vouched` rather than `untrusted` — someone
  with authority minted the link, and an invited collaborator who cannot claim work is a
  spectator — so what is withheld is the *name's* credibility, not the ability to help.
- ✅ A guest is **not an org member for `org_internal` purposes**, however its org row reads.
  This was the subtle one: a guest is provisioned into the inviting room's org, so a tenancy
  comparison called it a member and `org_internal` payloads would have flowed to a stranger
  holding a link. `authz.can_see_org_internal` now requires account provenance — and the two
  filters that actually gate disclosure were inlining their own tenancy comparison rather than
  calling it, so the predicate fix alone was decorative. A behavioural test caught that.
- ✅ The invitation is accepted directly as a bearer on `/mcp` and on `/api/rooms/join` — "paste
  a URL and a token into your agent", which is what a host that does OAuth badly needs. The
  consent-screen variant was not built: it buys standards tidiness, not reach, and the bearer
  path already makes every MCP client work.
- ✅ Revocation, expiry and exhaustion are all checked at the door, so a dead link never gets
  as far as provisioning anyone. **Refined from the original constraint:** revoking an
  invitation stops future *joins*; it does not eject participants who already joined. Removing
  someone is `participant.removed`, a separate admin act — conflating them would mean revoking
  a 50-use link silently kicks everyone who used it.

### M2.1 — Interop conformance harness ✅ (2026-08-15)
`backend/tests/test_interop_conformance.py` — four join paths in one room (ARP HTTP + SSE,
MCP autonomous, MCP attended, and a stranger holding only an invitation), with all six
properties from `docs/INTEROP.md` §3 asserted across them: mutual visibility with honest
grades, one claim winner refused to every other path, stale fences refused whichever path
presents them, a disconnect freeing work visibly to the rest, one event ordering with gaps
only where privacy explains them, and — the one that cannot appear in a single-path test — an
attended participant never presented as prompt to an autonomous one.

It surfaced something worth keeping: on the bearer-invitation path *every* participant is a
guest with a self-asserted name, because the invitation genuinely is the only authorization.
That is correct and is now asserted rather than assumed away.

The bar every later path must clear: add an adapter, add it to this harness.

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

### M2.5 — ~~Hosted deployment~~ → split (D-020)
The reachable-instance half moved forward to **M2.0** and ships now. The scale half —
PostgreSQL, OIDC login, horizontal scale-out — moved back to **M5**, because neither is needed
for a stranger's agent to join a room, and building them first would delay the only test that
matters. **Cottage tooling is frozen as of M2.0** — no further investment.

### M2.6 — Cross-org invitation over the internet
Two orgs, two hosts, one room, exercised for real: identity minimisation across the boundary,
`org_internal` refused, untrusted tier applied, audit readable by both sides.

### M2 exit criteria
1. Three host families from **at least two vendors** in one room, each graded honestly.
2. A room created on a Hosted instance, invited by link, joined by a stranger's agent.
3. The conformance harness passes for every path marked `implemented` or better in
   `docs/INTEROP.md` — and every row's status reflects observed reality.
4. `python scripts/check.py` passes.

Criterion 2 is the one M2.0 was meant to make possible. M2.0 delivered the *reachable instance*
half of it; the *joined by a stranger's agent* half turned out to be blocked by something else
entirely (D-023), which is M2.0b. Deploying is what revealed that, and it would not have been
visible from a laptop where the permissive local path masks it.

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

### M5 — Scale & multi-tenancy: when scale requires it, not before
Deferred here from M2.5 by D-020, and deliberately demand-driven:

- **PostgreSQL** — closes the D-011 blocker. Needed the moment one instance is not enough, or
  a volume is not durable enough. Every invariant is already engine-neutral, so this is a
  driver swap plus a migration path, not a redesign.
- **OIDC login** — needed the moment more than one person must create rooms on the same
  instance. Until then the operator credential is sufficient and honest.
- **Horizontal scale-out** — blocked on PostgreSQL, since the notify-then-read bus is currently
  in-process. Multi-instance needs the notification to cross processes (LISTEN/NOTIFY or
  equivalent). The design already tolerates a *dropped* notification — it costs latency, not
  data — which is what makes this tractable.
- Org admin surfaces, room policies, rate limiting, per-recipient privacy filtering matrix.

### M6 — Retention, audit, deletion
TTL expiry and purge with tombstones, event-log truncation with `resume_gap`, audit export.

### M7 — Attended-host experience
Deepen M2.4: richer digests, pasteable turn output, lease tuning for `attended` seats.

---

## Known blockers / open questions

- **No second-vendor client has ever joined a room.** Every "verified" row in
  `docs/INTEROP.md` was verified by our own software. Until that changes, cross-platform is a
  design property, not an observed one. **This is now the most important open item**, and the
  two things that used to block it — no reachable instance, no way for a stranger to
  authenticate — are gone.
- **PostgreSQL compatibility is argued, not demonstrated** (D-011). No invariant depends on
  SQLite locking, but that needs proving: a migration mechanism, a `TEXT` vs `timestamptz`
  review, and the concurrency invariants (I1, I3) run against Postgres. Deferred to M5 by
  D-020 — it is a scale blocker, not a launch blocker.
- **A2A is a 5-line placeholder.**
- **Consent takes a pasted principal token, not a login.** This caps Hosted-lite at one
  operator: that person can create rooms, and anyone they invite can join without an account.
  Blocking only when a second person needs to create rooms on the same instance (M5).
- **Hosted-lite is single-instance.** SQLite on a volume plus an in-process bus means one
  machine. Vertical scaling only until M5.
- **The dev venv is Python 3.10; production is 3.12.** This skew already produced one bug that
  a full green gate could not see (D-022), and it will produce more — every `sqlite3`,
  `asyncio`, and typing behaviour change between those versions is untested here. The fix is to
  align the venv to 3.12 and re-run the gate. Cheap, and it converts "tests pass" back into
  evidence about production.
- **`scripts/verify_oauth_flow.py` asserts on payload key names and nothing keeps it honest.**
  It silently rotted when the MCP adapter moved to compact payloads at `4784da5`, and only
  failed once pointed at a real deployment. Standing protection that no gate exercises is
  protection with a half-life.
- **Attended hosts are inherently weak on liveness.** No fix we are willing to build (no
  browser automation — ADR-007). M2.4/M7 mitigate with digests, not synthetic wake-ups.
- **Duplicate detection is lexical only.** Embeddings would require inference we do not pay
  for (ADR-006), so the quality ceiling here is deliberate.
- **Content inspection cannot catch deliberate paraphrase** (D-009). Accepted; the controls
  that work are authorization, privacy classes, provenance, and the audit log.
