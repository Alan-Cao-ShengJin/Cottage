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

_Amended by D-012: duplicate/overlap detection landed in M1 after all, because it fell out of the
target-normalization work for free._

---

## D-008 — Agent Rooms is a coordination orchestrator, not an intelligence orchestrator
**Date:** 2026-08-14 · **Status:** accepted · **Supersedes wording in D-001**

Earlier phrasing ("not an orchestrator that commands agents") understated what the product does. The
room is not passive. It **routes work, proposes and delegates tasks, grants and reclaims exclusive
leases, enforces coordination rules, detects conflicts, and delivers the event stream** — that
orchestration *is* the value.

The boundary is execution autonomy, not coordination authority: **full authority over coordination,
zero authority over execution.** The room never decides how an agent reasons, plans, or which model it
uses, and an agent may reject a proposal, decline to claim, release work, or leave at any time. A lease
is a promise the room enforces about *exclusivity*, not a command it issues about *behavior*.

**Consequences.** `docs/PRODUCT.md` §2.1 enumerates what is orchestrated. Task routing may legitimately
take a participant's negotiated capabilities into account — that is coordination, not intelligence
orchestration. The "not an orchestrator" line is removed from the not-this list, replaced by "not an
*intelligence* orchestrator".

---

## D-009 — Privacy-by-domain-shape is necessary but not sufficient
**Date:** 2026-08-14 · **Status:** accepted

Having no field for a prompt or a key removes accidental leakage paths, and the earlier framing treated
that as the primary control. It is not sufficient: message bodies, task titles and descriptions, work
headlines and notes, target lists, shared-state values, and artifact summaries are all free-form, and
any of them can carry a credential, part of a private file, or another client's context.

So the disclosure boundary is **modeled explicitly**. A content-bearing command carries a `Disclosure`
(privacy class, audience, addressee, claimed source). `core/privacy.check_disclosure` runs three gates —
**authorization**, then **policy**, then **content inspection** — and returns a `DisclosureDecision`
that is stamped onto the event, making what was disclosed, by whom, to whom, and under what class
permanently auditable. Inspection walks nested structures so a secret cannot be buried in a JSON value.

**Rejected:** silent scrubbing (it teaches the caller the channel accepted that content) and
downgrading an `org_internal` payload in a cross-org room (the downgrade performs the very disclosure
the class exists to prevent). Both are hard rejections.

**Limitation recorded honestly:** inspection is a heuristic over free text and cannot catch deliberate
paraphrase. Nothing can. The controls that work against that are authorization, privacy classes,
server-stamped provenance, and the permanent audit log. Inspection exists to stop accidents and
carelessness, which is what actually occurs.

---

## D-010 — Runtime behavior derives from negotiated capabilities, never provider labels
**Date:** 2026-08-14 · **Status:** accepted · **Supersedes the host-class mechanics in D-005**

D-005 was right that hosts differ irreducibly, but it encoded the differences as *host classes* with
per-class lease tables. That bakes a vendor's current limitation ("ChatGPT cannot be woken") into the
architecture, and it is wrong the day that vendor ships a webhook.

Behavior is now a pure function of explicit capability flags — `can_receive_events`,
`can_initiate_followup`, `can_execute_background`, `requires_human_presence`, `supports_push`,
`supports_poll`, `supports_resume`, `supports_tools`, `supports_artifacts` — plus room policy.
`derive_runtime_policy` **takes no host class**, and `tests/test_layering.py` asserts by AST inspection
that it never will. Flags are declared per *connection*, not per identity, because the same agent may
attach from a pushable transport now and a poll-only one later. Negotiation intersects declared flags
with what the transport can genuinely honor, so a client cannot talk itself into a capability the wire
cannot provide.

`host_class` survives as display/telemetry metadata that supplies defaults for a client declaring
nothing. A declaration always wins.

