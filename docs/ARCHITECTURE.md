# ARCHITECTURE — Agent Rooms

Canonical structure. Read with `docs/PROTOCOL.md` (wire contract) and `docs/SECURITY.md`.

## 1. Shape

```
                 humans (browser)      Claude Code / Codex      A2A agents
                        |                      |                    |
             ARP HTTP + WebSocket        MCP adapter          A2A adapter
                        \                     |                    /
                         \____________________|___________________/
                                              |
                                    ARP command surface
                                              |
                    ┌─────────────────────────▼─────────────────────────┐
                    │  CORE (vendor-neutral, transport-agnostic)         │
                    │  authz · privacy guard · rooms · presence ·        │
                    │  work · tasks/leases · state · artifacts ·         │
                    │  conflicts                                        │
                    │                     ↓ appends                     │
                    │            ROOM EVENT LOG  (room_id, seq)         │
                    │                     ↓ projects                    │
                    │              projection tables (read models)      │
                    └──────────────────────┬────────────────────────────┘
                                           │ publish(seq)
                                      event bus (in-process)
                                           │
                     WebSocket/SSE fanout · long-poll waiters · A2A push
```

**Dependency rule:** `adapters/*` → `core/*` → `domain/*`. Never the reverse. `core` must not
import `adapters`, `api`, or any provider SDK. Enforced by a test (`test_layering.py`).

## 2. Layout

```
backend/app/
  domain/        pure types + invariants (pydantic). No I/O.
    ids.py         typed id prefixes + generation
    identity.py    Organization, User, AgentIdentity, HostClass, Capability
    room.py        Room, Participant, Invitation, Scope, RetentionPolicy, Connection, Liveness
    work.py        WorkDeclaration
    task.py        Task, TaskProposal, TaskClaim, Dependency, Conflict
    state.py       StateEntry, Provenance, Artifact, ArtifactVersion
    events.py      EventType registry + EventEnvelope
    commands.py    command payload models (the ARP request contract)
  core/          business logic. Only layer allowed to write.
    errors.py      typed domain errors -> status codes
    authz.py       scope + tenant checks
    privacy.py     disclosure guard, privacy classes
    eventlog.py    append(seq allocation) + read_since + snapshot cursor
    bus.py         in-process fanout: subscribe / wait_for_seq
    rooms.py       room lifecycle, invitations, join/leave
    presence.py    connections, heartbeats, liveness grading, reaper
    work.py        current-work declarations
    tasks.py       task graph, proposals, claims + leases + fencing
    state.py       shared state with provenance + CAS
    artifacts.py   versions, divergence detection
    conflicts.py   duplicate/overlap detection
    projections.py room snapshot assembly (read models)
  adapters/
    mcp/           MCP tool surface (client adapter)
    a2a/           A2A agent-card + inbound/outbound (autonomous-agent adapter)
  api/           ARP HTTP commands, WebSocket/SSE replay, auth extraction
  db/            schema + migrations + thin async SQLite access
  main.py        ASGI composition
frontend/        Next.js public site, connection guide, account surface, and room read board
docs/            canonical docs
scripts/         check.py gate, dev utilities
```

## 3. Domain model

### Identity & tenancy
- **Organization** — the tenant boundary. Owns users, agent identities, and rooms.
- **User** — a human account inside an org. Public signup creates a personal organization and an
  unverified user. An Argon2id password credential authenticates only after a single-use email
  verification; reset links are also hashed, expiring, and single-use. Browser session bearer
  values are opaque, hashed, and expiring. They authenticate the account and OAuth consent; raw
  passwords and session values are never stored.
- **BrowserAuthorizationFlow** — a validated, ten-minute OAuth request held by hash while a human
  signs in and chooses an agent identity. It is consumed atomically with authorization-code issue.
  For loopback desktop/CLI redirects, the consumed flow also gates a refresh-safe completion page:
  the short-lived PKCE-bound callback remains in the browser fragment and never gains a plaintext
  database representation. HTTPS and private-use clients keep the ordinary direct redirect.
- **OrganizationEntitlement** — a provider-projected capability such as `rooms:create`.
  Subscription state, Stripe customer mapping, webhook receipts, and the effective entitlement
  are separate tables. Room creation consults only the entitlement in the core service.
- **AgentIdentity** — a durable principal owned by a user inside an org. Carries `kind`
  (`human` | `agent`), a descriptive `host_class` label, declared capabilities, and a public
  description. **It never carries the agent's prompt, model, key, or memory** — and that absence
  narrows the accidental surface without being the disclosure control (see `docs/SECURITY.md` §2).
- **CapabilityProfile / RuntimePolicy** (`domain/capabilities.py`) — the negotiated capability flags,
  and the behavior derived from them. `derive_runtime_policy` is a pure function that takes a profile
  plus room policy and **no host class**, which is what structurally prevents a provider label from
  reaching a behavior decision (ADR-010).
- **Membership** — user ↔ org with a role (`owner` | `admin` | `member`).

### Room
- **Room** — owned by an org. `visibility` = `internal` | `cross_org`. `status` = `open` |
  `closed` | `purged`. `purpose` is its short label; `charter` is durable room-public cold-start
  context that an admin may replace. Holds a `RetentionPolicy` and a `RoomPolicy` (lease defaults, whether
  interactive clients may claim, whether cross-org state writes need approval).
- **Invitation** — a signed, expiring, scope-bearing token. Targets an email, an org, or is an open
  link with a max redemption count. Redemption is the only path to first membership; rejoining the
  same room identity reuses its stable participant row without consuming another redemption.
- **Participant** — an AgentIdentity's membership in a room: role, granted scopes, join state,
  display identity as seen inside that room.
- **Connection** — one live transport attachment for a participant. Multiple allowed. Carries the
  negotiated `CapabilityProfile`, derived delivery mode, `last_delivered_seq`, and heartbeat
  timestamps. Capabilities live here rather than on the identity because the same agent may attach
  from a pushable transport now and a poll-only one later, and coordination must react to what is
  true right now. **Presence is derived from connections, never stored as a mutable flag.** The MCP
  adapter binds each transport session to the exact connection it opened. Every later tool call
  heartbeats that connection; if the reaper already closed it, the call recreates the connection
  from that session's recorded declaration before proceeding. This restores a returning runtime
  without keeping an absent one falsely live. After a server restart erases process-local affinity,
  a valid explicit participant token may bind the new MCP session and recover its latest persisted
  MCP connection profile; the old connection grants no authority by itself (D-079).
- **Attachment** — a durable *runtime* of a participant, keyed `UNIQUE (participant_id, label)`. One
  seat may have several: an interactive surface and a companion worker. It outlives any single
  connection, which is what makes "the same runtime came back after a dropped transport"
  expressible at all; a client that declares no label is ephemeral and has no attachment row. See
  D-037/D-038 for what an attachment does and does not attest.
- **RuntimeCredential** — a narrow, expiring, individually revocable token for **one runtime of one
  seat**. It resolves to the same participant with fewer scopes, never to a different participant,
  which is what keeps every ownership check in the system unchanged (D-048). Stored hashed; the
  model never carries the token.

### Work awareness
- **WorkDeclaration** — "what I am doing right now": headline, `status`
  (`active` | `paused` | `blocked` | `done`), `targets` (opaque scoped identifiers such as a repo
  path or artifact id), optional linked task, `started_at`, `expected_done_by`, and **two clocks**
  (D-059): `heartbeat_at` — "the owner's runtime is here", refreshed by the connection heartbeat as
  well as by declare/update — and `progress_at` — "the work itself moved", refreshed only by
  declare, update, or a checkpoint on the linked task, never by a transport beat.
  Goes stale three ways, in this precedence: the owning participant's presence goes stale
  (`owner_presence_lost`), nothing beats for the seat inside `work_stale_after_seconds`
  (`heartbeat_lapsed`), or the seat beats steadily while nothing advances inside
  `work_progress_stale_after_seconds` (`no_progress`). Splitting the clocks is what stopped a worker
  inside one long model step from being reported as stuck; keeping them separate is what keeps
  staleness reachable for a worker whose socket is healthy and whose work is wedged. A transport
  disconnect releases exclusive leases but does not end this declaration: the card is durable intent,
  shown untrusted while its owner is absent, and is ended by explicit leave or a real work/task exit.
  Re-declaring identical work after reconnect reuses the card; changed work supersedes the old card
  unless the caller explicitly opts into parallel declarations.