**Consequences.** An `interactive_client`-labeled participant that declares `supports_push` gets push
with no code change; a `native_remote_a2a`-labeled one that declares `requires_human_presence` gets a
short, policy-gated lease. Both are pinned as invariant tests (I9). The `interactive_attached` liveness
grade is renamed `attended`, and room policy `allow_interactive_claims` becomes
`allow_attended_claims` — the property being gated is the capability, not the vendor. Cost accepted:
clients must declare honestly, and one that under-declares gets less than it could have, which is the
correct failure direction.

---

## D-011 — Persistence is replaceable; no invariant may rely on SQLite locking
**Date:** 2026-08-14 · **Status:** accepted · **Refines ADR-001**

SQLite is acceptable for the current milestone, but the temptation in an async single-process service is
to lean on a process-level lock or SQLite's write lock for correctness. That would make the domain
guarantees unportable and, worse, invisible.

Every guarantee is therefore expressed with tools that behave identically on PostgreSQL: UNIQUE
constraints, CHECK constraints, and conditional `UPDATE ... WHERE <expected state>` whose affected-row
count the caller inspects. Specifically — `seq` allocation is `UPDATE rooms SET event_seq = event_seq +
1` then a read inside the mutating transaction, with `(room_id, seq)` as a primary key so a duplicate is
a hard failure; a task claim is one guarded UPDATE where 0 rows means "someone else won" and is reported
as `lease_conflict` rather than retried; command idempotency is a UNIQUE primary key reserved before the
body runs. `BEGIN IMMEDIATE` in `db/database.py` is an adapter detail, documented as such.

**Rejected:** an `asyncio.Lock` around writes. It would have made the concurrent-claim invariant pass
without the domain actually holding it — the worst kind of green test.

**Blocker recorded:** PostgreSQL compatibility must be established before external beta. Remaining work
is a migration mechanism, a `TEXT` vs `timestamptz` review, and running the concurrency invariants
against Postgres. Tracked in `docs/ROADMAP.md`.

---

## D-012 — Idempotent replay of secret-returning commands rotates the secret
**Date:** 2026-08-14 · **Status:** accepted

`command_id` replay returns the original result and appends no event. But `invitation.create` and
`room.join` return a bearer token that is stored only as a hash, so a replay has nothing to return.

Three options were considered: store the plaintext token in the receipt (rejected — a plaintext
credential at rest, to solve a convenience problem); return an error on replay (rejected — makes retry
after an ambiguous timeout unsafe, which is the exact case idempotency exists for); or rotate. **Rotate
wins:** the same authenticated caller is asking again, no duplicate participant/invitation/event is
created, and the caller gets a token that actually works. The previously issued token stops working,
which is documented in `docs/PROTOCOL.md` §2 so a caller holding an outstanding token knows not to
replay these two commands.

Found while writing invariant I3 — the first implementation returned a freshly generated id for an
entity the replayed body never created, which surfaced as `NotFound`. Every command that mints an id now
reads it back from the receipt.

---

## D-013 — Creating a room joins the creator and mints the join token, in one call
**Date:** 2026-08-14 · **Status:** accepted · **Resolves the M1 bootstrap blocker**

`room.create` used to produce a room with nobody in it. Because membership had exactly one entry path
(invitation redemption) and minting an invitation required an admin *participant*, the first
invitation could never be created — a bootstrap paradox. Callers worked around it by inserting the
owner row by hand, including in two of our own test files, which is a reliable signal that the API is
wrong rather than the callers.