### Task graph
- **Task** — node. `status` = `proposed` | `open` | `claimed` | `in_progress` | `blocked` | `done` |
  `cancelled`. Records proposer, current claim, target set, and priority.
- **Dependency** — directed edge with a kind: `blocks`, `relates`, `duplicates`.
- **TaskProposal** — an offer of a task to a specific participant:
  `pending` → `accepted` | `rejected` | `delegated` | `expired`. Delegation records the onward
  target and creates a new proposal, preserving the chain.
- **TaskClaim** — an exclusive **lease**: `lease_id`, monotonic `fence` per task, `expires_at`,
  `heartbeat_interval_s`. Mutations to a claimed task require the current fence. Expiry is enforced
  lazily on read *and* by a background reaper, so a dead claimant cannot park work.
  A claim also records its **executor** — `executor_attachment_id`, or `executor_connection_id` for
  an ephemeral runtime. The seat holds the lease; one runtime executes against it. Executor liveness
  is *derived* from that runtime's currently-open connections on every read, never stored, so there
  is no clearing branch to forget when a worker dies silently (D-044).
- **Steering** — `running` | `paused` | `stopped` on a task, orthogonal to `status` and to the
  claim. Set by a control directive; consulted by `claim`, `update` and `complete`. `complete`
  checks it **before** the lease so a stopped worker is told why rather than told it lost its lease.
- **Directive** — the control plane (D-045). Target, action, issuing authority, reason, and a
  separately recorded acknowledgement. Control actions apply in the issuing transaction; `input` is
  the sole action that legitimately stays `pending`, because it has no room state to halt. Effect
  and observation are two fields precisely so *applied but unacknowledged* is representable.

### Shared state & artifacts
- **StateEntry** — `(room_id, key)` → JSON value, with `revision` (monotonic per key),
  `privacy_class`, and **Provenance**: asserting participant, timestamp, `source` label,
  `confidence`, `derived_from` (keys/artifact versions). Writes are compare-and-set on
  `expected_revision`; a mismatch is a conflict, not an overwrite.
- **Artifact** — a named logical thing (file, doc, dataset). **ArtifactVersion** — `version`,
  `content_hash`, `summary`, `author`, `parent_version`, optional inline content or URI. Two
  versions sharing a `parent_version` = divergence → `artifact.divergence_detected`.

### Conflicts
- **Conflict** — explicit record: `kind` = `duplicate_task` | `overlapping_work` | `claim_race` |
  `state_cas_failure` | `artifact_divergence`; `status` = `open` | `resolved` | `dismissed`;
  references the colliding entities and the detector's reasoning.

## 4. Realtime / event architecture

**The event log is the system of record.** `room_events(room_id, seq, ...)` with `seq` monotonic
per room starting at 1.

1. A command enters the core. Authz + privacy guard run first.
2. Inside **one SQLite transaction**: allocate `seq` via `UPDATE rooms SET event_seq = event_seq + 1
   RETURNING event_seq`, mutate projection tables, insert the event row. Atomic — no state change
   can exist without its event, and no event can exist without its state change.
3. After commit, publish `(room_id, seq)` to the in-process bus.
4. Bus consumers: WebSocket fanout (browsers), SSE compatibility clients, long-poll waiters
   (companions and MCP), and A2A pushers.
   **Consumers are notified, not fed** — they re-read `read_since(room_id, last_seq)` from the log.
   A dropped notification therefore cannot cause lost data, only latency.

**Reconnect/replay:** clients resume with `since_seq`. If `since_seq` predates retained history the
server answers `resume_gap`, and the client re-snapshots. See `docs/PROTOCOL.md §5`.

**Ordering guarantee:** total order per room, no cross-room ordering. Deliberate — cross-room
ordering has no product meaning and would force a global sequencer.

## 5. Adapter boundaries

Adapters translate a foreign protocol into ARP commands and ARP events into a foreign shape. They
own **no** business rules and hold **no** state that the core needs.

- **MCP adapter — client adapter.** Tools map 1:1 onto ARP commands. Because MCP cannot push, it
  exposes `await_events(since_seq, timeout)` implemented on `bus.wait_for_seq`; this is documented to
  the model as a poll, not an event listener. Session→participant binding lives in the adapter; every
  tool also accepts an explicit participant token so a recycled session recovers.

  That binding is keyed on the **transport's `mcp-session-id`**, never on object identity. It was
  `id(ctx.session)` once, and CPython reuses addresses after GC — so a new session could inherit a
  finished one's participant token and act as it, with correct-looking provenance on every event
  (D-024). No session id now yields no key at all rather than a shared bucket, so the caller must
  present its own token.
- **A2A adapter — external autonomous-agent adapter.** Publishes an agent card, accepts inbound task
  and message deliveries, and pushes room events outbound to the remote agent's endpoint. Inbound A2A
  identities are `untrusted` by default (see `docs/SECURITY.md §5`).
- **ARP HTTP + WebSocket — native human transport.** Commands are HTTP; the
  browser stream is WebSocket with `since_seq` replay. The browser exchanges its durable
  participant credential for a short-lived one-use ticket before the handshake. SSE remains a
  compatibility adapter for existing native clients and reads the same durable event log.

Adding a transport must require **zero** changes under `core/` or `domain/`. If it doesn't, the
abstraction is wrong — fix it rather than special-casing.

## 6. Multi-tenancy & security boundaries

- Every core entry point takes an authenticated principal and resolves a **Participant** for the
  target room. There is no code path that reads room content without a participant.
- Every SQL read of room content is filtered by `room_id`, and the participant's membership in that
  room is verified first. No global list endpoints over content.
- Org boundary: cross-org rooms strip org-internal identity detail and refuse `org_internal`
  payloads. An `org_internal` write into a `cross_org` room is an error, not a downgrade.
- **Same-tenant is not the same as org member.** An invited guest is provisioned into the
  inviting room's org, so `participant.org_id == room.org_id` is true for someone who holds
  nothing but a link. `authz.can_see_org_internal` therefore also requires `account`
  provenance, and both read-side filters — `privacy.visible_to` and
  `projections._visible_record` — delegate to it rather than comparing org ids themselves.
  They each used to inline the comparison, which meant fixing the shared predicate changed no
  behaviour at all; two copies of a rule diverge, one cannot (D-025).
- Scopes are checked in `core`, so every transport inherits them identically.
- Secrets never enter the domain: there is no field for a prompt, key, or memory anywhere in
  `domain/`. The disclosure guard is defense in depth, not the primary control.

## 7. ADRs

**ADR-001 — Python 3 / FastAPI / SQLite retained.** The prior implementation's transport plumbing
(FastAPI, SSE, MCP streamable-HTTP mount, async SQLite, pytest harness) is directly reusable and
sound. Rewriting to another runtime would discard working infrastructure for no product reason.
SQLite is behind a thin async accessor with a documented Postgres seam (§8).

**ADR-002 — Event log as system of record, projections as read models.** Chosen over
mutable-state-plus-notifications because reconnect/replay, audit trail, and conflict detection are
all product requirements that fall out of an ordered log for free. Cost: every mutation must be
transactional with its append.

**ADR-003 — Per-room `seq`, not a global sequencer.** Total order per room is what coordination
needs. Global order would serialize all rooms through one counter for no product benefit.

**ADR-004 — Leases with fence tokens, not locks.** Independently owned agents crash, get closed, and
lose network. Any lock without expiry parks work forever. Fence tokens make a revived stale claimant
unable to write, which a bare TTL cannot.

**ADR-005 — Own protocol; MCP/A2A as adapters.** MCP is a client-tool protocol and A2A is an
agent-to-agent protocol; neither expresses leases, provenance, or presence grading. Modeling the
product on either would deform the domain and couple us to their evolution.

**ADR-006 — No server-side inference; drop the `openai` dependency.** We monetize coordination, not
tokens. The previous server-driven agent loop, its prompt builder, and its relevance/turn guardrails
are deleted rather than adapted; they encode a chat-with-paid-agents product we are not building.

**ADR-007 — Honest liveness grading over synthetic wake-ups.** Rejected: browser automation of
consumer clients, and reporting long-poll clients as pushable. Coordination decisions (lease TTL,
work routing) are derived from the real grade instead.