`room.create` now does three things in one transaction, emitting `room.created`,
`participant.joined`, and `invitation.created` at seq 1–3: creates the room, joins the creator as
owner, and mints a reusable **default join link** (50 redemptions, 7 days). It returns
`participant_token` (the creator's) and `join_token` (the one thing to share).

Membership still has exactly one entry path for everyone else. The creator is not an exception to the
rule so much as its origin: they are the authority the first invitation derives from, so their row is
created by the same authenticated act that creates the room.

**Consequences.** `_ensure_admin` / `_bootstrap_owner` deleted from both test files — no test bypasses
invitation redemption any more. The MCP adapter gained a `create_room` tool, so an agent host can
create, share, and join without ever touching the browser.

---

## D-014 — Every participant is an agent host; MCP is the universal join path
**Date:** 2026-08-14 · **Status:** accepted · **Refines D-005 / D-010**

Clarified with the product owner: "a human in a browser" means a person using ChatGPT (or similar) in
a browser tab with this server configured as an MCP connector. The human is not a participant — their
*agent* is, and the human drives it. There is therefore no human participant type to support, and our
Next.js UI is a **room console** (mint a room, copy the token, watch the board) rather than a
participation route.

**Consequences.**

1. Joining is one MCP call with one token. Room creation is also available over MCP, so the browser is
   genuinely optional.
2. `join_room` now takes a **required** `execution_mode` (`unattended_loop` | `human_turn_only` |
   `observer`) instead of four capability booleans. **Rejected: booleans with defaults.** Defaults have
   to lean somewhere, and either way half the clients silently mis-declare. A ChatGPT connector left on
   autonomous defaults over-claims — the expensive direction, because other participants then wait on
   work it will never do unprompted. "How do you run?" is a question every client can answer correctly
   about itself, so we ask it.
3. **Bug this surfaced:** a `human_turn_only` client declares `supports_poll`, and grading keyed off
   delivery mode alone, so it came out `live_poll` — telling everyone to expect prompt responses it
   cannot give. Liveness grading now treats mechanism and attendedness as separate facts:
   `requires_human_presence` caps the grade at `attended` regardless of how bytes reach it. The
   delivery mode is *not* downgraded, because a connector that can poll genuinely can poll; what
   changes is what others are told to expect. Pinned by
   `test_three_execution_modes_coexist_with_honest_grades`.
4. `join_room` returns a plain-language `what_this_means`, so a client knows its lease ceiling and why
   — rather than inferring it from flags and guessing wrong.

**Not adopted: requiring MCP and refusing other transports.** What "everyone must have MCP" is really
asking for — every participant is reachable and can do real work — is already enforced by capabilities:
without `supports_tools` and either background execution or an opted-in room policy, a participant
cannot hold a lease, whatever transport it arrived on. Gating on transport instead would be the
provider-label mistake (D-010) in a new costume, and it would break the native ARP client we already
support.

---

## D-015 — One user owns many identities; a seat is `(owner, display_name)`
**Date:** 2026-08-15 · **Status:** accepted

Identity resolution returned a *single* identity per user (`WHERE owner_user_id = ? AND kind =
'human' LIMIT 1`). That silently capped every person at one seat per room, which contradicts the
product's premise: a person brings Claude Code *and* Codex *and* ChatGPT, and each needs its own
presence grade, capability set, and leases.

Worse, it made a second join a **rejoin** of the first seat — rotating the first seat's participant
token away and, before the D-013 role fix, demoting it. A live smoke test hit exactly this: the room
creator added a second participant and lost their own owner session.

Identities are now keyed on `(owner_user_id, display_name)`. Joining under a new name creates a seat;
joining under an existing one is a deliberate rejoin. The MCP adapter's provisioning is get-or-create
on the same key, so an agent that restarts resolves to the same identity instead of littering the room
with ghosts of itself.

**Rejoin semantics, now documented in `docs/PROTOCOL.md` §3 rather than emergent:** same
`participant_id` (ids appear in claims, provenance, and every event, so stability matters); role is
the higher of existing and invited, never lower; a fresh participant token is issued and the previous
one for that seat is invalidated. The last part is intended — losing the token is the usual reason to
rejoin — but it does end any other live session for that seat, so adding a participant means joining
under a different name.

**Rejected: multiple concurrent tokens per participant.** It would avoid invalidating a live session,
but needs a token table, a revocation surface, and an answer for which token a `presence.changed` event
belongs to. Not worth it for a case that has a one-word workaround ("use a different display name").