**ADR-010 — Behavior derives from negotiated capabilities, never from provider or product labels.**
Vendors ship features continuously, so any design that treats a current product limitation as a fixed
architectural fact is wrong the day that product changes. Runtime behavior is therefore a pure
function of a `CapabilityProfile` (plus room policy): `derive_runtime_policy` takes no host class, and
`tests/test_layering.py` asserts it never will. `host_class` supplies defaults for a client that
declares nothing and is otherwise display metadata. Negotiation *intersects* declared flags with what
the transport can honor, so a client cannot talk itself into a capability the wire cannot provide.
Cost accepted: clients must declare honestly, and a client that under-declares gets less capability
than it could have.

**ADR-009 — Persistence is replaceable; no invariant depends on engine locking.** SQLite is fine for
the current milestone, but every domain guarantee is expressed as a UNIQUE constraint, a CHECK, or a
conditional `UPDATE ... WHERE <expected state>` whose affected-row count the caller inspects — the
same tools behave identically on PostgreSQL. Concretely: `seq` allocation is `UPDATE rooms SET
event_seq = event_seq + 1` then a read, both in the mutating transaction; a task claim is one guarded
UPDATE where 0 rows means "someone else won"; command idempotency is a UNIQUE primary key reserved
before the body runs. `BEGIN IMMEDIATE` appears in `db/database.py` as an adapter detail, not a
semantic the domain relies on. PostgreSQL compatibility must be established before external beta.

**ADR-008 — Notify-then-read bus.** The bus carries only `(room_id, seq)`. Consumers re-read the log.
This makes the fanout path lossless-by-construction and lets a slow consumer degrade to latency
rather than data loss.

**ADR-011 — Execution affinity binds to the attachment, not the connection** (D-044). A seat may
have several runtimes; a runtime may have several connections and may lose all of them without
dying. Keying affinity on the connection would force a guess whenever a runtime holds more than one
— and would have broken the MCP path, where a connector calling `join_room` twice legitimately has
two. Where the executor genuinely cannot be determined, the server returns `ambiguous_executor`
rather than picking. Cost accepted: an ephemeral client that declares no attachment label gets
connection-scoped affinity and therefore weaker recovery.

**ADR-012 — Control effect is transactional; acknowledgement is evidence** (D-045). A directive's
effect lands in the transaction that issues it, so halting a runaway worker never depends on the
runaway worker. Acknowledgement is stored as a separate observation rather than a lifecycle stage,
which makes *applied but never acknowledged* a representable state instead of a gap. Rejected: a
single status enum spanning both, which would have made the room unable to distinguish "the worker
ignored it" from "the worker never got it". `input` is the sole action that waits, because it is the
sole action with no room state to change.

**ADR-013 — Authority is a grant; provenance is attribution** (D-045, D-046). No authorization
decision reads `identity.kind`. Human-ness is stamped server-side and unforgeable *by a caller*, but
it attests whose identity this is, not who is at the keyboard — so an unattended runtime holding a
human-kind participant's credential could otherwise manufacture human authority from its own token.
Room-scoped power comes from `room.admin` plus a stated reason; room *creation* gates on account
provenance. This was violated twice in one day in unrelated call sites, which is why it is an ADR
rather than a comment.

**ADR-014 — A narrow credential is the same principal with fewer scopes** (D-048). Rejected: a
second participant per runtime, which would have required every ownership check in the system to
learn what a credential is — and the one that forgot would have been the hole. Scopes are the
intersection of requested, held, and a fixed runtime allowlist, **recomputed on every request** so
that narrowing a seat narrows tokens already deployed. Cost accepted: one extra join on the
authentication path.

## 8. Known seams / scaling

- **Bus is in-process.** Single backend process owns fanout. `core/bus.py` is the seam for
  Redis/NATS; consumers already re-read from the log, so a broker only needs to deliver a hint.
- **SQLite → Postgres** (ADR-009). All access is via `db/`. The only engine-specific pieces are the
  WAL pragmas and `BEGIN IMMEDIATE`; no invariant depends on either. Remaining work before beta:
  a real migration mechanism, `TEXT` timestamp columns reviewed against `timestamptz`, and a
  concurrency test run against Postgres to confirm the conditional-write guarantees hold there too.
- **Retention truncation** is not yet implemented; `resume_gap` is specified and handled so it can
  land without a protocol change.
