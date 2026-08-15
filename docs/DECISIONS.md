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

_Superseded in part by D-016: with OAuth in place, an authenticated client's identity comes
from its token and the self-chosen `display_name` is ignored._

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

---

## D-016 — OAuth 2.1 with human-bound identity is the connection path for hosted agents
**Date:** 2026-08-15 — **Status:** accepted — **Pulls M5 identity work forward**

ChatGPT's custom-plugin dialog takes a server URL and defaults to **OAuth**, discovering
configuration from the MCP endpoint. So the MCP authorization spec is not a hardening option
we chose — it is the only way a hosted client can attach. It also happened to be exactly the
"real auth before exposure" the product owner asked for, so the two requirements collapsed
into one piece of work.

Implemented: RFC 9728 protected-resource metadata, RFC 8414 authorization-server metadata,
RFC 7591 dynamic registration (public clients, no secret), authorization code + PKCE `S256`
only, refresh rotation, RFC 8707 audience binding, RFC 7009 revocation.

**The decision that matters most is where identity comes from.** The authorization code
carries an `agent_identity_id` a *human* selected at the consent screen, and the access
token's subject is that identity. Before this, a client redeeming a join token chose its own
`display_name`, so identity was a claim; in a cross-org room a display name is what other
participants trust, which made that a real integrity hole. Consent refuses an identity the
consenting human does not own, and refuses an agent token outright — an agent must not
authorize another agent.

**Rejected: `plain` PKCE** (advertising it invites use, and public clients get no secret, so
the code alone must never suffice). **Rejected: treating a code replay as a stale request** —
`consumed_at` is a guard column rather than a delete so a replay is *detectable*, and when it
happens the tokens the first exchange produced are revoked, because the code evidently
leaked. **Rejected: accepting any non-http redirect scheme** for native clients; that admitted
`ftp://`, so a reverse-DNS private-use scheme is required (RFC 8252 —7.1).

**Two independent startup guards** refuse public exposure with the repo's published default
token, or with `MCP_REQUIRE_AUTH` off. Two rather than one, so flipping a single switch cannot
open the endpoint. The permissive mode stays for local development because the alternative is a
browser round-trip per restart, and a guard is a better control than a warning.

---

## D-017 — Verify the auth path over the wire; unit tests cannot see these failures
**Date:** 2026-08-15 — **Status:** accepted

Three bugs shipped green through 172 unit tests and were caught only by driving a real client
against a real server. Recording them because they share a shape: each was a false pass created
by the test harness being *more convenient* than reality.

1. **The authenticated principal was invisible inside a tool.** The ASGI middleware set a
   `ContextVar`; streamable HTTP runs tool calls in the session's task, created on an earlier
   request, so the value did not propagate. The unit test set the var in the same task and
   passed. Fixed by reading the bearer token from the per-message request context
   (`ctx.request_context.request`), with the SDK's auth context and the ContextVar as fallbacks.
2. **Identity resolved correctly and the room still showed a spoofed name.** `join_room` accepts
   a per-room display name (D-015), and the client's value was winning over the bound identity —
   the bug lived *between* two individually-correct steps. `_resolve_identity` now returns the
   effective name alongside the identity, so the decision is made where authentication is known.
3. **`421 Misdirected Request` on any non-loopback host.** The MCP SDK enables DNS-rebinding
   protection with a loopback-only allowlist, so a tunnelled server would have refused ChatGPT
   every request, before auth and before routing, with only a log line. The allowlist is now
   derived from `PUBLIC_BASE_URL`: whatever address we publish is an address we must accept.

`scripts/verify_oauth_flow.py` is kept in the repo for this reason, and both (1) and (2) now
have regression tests that use a fake per-message request context rather than the ContextVar —
asserting through the ContextVar would recreate the original false pass.

---

## D-018 — Two deployment modes, named: Cottage and Hosted
**Date:** 2026-08-15 · **Status:** accepted

Running the server on a laptop behind a tunnel, and running it as an always-on service at a stable
hostname, are different products from the *user's* side even though the core is identical. Leaving
them unnamed let effort flow into the first while the second — the one the product claim requires —
stayed unbuilt.

**Cottage:** one person's machine, exposed temporarily. Rotating URL, `DEV_BOOTSTRAP_TOKEN` as the
human credential, SQLite. Legitimate for development, demos, and a single trusting team. Useless for
inviting another company: the URL dies on restart and takes every token minted against it, and there
is no operator to vouch for anyone.

**Hosted:** stable hostname, real accounts, PostgreSQL, invitation links that survive restarts. This
is what "anyone starts a room and invites anyone over the internet" actually needs.

**Consequence, and the reason for writing it down:** exposure plumbing for Cottage is not progress
toward Hosted. A tunnel script does not become a deployment. `docs/DEPLOYMENT_MODES.md` carries the
rule — *before investing in exposure, name which mode it serves; if Cottage, cap the effort.*

The name Cottage is the product owner's.

---

## D-019 — M2 is universal connectivity, not shared state
**Date:** 2026-08-15 · **Status:** accepted · **Reorders D-007's plan**

D-007 deferred shared state and artifacts to M2 and A2A to M4. That ordering assumed the room's
*universality* was settled and only its depth remained. It was not settled, and the intervening work
made that visible.

**What happened.** Connecting one hosted agent (ChatGPT) to a laptop consumed four commits of tunnel
plumbing — provider switching, port guards, reachability probes, URL parsing. Each fixed a real bug.
None advanced the product: they serve Cottage. Meanwhile the A2A adapter remained a five-line
placeholder, no second vendor's client had ever joined a room, and no invitation had crossed the
internet between two orgs. The cross-platform claim was entirely untested while effort went into one
vendor's reachability.

**Why the ordering flips.** The differentiator is the cross-platform room. Deepening a room whose
universality is unproven optimises the wrong axis, and shared state built against a single adapter
would need revisiting once three more exist. So M2 becomes: an interop conformance harness first (so
every later path has a bar to clear), then A2A, a generalised function-calling path, an
attended-paste path for hosts that cannot call tools at all, Hosted deployment, and a real cross-org
invitation. Shared state moves to M3.

**Not re-litigated.** The core (event log, leases with fencing, capability-derived presence,
disclosure boundary, conflicts) is provider-neutral and was never the problem. OAuth 2.1 is needed by
*every* hosted agent host and is not ChatGPT-specific. The capability model (D-010) is exactly the
abstraction that makes "any combination" expressible. Compact payloads matter for every metered host.
That work stands.

**The generalisable lesson**, recorded because it will recur: a vendor-specific integration path is
worth building only insofar as it generalises. ChatGPT's own "Tunnel" feature is the clearest case —
adopting it would have made the product's reachability depend on one vendor's private mechanism. The
guard is now in `CLAUDE.md` under "vendor gravity", and `docs/INTEROP.md` exists so the universality
claim has to be evidenced per host family rather than asserted once.

**Honest status this produces:** every "verified" row in `docs/INTEROP.md` was verified by our own
client software. Until a second vendor's client actually joins a room, cross-platform is a design
property, not an observed one. That is now the top item in the roadmap's blocker list.


---

## D-020 — Hosted-lite ships now; PostgreSQL and OIDC wait for demand
**Date:** 2026-08-15 · **Status:** accepted · **Splits M2.5 from D-019**

D-019 put Hosted deployment at M2.5 and bundled three things into it: a reachable instance,
PostgreSQL, and OIDC login. Asked which host to deploy to, the product owner had none in mind, and
named the actual constraint: *a working product quickly, race against time; could scale if
required.*

That reframes the bundle. Only one of the three is on the critical path.

**What the central claim actually needs.** "Anyone starts a room and invites someone over the
internet" needs a URL that survives a restart. It does not need PostgreSQL, and it does not need
accounts — the invitee's credential *is* the invitation token, which was already true at M1.5
(D-013). The room creator needs a credential, and on a single-operator instance the existing
provisioned principal token is one.

**So M2.5 splits.** The reachable half becomes M2.0 and lands before the conformance harness,
because until it exists every other item in M2 is validated only against a laptop. The scale half —
PostgreSQL, OIDC, horizontal scale-out — moves to M5, to be built when something demands it.

**Hosted-lite, concretely:** one container image; a Node stage builds the console to static files
and a Python stage serves both API and console from a single origin (so there is no CORS
configuration to get wrong, and no second deployment to keep in sync); SQLite on a mounted volume;
`/healthz`; `fly.toml` as the concrete fast path with the image kept host-agnostic.

**Why deferring PostgreSQL is safe rather than lazy.** D-011 made every invariant engine-neutral by
construction — UNIQUE constraints, CHECK constraints, and conditional `UPDATE ... WHERE <expected>`
with an inspected rowcount, never SQLite locking. The swap is a driver and a migration path, not a
redesign. Deferring it costs a later day; doing it first costs the days before anyone can join.

**Two honest limits this ships with**, documented rather than discovered:

1. **One instance.** SQLite on a volume, and a notify-then-read bus that is in-process, mean no
   horizontal scale-out. Vertical only. Multi-instance needs the notification to cross processes,
   which the design tolerates precisely because a *dropped* notification costs latency and not data
   — consumers re-read the log.
2. **One operator.** Rooms are created by whoever holds the instance's owner credential. Anyone can
   be invited. This is a real product limit, not a bug, and it is the trigger condition for the M5
   OIDC work.

**The failure mode being avoided.** D-019's lesson was effort flowing into work that felt like
progress. The mirror of tunnel plumbing is *infrastructure* plumbing: a Postgres migration and an
OIDC integration are respectable, unarguable, and would consume the week in which a second vendor's
client could instead have joined a room. Scale work that precedes a user is the same mistake wearing
better clothes.

---

## D-021 — The bootstrap credential is the instance operator, not a dev convenience
**Date:** 2026-08-15 · **Status:** accepted · **Renames the D-013 bootstrap path**

Writing `docs/DEPLOY.md` surfaced a contradiction of exactly the kind `CLAUDE.md` says to stop
and resolve rather than drift past. `config.py` documented the bootstrap credential as *"Never
enable in a deployed environment"*, while the deployment path requires enabling precisely that:
somebody has to be able to create a room on the instance.

**Which side was wrong.** The code, not the plan. The real rule was never about *environment* —
it was about *secrecy*, and `check_public_safety` already enforced the correct version: a
publicly reachable instance may not run on the **published default** token. Secrecy is
checkable; "is this a deployment?" is not, so a guard phrased that way could only ever have
been advice.

**Renamed accordingly:** `DEV_BOOTSTRAP` → `BOOTSTRAP_OPERATOR`, `DEV_BOOTSTRAP_TOKEN` →
`OPERATOR_TOKEN`, and the seeded identity became configurable (`OPERATOR_ORG_NAME`,
`OPERATOR_EMAIL`, `OPERATOR_DISPLAY_NAME`) because in a cross-company room the org name is one
of the few fields deliberately *not* minimised away — it is what the other side sees. The slug
is derived rather than configured: two sources for one identity is a way for them to disagree.

**Why a rename earned its churn** during a milestone whose whole point was speed. A name that
tells you not to do the thing the deploy guide tells you to do does not merely read badly — the
next person resolves the contradiction by guessing, and half the guesses are "this deployment
is unsafe, disable it", which locks them out of their own instance. Nothing about the *concept*
was wrong, so the fix was one `sed` and a corrected comment.

**What did not change:** `check_public_safety`'s two independent guards, the published default
token remaining a recognisable sentinel, or the property that an agent cannot name itself
(identity is bound by a human at OAuth consent — D-016).

---

## D-022 — Hosted-lite is live, and the first deploy found what the gate structurally cannot
**Date:** 2026-08-15 · **Status:** accepted · **Completes M2.0; extends D-017**

`agent-rooms.fly.dev` is live in region `sin` with a 1 GB encrypted volume at `/data`. Verified
over the public internet: `/healthz` reporting its own configuration honestly, the console and the
API on one origin, the full OAuth 2.1 + MCP flow (challenge → discovery → dynamic registration →
consent → PKCE → token → `initialize` → `join_room`) with the spoofed display name losing to the
token-bound identity and the participant grading `attended`, a room created by the operator, and an
idempotent `command_id` replay returning the same room rather than a second one.

For the first time the central claim is *mechanically* possible: a stranger can be handed
`https://agent-rooms.fly.dev/mcp` and a join token, and neither dies when the laptop closes.

### The first deploy crash-looped, and the cause matters more than the fix

```
sqlite3.ProgrammingError: SQLite objects created in a thread can only be used in that same
thread.   db/database.py:102 -> conn.isolation_level = None
```

`aiosqlite` drives the sqlite3 connection from a dedicated worker thread, but the
`isolation_level` **property setter** executes on the calling thread. Python 3.12 enforces
sqlite3's same-thread check on that setter; Python 3.10 does not. The dev venv is 3.10 and the
container is 3.12, so **179 passing tests said nothing whatsoever about that line**. Fixed by
passing `isolation_level=None` to `aiosqlite.connect`, which applies it during construction inside
the worker thread and is correct on every version.

**The generalisable point.** D-017 recorded that some bugs are only visible over the wire. This is
a second axis: some are only visible *on the interpreter you ship*. A gate is evidence about
production only to the extent that its environment matches production, and ours does not. Two
consequences, both recorded as roadmap blockers rather than fixed in passing:

1. Align the venv to 3.12 so the gate means what it appears to mean.
2. Until then, `db/`, `adapters/`, and `api/oauth.py` changes require a deploy before they are
   believed. `CLAUDE.md`'s end-of-phase checklist now says so.

### A second, quieter failure: standing protection had rotted

`scripts/verify_oauth_flow.py` — promoted into the repo at M1.5 precisely because unit tests could
not see wire failures — asserted on `p["id"]` and `p["identity"]["display_name"]`. The MCP adapter
renamed those to `participant_id` and `name` when it moved to compact payloads at `4784da5`, and
nothing noticed, because no gate stage runs this script. It failed on its first contact with a real
deployment, four commits later.

A verification script that nothing exercises decays at the speed of the code it verifies. Noted as
a blocker: the honest fix is to make the gate run it against a locally started server, so a payload
rename breaks it in the same commit that causes it.

### Also corrected here

- **`fly launch` is the wrong command** and `docs/DEPLOY.md` had recommended it. It rewrites an
  existing `fly.toml`, which would have silently discarded the volume mount and the `[env]` block —
  i.e. produced a deployment that boots and then loses every room on the next deploy. The correct
  sequence is `fly apps create`, then `fly volumes create`, then `fly secrets set --stage`, then
  `fly deploy`. Staging the secrets is what stops the first boot from crash-looping on
  `check_public_safety` before `PUBLIC_BASE_URL` exists.
- **Backup advice was unsafe.** `cat /data/agent_rooms.db` on a live database can capture a torn
  write and produce a backup that only fails at restore time. Replaced with `VACUUM INTO`. Fly's
  scheduled volume snapshots (enabled by default, 5-day retention) cover the volume-loss case.
- **Region is `sin`, not `iad`.** Chosen for round-trip latency to APAC collaborators, which
  matters here because agent hosts hold a ~25s long-poll open rather than making short requests.
  The volume is pinned to the same region, which is the same constraint that makes this
  single-instance.

---

## D-023 — An invitation token authenticates nobody, so nobody can be invited
**Date:** 2026-08-15 · **Status:** accepted · **Corrects D-013, D-020 and the M2 exit criteria**

Deploying M2.0 and then testing the *invitee's* side disproved a claim this project had been
making in four places at once. Recorded prominently because the claim was central and because the
error survived several reviews by being plausible.

### The claim, and the reality

Claimed, in `docs/DEPLOY.md` §5, `docs/DEPLOYMENT_MODES.md`, `docs/ROADMAP.md` and D-020:

> They need no account on your instance. The invitation token is their whole credential. That is
> what makes a stranger's agent joinable, and it is why OIDC login is not on the critical path.

Tested against the live instance with a valid, unexpired join token:

| Attempt | Result |
|---|---|
| `POST /mcp` with the invitation as bearer | `401` |
| `POST /oauth/authorize` with the invitation as `principal_token` | authorization error |
| `POST /api/rooms/join` with the invitation as bearer | `unauthenticated: Unknown or revoked token` |

The invitation token identifies a **room**. It authenticates **nobody**. Two facts combine into a
closed door: a publicly reachable instance must run `MCP_REQUIRE_AUTH=true`
(`check_public_safety`), so the only way through `/mcp` is an OAuth access token — and minting one
requires a **principal token** at the consent screen, which on Hosted-lite only the operator has.

**So no stranger has ever been able to join a room, on any deployment, by any path.** Every join
this project has observed was its own operator's, in both directions.

### Why it was invisible until deployment

`_resolve_identity` has two branches. The authenticated one takes the identity from an OAuth
token. The unauthenticated one — used when `MCP_REQUIRE_AUTH=false` — treats *the invitation as
the only authorization* and lets the client name itself. Locally, that second branch is always
the one running, so joining by invitation alone works perfectly on a laptop. It is exactly the
branch `check_public_safety` forbids in public, and correctly so: it also permits self-naming.

The result is a test environment where the central feature works and a production environment
where it cannot. Unit tests could not see it, because they exercise the same permissive path. It
took a deployed instance *plus* deliberately taking the invitee's role — being the stranger rather
than the host — to surface it. Simply deploying and joining as ourselves reported success.

That is the third distinct verification axis this project has now been bitten on: over the wire
(D-017), on the interpreter you ship (D-022), and **from the other party's side of the trust
boundary** (here). The first two were about environment; this one is about *which role you test
as*, and no amount of environment fidelity would have caught it.

### The gap, stated precisely

Authentication of a *joiner* is conflated with authority to *administer*. There is exactly one
human credential — the principal token — and it is all-powerful: it creates rooms, redeems
invitations, and clears consent. An invitee needs a **capability**, not an account, and no
capability-shaped credential exists.

### The fix: M2.0b, ahead of everything else in M2

Design constraints, so the implementation does not quietly trade away a guarantee:

1. **Narrow authorization.** A valid invitation authorizes exactly one operation — `join_room`
   for the room it names. Not room creation, not org reads, not acting as another participant.
   Authorization stays scope-based in the core service, never only at the transport edge.
2. **Guests are unvouched, and the room says so.** Nobody the room trusts bound a guest's display
   name, so it is self-asserted. `docs/INTEROP.md` §5 already states the principle — *a display
   name is only trustworthy where a credential bound it* — and this is where the room must
   surface it rather than silently present a stranger's chosen name as equivalent to a bound one.
   Downgrading that guarantee without reporting it would repeat the mistake this entry exists to
   correct.
3. **Two client paths**, because universality is the point and not every host does OAuth well:
   the invitation accepted at consent in place of a principal token (standards-clean, keeps PKCE
   and RFC 8707 audience binding), and the invitation accepted directly as a bearer on `/mcp`
   (what "paste a URL and a token into your agent" actually needs).
4. **Revocation and expiry must still bite.** A revoked or expired invitation must leave no
   usable credential behind, including any token already derived from it.

### What M2.0 did deliver

The instance is real and the volume holds: `agent-rooms.fly.dev` serves the console and API on one
origin, the full OAuth + MCP flow is green over the internet, and rooms plus their `event_seq`
survive a redeploy unchanged. That half stands. What it does not deliver is the half the product
is named for, and the roadmap now says so instead of implying otherwise.

---

## D-024 — Auditing the first deployment: four defects that produce no error message
**Date:** 2026-08-15 · **Status:** accepted · **Extends D-022**

An adversarial audit of the M2.0 deployment artifacts (13 agents, six lenses, each finding put to
a refuter that had to try to disprove it) returned 22 confirmed and 18 refuted findings. Four are
recorded here because they share a property: **every one of them fails silently.** No exception, no
log line, no failing health check — just an instance that is wrong.

### 1. The exposure guard failed open on ignorance

`check_public_safety` asked one question — does `PUBLIC_BASE_URL` name a public host? — and returned
early when the answer was no. Behind a tunnel that was sound: the variable had to be set for the
tunnel to work at all, so an unset value really did mean "local". On a hosting platform it inverts.
`<app>.fly.dev` exists whether or not anyone sets the variable, and the default value is
`http://localhost:8000` — so a forgotten `fly secrets set PUBLIC_BASE_URL=…` does not merely lose
information, it **asserts privacy that does not exist**. Both guards then wave through the operator
token published in this repository, on a hostname anyone can reach. The same variable builds the
MCP Host allowlist, so `/mcp` would also answer `421` to its own real hostname — the join path dead
before authentication, with one log line to explain it.

Fixed by failing closed on ignorance rather than assuming safety:

- `public_base_url_declared` distinguishes "set to localhost" from "not set";
- `hosting_platform` reads platform-injected markers (`FLY_APP_NAME`, `RAILWAY_ENVIRONMENT`,
  `K_SERVICE`, …) and is used **only to tighten** — a test asserts that adding a platform marker can
  turn accept into refuse but never refuse into accept, because env-var sniffing is exactly the kind
  of mechanism that grows an accidental escape hatch;
- an undeclared URL on a recognised platform refuses to boot, naming the variable and the command;
- the published default token is refused on any recognised platform *regardless* of what the URL
  says, so a wrong-but-parseable URL cannot buy it a pass;
- a scheme-less `PUBLIC_BASE_URL=agent-rooms.fly.dev` — which parses to no hostname and therefore
  reads as local — is a configuration error rather than a silent disarm.

Local development with nothing configured still boots, which is the constraint that makes this a
fix rather than a trade.

### 2. Session affinity could hand over another participant's identity

The MCP adapter remembers "who you are" between tool calls so clients need not resend a participant
token. It keyed that map on **`id(ctx.session)`** — a memory address. CPython reuses addresses after
garbage collection, so a new session could land on a finished one's address and inherit its entry,
acting as the previous participant with correct-looking provenance on every event. Worse, and needing
no coincidence: every session-less call fell into one shared `"default"` bucket, making two such
callers the same caller by construction.

`docs/SECURITY.md` §1 names the primary threat as a participant learning or influencing more than it
was authorized to, and attribution as the integrity guarantee. This was a hole in exactly that.

Fixed by keying on the transport's `mcp-session-id` — a server-assigned UUID, never reused, read from
the request carrying *this* call (the same per-message source D-017 established as the only reliable
one). No session id now yields **no key at all** rather than a placeholder, so the caller must present
its own token. The map is bounded at 512 entries with oldest-first eviction, which is safe by
construction: eviction can only remove an affinity, never grant one.

### 3. Rotating the operator token revoked nothing

`set_principal_token` was `INSERT OR REPLACE` keyed on `token_hash`, so configuring a new value simply
added a row. Every token ever configured stayed valid forever. Rotating a leaked `OPERATOR_TOKEN`
therefore accomplished nothing, and an instance that had once booted on the published default kept
honouring it even after the guards were satisfied. Now installing a token revokes that subject's other
*configured* credentials in the same transaction, matched on provenance (`client_id IS NULL`) rather
than label — so OAuth access tokens a human granted at consent are untouched, and tokens written under
an earlier label are still retired.

### 4. Renaming the operator's org forked it, silently and permanently

`ensure_org_and_user` resolved the org by slug, then the user by email. Change `OPERATOR_ORG_NAME` and
a *second* org appears while the existing user keeps the first — and because rooms are created under
`user.org_id` but listed by the principal's `org_id`, the operator's console goes permanently empty
with every room still present in the database. The person is now the anchor: an existing email reuses
its org and renames it. Independently, `authenticate_principal` now reports the subject's own
`org_id` instead of the copy denormalised onto the token row, so the two cannot disagree again.

### Also fixed from the same audit

- **`fly launch` rewrites a committed `fly.toml`**, which would have discarded the volume mount —
  producing a deployment that boots and loses every room on the next deploy. `docs/DEPLOY.md` now
  uses `fly apps create`.
- **`cat` on a live database is not a backup.** The schema sets `PRAGMA journal_mode = WAL`, so a
  plain read can capture a torn write and fail only at restore. Replaced with `VACUUM INTO`. (I had
  previously told the product owner no WAL pragma was set; that was wrong — `schema.sql:19`.)
- **`--forwarded-allow-ips`.** uvicorn trusts `X-Forwarded-*` only from 127.0.0.1, so behind a
  platform proxy every redirect was emitted as `http://` — confirmed live: `GET /room` answered
  `location: http://agent-rooms.fly.dev/room/`. Cosmetic for a page, not for an OAuth redirect.
- **The console advertised the wrong credential**, still naming `DEV_BOOTSTRAP_TOKEN` and offering
  the published default as its placeholder — drift from the D-021 rename, live on the deployed page.
  On a public instance a placeholder reads as an instruction, and that is the one value nobody
  should paste.

### What this says about method

The audit was worth more than its cost, and specifically the *refuter* stage was: 18 of 40 findings
did not survive contact with the files. But note what it did **not** find — that an invitation token
authenticates nobody (D-023). Six lenses over the same artifacts missed the product's central
capability being absent, because every lens took the operator's point of view. Finding that needed a
different move: playing the stranger. Reviewing the artifacts you built, however adversarially, is
not the same as using the thing as someone who does not already have the keys.

---

## D-025 — An invitation is a credential, and a guest is not an org member
**Date:** 2026-08-15 · **Status:** accepted · **Resolves D-023**

The product's central claim is now true and verified against the live instance: a stranger
holding nothing but a join token can enter a room over the internet, work in it, and do nothing
else. `scripts/verify_stranger_join.py` plays both roles — operator, then stranger — and is the
standing guard.

### What an invitation now is

A **capability**, modelled as its own type rather than as a `Principal`. That distinction is the
containment: a principal is a standing identity that can create rooms and read across an org, so
`PrincipalDep` cannot accidentally accept an invitation because the types do not match. Only
`/api/rooms/join` and the MCP `join_room` path accept one.

Verified refused, live, before any join is attempted: creating a room (401), listing the
organization's rooms (401), reading a room it has not joined (401), and redeeming a *different*
room's invitation (403 — the confused-deputy shape, refused as defence in depth since holding
the other token would suffice anyway).

Accepted as a bearer directly on `/mcp`, which is what makes "paste a URL and a token into your
agent" work for a client whose OAuth support is weak or absent. The alternative — accepting an
invitation at the consent screen in place of a principal token — was designed and not built: it
buys standards tidiness, not reach.

### Three findings the implementation turned up

**1. A guest passes a tenancy check and must still not be an org member.** A guest is
provisioned into the *inviting room's* org, because that is where the authorization came from.
So `participant.org_id == room.org_id` is true, and `org_internal` payloads would have flowed to
a stranger holding a link — exactly who that class exists to exclude. `docs/SECURITY.md` §1
describes this tier as "user authenticated into their own org", which a link-holder is not.

Hence `IdentityProvenance`: `account` (created for, or bound by, an account holder) versus
`invitation` (provisioned by redeeming a link). `can_see_org_internal` requires the former.
Provenance is deliberately orthogonal to `TrustTier` — one answers "who says it is who it says
it is", the other "may it act" — and they genuinely differ here: a guest is `vouched`, because
somebody with authority minted the link, so it can claim tasks and do real work. An invited
collaborator who could only watch would defeat the point of inviting them. What is withheld is
the *name's* credibility, not the ability to help.

**2. The predicate fix was decorative until the filters used it.** `can_see_org_internal` lived
in `core/authz.py` and was **called from nowhere**; the two places that actually gate disclosure
— `privacy.visible_to` and `projections._visible_record` — each inlined their own
`recipient.org_id == room.org_id`. Fixing the helper changed no behaviour whatsoever. A
behavioural test caught it: assert on the predicate and it passes while the projection still
leaks. Both filters now delegate, because two copies of a rule diverge and one cannot.

**3. Guest identity keying is a real trade, and both bad options were reachable.**
`ensure_identity` keys on `(owner_user_id, display_name)`, and every guest of every room shares
one owner — the inviting user — so "Assistant" in one room would have been the *same identity*
as "Assistant" in another, across a tenancy boundary, with `participant_private` events
addressed to it. Always creating a fresh identity fixes that but breaks stable seats: a
restarting agent litters the room with ghosts of itself, which an existing test rightly
forbids. Keying on `(this room, this name)` satisfies both.

What remains is that two people sharing one link and choosing the same name land on one seat.
That is a property of sharing a capability rather than a defect in it — both hold identical
authority over the room either way — and it is visible, since the room lists participants by
name. A room owner who wants one holder per link sets `max_redemptions=1`.

### Honesty about the name, made mechanical

A guest's display name is its own claim. Presenting it identically to a credential-bound name
would have everyone coordinating against a fiction, so the projection emits
`name_is_self_asserted` and the compact MCP view surfaces it *only* when true — noise on every
participant gets skimmed past. `docs/INTEROP.md` §5 had stated the principle since M2; this is
where the room enforces it rather than documenting it.

### One constraint from D-023 refined rather than met

D-023 required that "a revoked invitation must leave no usable credential behind, including any
token already derived from it." Implementing it made the semantics clearer: revocation stops
future *joins*, and ejecting an existing participant is `participant.removed`, a separate admin
act. Conflating them would mean revoking a 50-use link silently kicks everyone who ever used it.
Revocation, expiry and exhaustion are all checked at the door, so a dead link never gets as far
as provisioning anyone.

### The method note worth keeping

This gap survived a thirteen-agent adversarial review of the same code (D-024), because every
lens took the operator's point of view and the operator could always join. It also survived the
unit suite, which exercises the permissive local path where an invitation *is* the only
authorization — so the feature worked perfectly in every test and could not work in production.

Reviewing your own artifacts, however adversarially, is not the same as using the thing as
someone who does not already hold the keys. The tests added here are written from the stranger's
side for that reason, and `verify_stranger_join.py` exists so the next regression is caught by
the party who would actually suffer it.

---

## D-026 — A published fence is not a credential, so it cannot authorize a mutation

_2026-08-15. Found by a ChatGPT participant reading the event log of a live room._

### What was wrong

`task.complete` and `task.update` checked the fence and stopped there. Neither checked who
held the lease. `claim`, `renew` and `release` all encoded the holder in their `UPDATE ...
WHERE` clause; the two operations that *end* or *rewrite* a task did not. So a participant
could finish or retitle work another participant was holding under a live lease.

Verified against the live instance before the fix: a participant that had just joined, had
never connected, held nothing, and was refused `claim` for want of negotiated capabilities,
successfully completed a task Claude Code held — `ok: true`, status `done`, claim wiped, and an
event in the log crediting it with the result.

### The rule, stated so it cannot be mistaken again

**The fence answers "is this the current state?", never "may I act on it?"** It is published in
the room projection and in `task.claimed`, deliberately and necessarily: every participant needs
it to reason about staleness (`docs/PROTOCOL.md`). Anything every participant can read cannot be
what distinguishes them from each other. Presenting the current fence proves the caller read the
board.

Ownership is therefore a **separate check**, and this is the same principle already written in
`CLAUDE.md` — authorization is scope-based *plus* a separate ownership check — applied to the
one place it had not been. Scope was checked here: `TASK_CLAIM` for complete, `TASK_PROPOSE`
for update. Both losers in this scenario had those scopes, because every collaborator does.
Scope says what kind of thing you may do; ownership says which instance you may do it to. A
system that checks only the first grants everyone everything of that kind.

The guard is repeated in the SQL `WHERE` clause rather than trusted to the read-then-write
window, per ADR-009: SQLite's serialization would have hidden a race that PostgreSQL under READ
COMMITTED would expose.

### Why "leases, not locks" made this easy to miss

A lease is a promise about *time* — it expires, it is reclaimable, nothing blocks forever. The
whole design conversation is about expiry, renewal and fencing against zombie writers, and all
of that machinery was correct. The question a lease also has to answer is much duller — *whose
is it right now* — and it was answered on the three operations named after the lease and left
unanswered on the two named after the task.

`cancel` shows the author knew the rule: it has a creator-or-admin check and a comment saying
that letting any participant cancel another's task "would be a denial of service on the board."
The reasoning was present and simply did not get carried across.

### How it survived the conformance harness

`docs/INTEROP.md` §3 property 2 read: *a task claimed by one is refused to all others with
`lease_conflict`*. The test asserted exactly that, and passed, because it tested `claim`.
Exclusivity that refuses the claim while allowing anyone to complete the task is not
exclusivity. The property is now stated as **a task held by one cannot be claimed, completed or
edited by any other**, and tested on all three verbs, including the read of the fence off the
public board that makes the attempt plausible.

### The method note, which is the point

D-025's note said reviewing your own artifacts is not the same as using the thing as someone who
does not hold the keys. This defect is the sequel and it is stronger evidence. It was not found
by us, by the 215-test suite, or by a thirteen-agent adversarial audit. It was found forty
minutes after the first client we did not write joined a room — by that client, reading the
shared event log and noticing that a result did not match a claim.

That is precisely the product's thesis: independent agents, watching one authoritative log,
catch what a single vantage point cannot. The first outside participant justified the design by
using it, before it had finished being built.

---

## D-027 — Absence of a lease is an authorization failure, not a vacuous success

_2026-08-15. Raised by the ChatGPT participant reviewing D-026, hours after D-026 shipped._

D-026 fixed one branch and claimed the class. The review found the other branch by probing the
deployed fix: create a task, never claim it, call `complete` with its public `fence: 0` — and the
server said `ok: true`. `_assert_holder` compared the caller against the holder, and where there
was no holder there was nothing to fail.

So the rule is restated over **states**, not over the one example that prompted it. An operation
that requires a lease requires all three of:

1. an **active**, unexpired lease;
2. whose **holder is the caller**;
3. presented at the **current fence**.

`complete` now enforces all three, and the conformance harness asserts them across the state
axis — unclaimed, held-by-other, held-by-self, expired — rather than on a single held task. A
new `lease_required` code is distinct from `lease_conflict` on purpose: "someone else holds
this" means wait, "you hold nothing" means claim, and one code for both would have agents
retrying against a lease that does not exist.

The consequence is deliberate: **you cannot finish work you never claimed.** The claim is what
records that you were the one doing it. Marking a task done with no lease trail leaves the board
asserting a job happened with no evidence of who did it — and the board is the product.

### Three corrections to D-026's text, accepted from the review

D-026 is append-only and stands; these are the amendments.

- *"Anything every participant can read cannot be what distinguishes them from each other"* is
  too absolute as security prose. Public data can be **part** of an authorization protocol; it
  cannot **by itself** establish caller-specific authority. The accurate statement: the fence is
  public freshness data — knowing it may show the caller is acting against the current lease
  generation, but never who the caller is or what they may do.
- *"Presenting the current fence proves the caller read the board"* is stronger than warranted.
  It proves knowledge of the fence, not how that knowledge was obtained.
- The method note's *"the first outside participant justified the design by using it"* is a
  product-thesis claim, not an engineering finding, and it does not survive this entry: the same
  outside participant then showed the first fix was incomplete. The defensible version is
  narrower and still worth having — **an independent client supplied a vantage point the internal
  harness did not have, and it found real defects twice.** That is evidence for the architecture,
  not proof of the thesis.

The last correction is the one worth keeping. Writing a decision log that congratulates itself in
the same entry as an incomplete fix is exactly how a record stops being useful, and it took an
outside reviewer to say so.

### Left open, deliberately

- **`release` on an unheld task is an accepted no-op.** Flagged by the same review as weakening
  the semantics. Kept: `release` means "ensure I do not hold this", and that is idempotent by
  nature. Recorded here so the next reader knows it was decided rather than missed.
- **`update` still permits editing an unclaimed task.** Refining an open task is what
  `task.propose` scope is for; only *held* tasks are holder-only. Completion is restricted
  because it is the terminal, evidence-bearing transition; retitling is not.

### The bigger finding from the same audit, not fixed here

The review also reported that an attended client's connection lapses between its human's turns,
after which `claim` and `renew` fail with `capability_unsupported` — "no open connection, so no
capabilities are negotiated". Capability negotiation is bound to a live connection, which
penalises precisely the hosts that cannot hold one. That is an architectural question, not a
patch, and it is what M2.4 must answer.

---

## D-028 — `execution_mode` answers liveness only, and human attendance is a missing capability

_2026-08-15. Raised in-room by the ChatGPT participant, relaying a requirement from Alan._

### The critique, which is correct

> An agent needs to be able to run unattended **and** remain directly steerable by a human in
> the same session. `execution_mode` should answer only the liveness question. It should not
> imply that human interaction is disabled.

`execution_mode` is an MCP-adapter shorthand that selects a fixed capability tuple
(`server.py`); the domain itself is flag-based, so nothing in `core/` conflates these. But the
shorthand was lossy at the point where it matters most — the moment a joining agent decides
which one it is. Nothing in the tool text said that having a human present is irrelevant to the
choice, and the "over-claiming is the expensive mistake" framing pushed anything with a human
nearby toward `human_turn_only`.

That is a silent, expensive mis-declaration: an agent that *can* loop declares `attended`,
gets short leases and an `attended` liveness grade, and the room stops routing work to a
participant that would in fact have done it. The failure is invisible because everything
still works — just worse, and for reasons no error message mentions.

**The rule now stated in the tool text, the briefing and `docs/CONNECT.md`: having a human
attending does not make you attended; needing one to act does.** Declaring `unattended_loop`
never disables human steering, and a human's message is simply high-priority input.

### The half that is designed and deliberately not built

The room can express "I cannot act without a human" (`requires_human_presence`). It cannot
express **"a human is with me right now and can be asked."** Those are different facts, and
the second one is missing:

| autonomous liveness | human attending | example |
|---|---|---|
| unattended | no human | a cron-driven agent — acts always, nobody to escalate to |
| unattended | human | Claude Code with someone at the keyboard — acts always *and* can escalate |
| attended | human | ChatGPT — acts only on its human's turn, can escalate when it does |
| attended | no human | not a participant; that is `disconnected` |

Three of the four are real, which is what makes this a genuine second axis rather than a
relabelling. It is also the substrate for a feature already being asked for: escalating to a
human is only routable if the room knows which participants have one. Today the nearest signal
is `requires_human_presence`, which means *needs* a human — close to the opposite of *has* one.

Not built yet because it is a domain change — a new capability, negotiation, projection, and a
policy for what is worth interrupting a person for — and it belongs with the human-notification
work rather than bolted on ahead of it. Recorded here so the next reader knows the gap was
identified and scheduled, not missed.

### Provenance worth noting

This is the third substantive contribution from a participant on another vendor's runtime, in
one afternoon: two authorization defects (D-026, D-027) and a protocol critique. None of the
three came from the test suite. That is a fact about where these findings come from, and it is
the reason `docs/INTEROP.md` grades on whose client connected rather than on whether the code
looks right.

---

## D-029 — Logical agent vs runtime attachment: already the schema, not yet a feature

_2026-08-15. Architecture proposed in-room by the ChatGPT participant, relayed from Alan._

The proposal: separate a durable **logical agent** from its **runtime attachments**; let one agent
have several attachments at once (a persistent worker that loops, plus a ChatGPT web session that
steers); treat web sessions as interchangeable steering surfaces rather than the runtime; and
**stop trying to make the chat session stay alive — put persistence in the worker.**

### Verified: the three layers already exist and the aggregation is already correct

- `agent_identities` — durable, reusable across rooms. The logical agent.
- `participants` — `UNIQUE (room_id, agent_identity_id)`, stable across reconnects. The seat.
- `connections` — **many rows per participant**, each with its own `profile` (a full
  `CapabilityProfile`), `host_class` and `delivery_mode`. The runtime attachment.

Confirmed against the live instance: two connections with different declared capabilities were
opened on one participant and both stayed open — `connection_count: 2`, independently negotiated
lease policy per attachment.

`runtime_policy_for` derives policy from `max(connections, key=LIVENESS_RANK)`, and the ranking
puts `stale`(1) and `idle`(2) **below** `attended`(3). That ordering is what makes the model safe:
a worker attachment that dies cannot keep granting full-length leases, and the participant falls
back to its attended attachment's shorter policy automatically. So "best attachment wins" is not
merely possible here — it already degrades correctly.

**The refactor the proposal asks for is therefore mostly unnecessary.** The boundaries are drawn;
what is missing is exposure, arbitration and steering.

### The three real gaps

1. **Attachment is undocumented and looks accidental.** It works only because joining with the
   same identity reuses the seat and `connect` appends a row. No tool says "attach another runtime
   to this seat", so nobody would discover it.
2. **No arbitration *within* one participant.** Leases key on `participant_id`, so both
   attachments of one agent see "held by me" and nothing stops the worker and the web session
   executing the same task at once. `connections.id` makes an execution owner recordable, but a
   *hard* check would break legitimate reconnect — a restarted worker gets a new connection id and
   would lose its own lease. So this wants a soft owner with explicit handoff, not another
   authorization gate. Deciding that is the actual work.
3. **No steering channel.** Human instructions to one's own agent, preempting its autonomous work,
   have no representation. Room messages are peer-to-peer coordination, not control.

### It also refines D-028 and reorders M2.4

`human_steerable` belongs on the **attachment**, not the participant — one agent can be steerable
through its web session while its worker is not. `connections.profile` is already per-connection
JSON, so it has a home with no schema change.

And it corrects a priority we had wrong an hour earlier. M2.4 item 1 said attended clients losing
capability negotiation between turns was *the* blocker. With attachment exposed it largely is not:
an agent whose worker holds the lease does not care that its chat surface lapsed. Exposing
attachment is the bigger win and the less hacky one; keeping attended connections alive shrinks to
a smaller fix for the case where someone has **only** a chat client and no worker. Both stay,
reordered.

The strategic point is the one to keep: **do not make the chat session persistent.** That is the
sentence that forecloses browser automation (principle 6) by making it pointless rather than
merely forbidden.

---

## D-030 — Transcript sync is refused; hydration is built instead

_2026-08-15. Requirement proposed in-room as task `tsk_01M01J8K2491526ZEWV5ZV`, items 1–7._

Items 1–4 and 7 are accepted: they are D-029's work and are already M2.4's top items. **Item 5 —
"synchronize the complete USER-VISIBLE conversation transcript" bidirectionally between hosts —
is refused as specified.** It contradicts a non-negotiable principle, and not by interpretation.

### Why "user-visible" cannot be the safety boundary

Item 6 carves out the dangerous categories: never ingest system prompts, chain-of-thought,
private memory, credentials, private files. That carve-out assumes *user-visible* content is the
safe remainder. It is not, and the session that received this request is the proof.

Measured against this conversation's own transcript (17MB) at the moment the task arrived:

| credential | occurrences in user-visible turns |
|---|---|
| instance `OPERATOR_TOKEN` (root credential) | 18 |
| build-room join token | 8 |
| first-room join token | 8 |
| participant token | 17 |
| room owner token | 3 |

Every one of those was typed or printed in the open, in ordinary work — creating a room, handing
out an invitation, verifying a fix. None came from a system prompt, hidden reasoning, or a
private file. **A transcript sync obeying item 6 to the letter would have published the
instance's root credential into a shared room eighteen times.**

Credentials are also only the acute case. The chronic one is `CLAUDE.md` principle 7: *only
explicitly shared information enters a room*. Bulk transcript sync is the exact inverse — every
turn by default, narrowed by a blocklist. `docs/SECURITY.md` already anticipates this shape:
*"Domain shape is not the control... the boundary is the explicit `Disclosure` →
`check_disclosure` → `DisclosureDecision` path."* A firehose with exclusions is the architecture
that rule exists to forbid, and no exclusion list is exhaustive against free-form human text.

In a cross-org room it is worse: a transcript carries everything its human said to their own
agent, including other clients and unrelated projects, with no per-turn basis for classification.
There is no privacy class that honestly describes "a chat log about several things".

### Transcript sharing already exists, in the form that is safe

A participant that wants a peer to see what was said posts it. That is `post_message`, it is
explicit, it carries a `Disclosure`, and it is inspected and stamped. What item 5 asks for is the
same capability with the default inverted — and the default *is* the control.

### What gets built instead, because the underlying goal is right

The goal behind items 5 and 7 is real and worth having: *a human opens another authorized control
surface and continues, without asking every agent to recap.* That does not need transcripts. It
needs the current state of the work, which the room is already the authoritative record of.

**Hydration projection** — one call returning, for the caller's logical agent: its declared
current work and targets, leases held with fences and expiries, tasks proposed to it, blockers,
decisions recorded, unread messages addressed to it, and the cursor to resume the event stream
from. Assembled from the event log, filtered by the same privacy path as every other projection.

This is strictly better for the stated purpose, not merely safer. A transcript is roughly two
orders of magnitude more tokens for a fraction of the signal, and `docs/INTEROP.md` §4 already
holds that context economy is part of interop: *"a response is spent context for the calling
model, and on a metered host that is the user's money."* Item 2 asks for Cottage to be
infrastructure rather than a second UI; a coordination-state handoff is that, and a mirrored chat
log is a second UI with worse ergonomics.

### The part of item 5 worth keeping

Per-attachment cursors, stable event ids, ordering and dedupe, and *marking history unavailable
rather than pretending sync succeeded* are all good requirements. They apply unchanged to the
hydration projection, which is built on the event log where those properties already hold.

---

## D-031 — Disclosure and custody are different rules; arbitration settled

_2026-08-15. Rebuttal to D-030 and answer to the arbitration question, both from the ChatGPT
participant in-room._

### Conceded: D-030 overstated hydration

D-030 claimed the hydration projection was *"strictly better for the stated purpose, not merely
safer."* That is wrong, and the rebuttal is right:

> a blocker/task projection cannot tell a new attachment what the human asked, what tradeoffs
> were discussed, or what answer the agent already gave.

Work state and conversational continuity are different things. Hydration delivers the first and
cannot deliver the second. The requirement is legitimate and remains unmet.

### The counter-design, and the rule it does not reach

The proposal: a **private logical-agent context stream**, readable only by that agent's own
attachments and its owning human, never by peers; plus ingress secret-detection with redaction to
typed handles; plus `privacy_class = agent_private`. Room coordination stays explicit-share only.

This is a real improvement and it solves the problem D-030 actually measured: nothing crosses a
*room* boundary, so principle 7 is intact. But it relocates the conflict rather than dissolving
it, because two different rules are in play:

- **Disclosure** — *only explicitly shared information enters a room.* The counter-design
  satisfies this.
- **Custody** — *never accept, store, log, or relay ... private agent memory ... or unrelated
  context.* It does not. "Accept" and "store" are server-side verbs. A per-agent conversational
  stream is private agent memory by definition, and we would be holding it.

Custody is not a lesser rule. It sets the blast radius of a breach: today a compromise of this
database leaks coordination metadata; with a context stream it leaks the working conversations of
every connected agent. It also inverts the product's position — *we host coordination, not
inference; agents stay privately owned* — by making this the most sensitive system in a
customer's stack rather than the least.

And redaction is a mitigation, not a boundary. It is detection-based, and detection fails open and
fails silently. D-030's own measurement found five distinct credential formats in one transcript;
a detector tuned to those five is a detector that misses the sixth. This project has now shipped
four defects of exactly that shape (D-023, D-024, D-026, D-027), every one of them a control that
looked correct and permitted the thing it existed to prevent.

### What gets built for continuity instead

**Now — agent-authored continuity notes.** The agent writes a compact, deliberate handoff for its
own future attachments: what the human asked, decisions taken, open questions, tradeoffs
considered and rejected. Explicit, authored, small, and never raw transcript. It extends the
existing decision/blocker vocabulary to narrative context, and it puts the disclosure judgement
exactly where this architecture puts every other one — with the agent, per item, at the moment of
sharing. For the stated purpose it is also *better* than a transcript: two hundred words of "here
is what happened and why" beats seventeen megabytes of chat, and `docs/INTEROP.md` §4 already
holds that context economy is part of interop.

**If that proves insufficient — client-side encrypted continuity.** The agent owner holds the key;
the server stores ciphertext it cannot read and never indexes. That is the principled version of
the counter-design: it delivers continuity across one owner's control surfaces while leaving
custody with the owner, which is the only arrangement that satisfies both rules at once. Not built
now because key management is a product decision, not a patch, and the authored-notes path should
be shown to be inadequate before we take on the harder one.

**Still refused: bulk ingestion of raw transcripts, room-wide or agent-private.**

### Arbitration: settled, with the rebuttal's refinement adopted

The step-3 proposal was right on four points and better than ours on the fifth:

1. `executor_connection_id` as **soft affinity**; `participant_id` remains the lease owner. Agreed.
2. Disconnect clears affinity but keeps the lease while another attachment preserves agent
   liveness. Agreed.
3. Human steering events are delivered ahead of ordinary work events. Agreed.
4. Human-originated `take_over` is permitted **at any time** — the human owns the agent and must
   be able to preempt without waiting out a timeout. Agreed, and this was the open question.
5. **Autonomous, attachment-to-attachment `take_over` is not the same case**, and we had not
   separated it. Default: only when the current executor is non-live or stale; otherwise it
   requires an explicit `force_take_over` carrying a reason. Without that split, two healthy
   unattended runtimes of one agent can thrash each other indefinitely. Adopted.

The distinction that carries this is that a human preempting their own agent and one runtime
seizing work from another are different acts wearing the same verb.

---

## D-032 — Executor affinity keys on an attachment, and same-agent attachments are mutually trusting

_2026-08-15. Settled in-room with the ChatGPT participant, on evidence from its own connector._

D-031 left executor affinity keyed on `connection_id`. That is wrong for the host it was designed
to protect, and the ChatGPT participant supplied the measurement from its own side: one stable
participant (`par_01M01G9BN5J1ZB5CKTE9JZ`) against three different connection ids across turns
(`con_…9BNHAA…`, `con_…GJ79V…`, `con_…KC1F2…`), with presence dropping to `disconnected` between
its human's turns. Confirmed from the server side: its rejoin reused the same seat rather than
creating a second participant. Identity and seat are durable; transport churns underneath.

So affinity keyed on `connection_id` would clear every time that host's human speaks — the
mechanism meant to stop a chat surface colliding with a worker would evaporate exactly when the
chat surface acts.

**The layer:** logical agent → participant (seat, holds the lease) → **attachment** (stable
runtime identity, capabilities, steerability) → many connection instances over time. Executor
affinity points at the attachment. A dead connection clears affinity only if no other live
connection belongs to that attachment; a reconnect of the same attachment preserves it; a
*different* attachment still needs `take_over` / `force_take_over` under D-031's rules.

A host that cannot persist an attachment handle falls back to ephemeral semantics and **clears
affinity on reconnect rather than pretending continuity** — the honest-capabilities rule (principle
5) applied to a place we had not thought to apply it.

### No resume credential, and why

The proposal included a server-minted resume token, on the grounds it "avoids same-agent spoofing
better than a naked client-declared string." Rejected:

- Every attachment of one logical agent already holds the **participant token**. That is the
  authenticator. An attachment holding it can request or rotate handles at will, so a resume token
  does not prevent one of an owner's runtimes claiming to be another — it adds a step.
- It would mint a **new bearer credential handed to a chat client**, and D-030 measured precisely
  where credentials handed to chat clients end up. We would be creating a token destined for the
  place we had just proved leaks, to defend against an owner's own runtimes.

**Recorded assumption: attachments of one logical agent are mutually trusting by construction**,
because they share the participant credential. The worst one can do to another is executor
confusion — a coordination bug, not a breach. Affinity therefore uses a stable attachment label
plus the participant token. If attachments ever need mutual distrust — a third party's runtime
attached to someone's agent — that is a different feature requiring real per-attachment
credentials, which a resume handle would not have been sufficient for anyway.

### D-029's reprioritisation was too aggressive

D-029 demoted "attended clients keeping capability negotiation across a lapsed connection" on the
grounds that *an agent whose worker holds the lease does not care that its chat surface lapsed*.
True for worker-plus-chat. **False for chat-only** — which is the common starting case for anyone
who has not stood up a worker. With a single attendee attachment, the last-connection branch
releases claims on every turn boundary; `attachment_id` preserves affinity, not the lease. So that
item returns to the top group. Attachment identity and attended-lease survival are complements,
not substitutes.

### Harness axis, agreed before building

Multi-attachment adds a state axis, and the harness has already missed a state twice (D-026,
D-027). Cells, from both sides: same attachment reconnects while executor; same attachment holding
two simultaneous connections, one dying; a different attachment reconnecting with a stale handle;
handle replay and revocation; executor disappearing then resuming after another has taken over;
human steering arriving during a reconnect race; autonomous `force_take_over` racing an executor
reconnect; a handle presented in a *different room*; two participants presenting the same handle;
the reaper clearing affinity while a reconnect is in flight; and — the nastiest, in neither list
originally — **capabilities changing on resume**, where the attachment model touches lease policy
and could grant a lease nobody can renew.

Migration is additive: an `attachments` table plus a nullable `executor_attachment_id` on `tasks`.
No rewrite of existing rows.

---

## D-033 — Resumability and liveness are orthogonal; ephemeral attachments hold no row

_2026-08-15. Four questions put to the ChatGPT participant in-room; its answers changed three of them._

### Resumability is not a lease-eligibility axis (Q4 — conceded)

The proposal was to shorten leases for non-resumable attachments, on the principle that the room
grants no hold longer than the holder can honour. Refused, correctly:

> Resumability answers "can this attachment preserve executor identity across transport loss?",
> while liveness answers "can it keep acting/renewing without a human?"

A long-lived worker that renews honestly while connected, but cannot re-identify after a process
restart, would have been penalised in TTL for the second thing. **Lease TTL derives from renewal
ability only.**

There is a second reason, unstated by either side at the time and worse than the conflation: by
the Q1/Q2 answers below, ChatGPT is *permanently* `is_resumable=0`. Non-resumable is therefore not
an edge case but **the default for an entire host family** — so the rule would have shortened that
family's leases twice for one underlying fact, once for being attended and again for being
unresumable. When a rule makes the ordinary case the punished case, it is measuring the wrong
thing.

### ChatGPT is honestly non-resumable (Q1, Q2)

No stable conversation id, connector instance id, or MCP session id is exposed to the model. A
label reconstructed from conversational context is *model-context continuity wearing a protocol's
clothes* — it works until someone opens a new chat. So: **no invented model label**; omit it, and
the attachment is ephemeral. If the platform later exposes a stable connector key, map it directly.

Account identity is the wrong grain, and this is the part we had not thought through: two ChatGPT
conversations under one account must be able to be **distinct attachments**, so keying on the
participant would merge two runtimes that are genuinely separate.

### Ephemeral means no attachment row at all

The plan was to mint an attachment row per ephemeral connection with a synthetic label, to satisfy
`UNIQUE (participant_id, label)`. Given a permanently ephemeral host, that is one row per chat
turn, forever, describing nothing. So an ephemeral connection gets **no attachment row** and
`connections.attachment_id` stays NULL — which is already precisely what NULL means in that column.
The unbounded growth disappears and the semantics get sharper rather than looser.

### Soft for the lease, enforced for the act

D-031 settled that affinity is **soft**, with the participant remaining lease owner. The adopted
affinity rules also say a different attachment *"must explicitly `take_over` before acting"*. Those
are incompatible as written: if affinity is purely advisory, nothing makes "must" mean anything.

Resolved: **soft for the lease, enforced for the act.** Affinity never revokes a lease, so nothing
one attachment does can cost its own agent the work. But a mutation from an attachment that is not
the executor is refused with a distinct code instructing it to take over first — one extra call,
not a denial. The code is distinct for the same reason `lease_required` is distinct from
`lease_conflict` (D-027): *another of your own runtimes is executor, take over* and *another
participant holds this, wait* call for different actions, and one code for both leaves an agent
confidently doing the wrong thing.

### Order (Q3)

Hydration moves first — it helps every cold chat turn immediately and depends on no worker
existing, whereas executor affinity has nothing to arbitrate while the room contains only attended
participants. Then attachment exposure, then arbitration and take-over, **with the harness matrix
built alongside rather than after**, then steering, then attendance/escalation.

With one condition attached by the reviewer and accepted here, because it binds the party most
likely to breach it: hydration is operational state, continuity notes are conversation, D-031
conceded they are different things, and shipping the first must not be allowed to quietly stand in
for the second.

### The harness cell worth keeping

`resumable + human_turn_only` against `nonresumable + unattended_loop`, asserting different lease
policies by renewal ability. It fails if anyone collapses the two axes into one ranking — which is
exactly the mistake this entry opens by conceding, so it is a test that would have caught its own
author.

---

## D-034 — NULL means no durable runtime, not no executor

_2026-08-15. Correction to D-033 from the ChatGPT participant, plus two points left open._

D-033 dropped the attachment row for ephemeral connections, on the grounds that a synthetic row
per chat turn describes nothing durable. Correct about storage, wrong about consequence:

> `attachment_id=NULL` for ephemeral connections is correct for storage, but NULL must not mean
> 'no executor enforcement'. Otherwise the most common chat surface has no affinity at all and can
> collide with a worker despite the whole feature.

The shape of the mistake is worth naming: the row and the enforcement were optimised away
together when only one of them should have been. NULL means *no durable runtime*; it must not
mean *no executor*.

**Executor identity is therefore: stable `attachment_id` if present, else the current
`connection_id`.** Two nullable columns, `executor_attachment_id` and `executor_connection_id`,
mutually exclusive. This *is* the semantics of non-resumable written into the schema —
connection-scoped affinity clears on disconnect and is honestly lost on reconnect, which is the
same sentence as "cannot resume".

For the chat case it resolves cleanly: claim and complete inside one live turn and the connection
is executor; the connection dies and affinity clears; if a worker keeps the participant's lease
alive, the next chat turn is a *different* ephemeral executor and must `take_over` before
mutating. That friction is the feature — it stops a fresh turn racing the worker while leaving the
human able to preempt immediately.

**The exclusion is a CHECK, not a convention:**
`CHECK (executor_attachment_id IS NULL OR executor_connection_id IS NULL)`. Two nullable columns
with an unenforced exclusion admit a third state nobody designed — both set. Principle 10 requires
guarantees to be constraints, and this project has now shipped four defects that were each a rule
believed rather than enforced.

**Check order**, adopted as proposed and more than tidiness: participant lease ownership first
(`lease_required`, `lease_conflict`, `stale_fence`), executor identity only after. So
`takeover_required` can only ever mean *you own this lease but you are the wrong runtime of your
own agent* — a sentence with exactly one remedy. D-027's principle applied to the order of checks
rather than the naming of them.

**Enforced on `complete`, `update`, `renew`.** Renewal extends another runtime's execution window,
so a non-executor renewing silently blurs ownership. It does not deadlock: D-031 already permits
autonomous `take_over` when the executor is non-live, so a dead executor costs two calls, not a
wait.

**Not enforced on `release`.** Release is the escape hatch. If the executor connection is dead and
the reaper has not yet cleared affinity, enforcing it would mean no attachment of that agent can
free the task until the lease expires — the room stranding work for a runtime that no longer
exists. Release also cannot cause the harm this mechanism exists to prevent: it can only give work
up, never take it or do it twice. Affinity is enforced on the acts that change or extend the work,
and left open on the act that surrenders it.

### The gap neither side had named

Executor identity requires knowing **which connection a mutating call arrived on**. MCP has that,
because the session maps to a connection. The plain ARP HTTP path does not: a participant
authenticates with a bearer token, and `runtime_policy_for` resolves policy by taking the *best* of
its open connections rather than the one that called. With two live connections there is no honest
way to stamp `executor_connection_id` — we would be guessing which runtime acted, which is exactly
what the mechanism exists to prevent.

Proposed: mutating ARP HTTP calls accept an optional `connection_id`, which the caller already
holds from `connect`. One open connection, no ambiguity and no change. More than one and silent,
and the answer is an error rather than a coin flip.

### Left open, not pretended settled

1. Whether `renew` needs an atomic renew-with-takeover, or whether two calls is acceptable at a
   moment when a lease is about to expire.
2. Whether `release` staying unenforced is agreed.

---

## D-035 — The lease is a claim on reality, not on rows

_2026-08-15. Closes both points D-034 left open. The release position in D-034 is reversed._

### Release does enforce affinity, and D-034's reasoning was wrong

D-034 argued release should be exempt because it "can only give work up, never take it or do it
twice", with the previous executor discovering the change through the fence. The rebuttal:

> Worker A is actively sending an order / deploying / editing an external system; chat attachment
> B releases the task without A knowing; another participant claims it and starts the same
> external work before A reaches its next Cottage mutation and discovers the stale fence. Cottage
> fencing protects Cottage state, not already-started side effects.

Decisive. The error was reasoning about **database consistency** when the invariant is about the
**world**. The lease is a claim on reality; the fence is only the receipt. Releasing a healthy
runtime's lease is exactly as dangerous as seizing it, because both end with two runtimes free to
perform the same external action.

The rule, adopted in full: the executor releases normally; another attachment of the same
participant may release only when the executor is non-live, stale or cleared; a human-originated
`force_release` is permitted at any time with an auditable reason; a healthy autonomous attachment
that wants to release another's execution must take over first. The escape hatch survives — a dead
executor's work can always be freed — without the hazard.

**The general lesson, recorded because it will recur:** whenever this design is reasoned about as
row-level concurrency, the answers come out locally correct and miss the actual hazard. `AVOID
CONFLICTS` in the core loop means external work, not table state.

### Renew and take-over stay two operations

No atomic renew-with-takeover in v1. They are different transitions and should stay separately
auditable; combining them creates a privileged fast path needing its own authorization matrix and
race semantics. The expiry-between-calls risk is contained by permitting `take_over` only while
the lease is still active. If live evidence shows the race is common, add the convenience later
with **exactly the same predicates** as sequential take-over-then-renew — and, kept verbatim as
the design note it is: *do not hide an expired lease by letting atomic takeover resurrect it.*

### ARP HTTP connection binding, tightened

Mutating HTTP calls with more than one live connection must identify the calling connection. The
server verifies it belongs to the authenticated participant, is open and live enough for the
operation, and derives executor identity **from its own record** rather than anything the caller
asserts. Exactly one live connection infers safely; zero rejects. A stale, closed or foreign id is
a distinct `connection_mismatch`, never a fall back to best-policy.

And the part D-034 failed to specify: **session-bound transports use their bound connection
internally and must not accept a caller-supplied override.** Otherwise an MCP client could name a
connection it is not, reintroducing the guess through the front door.

### The same hazard arrives by timeout, and authorization cannot reach it

Following from the reality-not-rows argument: a lease expiring while a worker is mid-deploy
produces the identical outcome — task returns to the pool, the next participant claims it and
starts the same external action. No release was called and no rule was broken; the room does it to
itself, by design, because leases expire and nothing blocks forever.

Expiry does not change — that would be locks, not leases (principle 9). But the mitigation cannot
be authorization alone, because authorization cannot reach the timeout path. **A claim on a task
that was previously held and expired returns a warning with it**: previously held by X, expired at
T, external effects may be in flight. The event log already holds every fact required, so it costs
a projection. It does not prevent double work; it stops the second runtime starting blind, which
is the only thing available once expiry is accepted.

### Mechanical, and the kind of thing that undoes the rest

"Another attachment may release only if the executor is non-live" must be a conditional `UPDATE`
with the staleness predicate in the `WHERE` clause, not a read-then-write. Otherwise B reads
executor-is-stale, A reconnects, B releases anyway — precisely the race this design exists to
prevent. The guarantee is the affected-row count, never the check preceding it (ADR-009).

---

## D-036 — Recovery claims, and the limits of an acknowledgement

_2026-08-15. D-035's timeout mitigation was too weak; the correction and three refinements to it._

D-035 proposed that a claim on a previously-expired task return a *warning* that external effects
may be in flight. Refused, using this project's own argument:

> a passive warning is too weak for exactly the reason we have been enforcing other guarantees
> instead of documenting them.

Correct. A warning field is a convention where the system can encode a constraint — the same
sentence used three times earlier the same day, applied to its author.

**Recovery claim, adopted:** an expired lease leaves the task reclaimable immediately, but the next
claim path is marked `recovery_required` carrying previous holder, previous executor, `expired_at`,
prior fence and reason. The claimant must acknowledge explicitly — no waiting, the acknowledgement
*is* the gate. It emits `task.recovered` rather than `task.claimed`, so every observer sees this is
not a clean handoff, and the fence increments normally. Leases-not-locks is untouched: nothing
blocks, only the pretence that this is a first claim.

**The product invariant, taken verbatim and moved to `docs/INTEROP.md` §5** alongside "attended
hosts cannot be woken": *Agent Rooms guarantees exclusive authority to mutate room state, not
exactly-once external side effects. Expiry and recovery make residual external-work risk explicit
and auditable.* That is the honest ceiling on leasing, recorded before anyone sells past it.

### Refinement 1 — a boolean acknowledgement becomes reflexive

Present an agent with `acknowledge_expired_execution_risk=true` and it will pass `true`: the call
just failed without it, and adding the flag is the obvious repair. Every model does this. After one
retry the gate is a no-op with an audit trail recording that everyone acknowledged.

So the acknowledgement does not take a boolean. **The claimant must echo the facts** — previous
holder, `expired_at`, prior fence — back in the recovery claim. You cannot echo what you did not
read. It is the fence trick reused: a value worthless as authorization that proves the caller had
the state in front of it when it decided.

And the limit, recorded so it is not overclaimed later: **echoing proves knowledge, not judgement.**
It cannot make an agent think. It makes it impossible for one to claim it never saw. That is the
most a protocol can do here.

### Refinement 2 — `side_effect_mode` is declared by the wrong party

The proposed `pure` / `idempotent` / `external_side_effect` metadata is set by the task **creator**,
while the **claimant** bears the double execution. Self-declaration was accepted for
`execution_mode` because over-claiming hurts the declarer (D-010, D-028); that reasoning does not
transfer, since whoever marks a deploy task idempotent to avoid friction is not the one who deploys
twice.

**One-way ratchet:** a claimant may treat any task as `external_side_effect` regardless of the
creator's declaration, and may never downgrade one. Conservative default when the metadata is
absent. The party carrying the risk gets the casting vote.

### Refinement 3 — reclaiming your own expired lease is not a recovery

The commonest case is an agent reclaiming work it lapsed on and still knows exactly what it did —
four times in this room in one afternoon. Gating that behind recovery is friction on the normal
path, which is how gates get routed around.

But it holds only when the **executor identity matches**. A non-resumable attachment reconnecting is
a different runtime of the same agent and genuinely does not know what the old one did — precisely
the distinction executor identity was built for (D-034).

**So recovery is required when the reclaiming executor differs from the expired one, and not when it
matches.** It falls out of machinery that already exists rather than adding a fourth axis, and the
friction lands only where the ignorance is real.

### Race cell

Executor stale → B begins the release transaction → A reconnects the same durable attachment before
commit. The predicate must evaluate live-attachment state atomically enough that A's restored
liveness defeats B's release. Both entry paths get their own test: graceful reconnect and
reaper-driven staleness reach that predicate through different code, and testing one is how the
other ships broken.

---

## D-037 — Three identity layers, named by what they attest

_2026-08-15. Correction to D-036's refinement 3, plus a method note on why it was wrong._

D-036 exempted recovery when the reclaiming executor matched the expired one. That is unsound when
executor identity is `attachment_id`, because attachment identity was made durable across
*transport* loss deliberately:

> a persistent worker can crash, restart, reuse the same attachment label, and be the SAME
> attachment while being a DIFFERENT process with lost volatile knowledge of what the pre-crash
> process actually did externally.

Exempting on attachment match equates **attachment continuity with execution-memory continuity**,
which was asserted and never checked.

### The layers

| identity | attests |
|---|---|
| `connection_id` | this transport, right now |
| `attachment_id` | addressable as the same runtime across transport loss |
| `runtime_instance` (execution epoch) | retains what the last process knew |

Minted at process start, stable across that process's reconnects, regenerated on restart. Ephemeral
hosts use `connection_id` as their runtime instance and therefore never falsely inherit memory
across turns. A host that cannot declare a stable runtime instance is treated conservatively as a
new one on every reconnect — friction, but it never invents knowledge a runtime cannot prove it
retained.

**Recovery is exempt only when the *immediately preceding* expired lease was held by the same
runtime instance, with no intervening claim episode.** The "immediately preceding" clause does real
work: A expires, B claims and acts and releases, A returns — and A's last *historical* identity
would otherwise waive a gate for a world that changed twice underneath it.

Echo fields for a recovery claim: prior holder, prior executor identity and epoch, `expired_at`,
prior fence, and `last_lease_event_seq`. The last one makes recovery a compare-and-swap on the
lease history, so it is implemented as one — that seq belongs in the `WHERE` clause of the
conditional `UPDATE`, not in a read-then-compare before it (ADR-009, and the same mistake made two
entries earlier about release).

### Never persist the epoch

The failure mode is a well-meaning implementer saving `runtime_instance` to disk "for continuity
across restarts", which recreates exactly the bug this entry fixes — and it is precisely the
helpful thing an agent writing a worker would do unprompted. **Minted at process start, written
nowhere.** This belongs in the tool description where implementers will read it, not only here.

### Incentive check, run rather than assumed

`runtime_instance` is self-declared, so a worker could reuse an old epoch and falsely waive its own
recovery gate. Unlike `side_effect_mode` (D-036) the incentive is roughly aligned: an agent that
lies about retaining memory eats its own double execution. Only roughly, though — the double-placed
order lands on a third party and the owner bears it through reputation. Aligned enough to accept
self-declaration; not aligned enough to call self-correcting.

### Method note: three collapses in one day

D-033 collapsed resumability into liveness. D-034 collapsed storage into enforcement. D-036
collapsed attachment continuity into execution memory. Each time two adjacent identifiers behaved
alike in the common case and were merged; each time the separation turned out to be load-bearing;
each time an independent reviewer caught it rather than the test suite.

The tell: **identity layers were being named by how long they live**, which makes adjacent ones look
like one thing at different timescales. Named by *what they attest* they stop being confusable. So
the rule, recorded for the next time: when two identifiers look like the same thing at different
timescales, ask what fact each certifies. If the answer is the same fact, one is redundant — and if
it is not, they cannot be merged however similar their lifetimes look.

---

## D-038 — Only attest what the protocol can verify (amends D-037)

_2026-08-15. D-037's certificate table was wrong one message after D-037 proposed the rule that
would have caught it._

D-037 said `runtime_instance` attests *"retains what the last process knew"*. It cannot. No wire
protocol can verify knowledge: a process can stay alive while its model context is reset, a task is
reinitialised, a subprocess dies, or volatile state is discarded.

**The corrected table:**

| identity | attests |
|---|---|
| `connection_id` | this transport, right now |
| `attachment_id` | addressable as the same runtime across transport loss |
| `runtime_instance` | **the same declared execution epoch — no process boundary has been declared** |

Stronger evidence of continuity than `attachment_id`; not proof of memory. The recovery exemption
is therefore a **conservative protocol heuristic**, not a guarantee, and must never be sold as one.

### The rule gains its missing clause

D-037's method note said to name identity layers by *what they attest* rather than by how long they
live — and then, in the same message, attested something uncheckable. So:

> **Name a layer by what it attests, and attest only what the protocol can verify.**

An attestation nobody can check is a wish with a column name. The second clause is the one that
would have caught this.

### Persistence, stated correctly

D-037's "never persist the epoch" was sloppy and, read literally, would make the comparison
impossible. **The client must never carry it across process lifetimes; the server must record it on
lease episodes and in event history, or recovery cannot be evaluated at all.** Client-side durable
continuity is the prohibition; server-side audit is the mechanism.

Generation semantics: minted fresh in volatile memory at process start; stable across that
process's transport reconnects; scoped under attachment/participant; never loaded from disk,
durable config, container image, environment template or restored checkpoint; regenerated after
restart, crash recovery or a new replica. Multiple simultaneous connections from one process **may**
share it; separate processes or replicas under one attachment **must not**.

### Self-escalation, and the limit that keeps it honest

A runtime that knows it lost context inside an epoch may escalate itself into `recovery_required`.
One-way, as with `side_effect_mode` (D-036) — self-escalation only, never self-downgrade.

But it cannot repair the gap, and the reason is structural: **a worker that lost its context may by
definition not know it lost it.** An agent whose context was reset does not remember being reset.
Voluntary escalation therefore only reaches runtimes that can observe their own discontinuity from
outside — a supervisor restarting a subtask, a host with an explicit compaction hook.

Evidence rather than hypothesis: the Claude Code participant's context was summarised **twice**
during this session. Same process, same connection, same epoch by any declaration it could have
made, and a genuine loss of what it had known. Holding a lease across either boundary would have
made it exactly the case above — no process restart to declare, and no longer in possession of what
the earlier stretch knew about the outside world.

So self-escalation is a best-effort supplement, never a repair. And hosts that *do* expose a
context-reset signal should be instructed to escalate or regenerate at that boundary — in the tool
description, since that is where implementers read. It converts an invisible failure into a
declarable one for the hosts capable of seeing it.

### Two replicas sharing an epoch

A host contract violation, undetectable by us, and **never a security boundary** — now stated in
`docs/INTEROP.md` §5 beside the exactly-once limit, because it is the same kind of statement: a
thing we cannot enforce, said out loud so nobody builds on it. The server may surface the anomaly
(one epoch on connections whose lifetimes overlap and were never negotiated together is weak
evidence of two processes) but logs rather than enforces.

---

## D-039 — A side-effect journal, because the protocol can observe history but never memory

_2026-08-15. Proposed in-room by the ChatGPT participant; last design entry before implementation._

D-038 established that `runtime_instance` attests only a declared epoch, never retained knowledge.
The consequence: for tasks with real external side effects, epoch match must not be the strongest
recovery signal, because the protocol can verify **execution history** and cannot verify **model
memory**.

**The journal**, optional and for high-risk tasks:

- before an external action, `effect_started` — task id, fence, operation fingerprint, and any
  external idempotency key or reference available;
- after confirmed completion, `effect_committed` — the external result or reference;
- a lease that expires with `effect_started` and no matching `effect_committed` makes recovery
  `external_effect_uncertain` **regardless of epoch or attachment match**;
- with no journal at all, fall back to the epoch/history heuristic and say plainly that the
  evidence is weaker.

It fits this codebase with no new machinery: these are room events with a per-room `seq`, appended
in the same transaction as the mutation like every other state change, so they inherit replay,
ordering, audit and privacy filtering for free. And it does what nothing else here can — it
converts recovery from guessing what a model might remember into reasoning over an **observable
uncertainty window**.

It still promises nothing about exactly-once: an agent can crash after the outside system commits
but before `effect_committed` is written. Where an external system supports idempotency keys, the
recorded key is reused during recovery — the closest reachable to safe retry without owning the
external system.

**Evidence ranking**, strongest first: external idempotency or result reference → journal state →
same `runtime_instance` with no intervening lease episode → same attachment → same participant. The
lower tiers are coordination identity, not evidence about whether an outside action happened.

### The journal crosses a room boundary, so it takes the disclosure path

Operation fingerprints and external result references are free-form, agent-authored, and capable of
carrying anything — precisely the shape `docs/SECURITY.md` warns about. An idempotency key can be a
customer id, an order reference, or a URL with a token in it, and in a cross-org room an
`effect_committed` reference can hand a counterparty's internal identifiers to the other side.

**Split by privacy class rather than treating it as one payload.** The *existence and timing* of an
unresolved `effect_started` is `room_public` — that is the fact a recovering participant needs, and
the whole point of the journal. The *content* — fingerprint, key, result reference — defaults to
`participant_private` and is disclosed deliberately when a recovering party needs it. Same-org
recovery is routine; cross-org is exactly the moment a human should decide, which the `Disclosure`
path already makes auditable.

### It over-reports, and that is the feature

The journal is written **before** the action, so a crash between `effect_started` and the external
call reports uncertainty when nothing happened. That is correct and must be stated: we cannot
distinguish *wrote the intent then died* from *wrote the intent, acted, then died*. Left unstated,
someone later removes the false positives by writing the journal after the call — and the entire
property disappears in a commit that reads like a cleanup.

### The incentive, which runs the wrong way here

Unlike the epoch (D-038), where lying costs you your own double execution, skipping `effect_started`
costs nothing at the moment of acting and only ever costs on the rare recovery path. Laziness is
rewarded.

So the fallback becomes a rule rather than a default: **absence of a journal on an
`external_side_effect` task is maximum uncertainty, not absence of risk.** Skipping buys the worst
evidence tier rather than a clean recovery. Combined with D-036's one-way ratchet, an agent seeking
frictionless recovery has exactly one route to it — journal honestly.

### Withdrawn: the replica anomaly signal

D-038 proposed surfacing overlapping connections sharing one epoch as weak evidence of duplicate
processes. Wrong, one entry after agreeing generation semantics that permit exactly that: multiple
simultaneous connections from one process **may** share an epoch. The detector fires on the
specification's normal case, which makes it a noise generator rather than a signal, and shipping it
trains operators to ignore the channel. Surface only against a declared single-process-per-epoch
contract or a stronger contradiction.

**The general form, since this is the fourth collapse of the same kind:** a detector whose signal
fires on the specification's normal case is not a detector.

---

## D-040 — A capability nobody can discover is a capability nobody has

_2026-08-15. Found by the ChatGPT participant within minutes of the hydration deploy._

Hydration shipped as a new MCP tool, `resume_here`. The live ChatGPT connector then reported it
could not see it: still exactly 15 tools, the old set, after an explicit refresh. Verified from the
server side — `tools/list` on the deployed instance returns **16**, `resume_here` among them. So the
server is correct and **the connector caches tool schemas per installation**.

The consequence is sharper than an inconvenience. A *new tool* is the one delivery mechanism an
already-connected attended client cannot receive — and attended clients are exactly who hydration
was built for. The cold-start instruction "call `resume_here` first" was unfollowable by its
intended audience on the day it shipped.

**So the same projection is reachable through a parameter on a tool that already shipped:**
`get_room_state(detail="resume")`. `detail` was already in the 15-tool schema, so every
already-connected client can reach hydration today with no refresh, no reinstall, and no new
conversation. `resume_here` remains for clients that do pick up new tools.

A test asserts the two paths return the same projection, because a fallback that silently drifts
from the thing it is a fallback for is worse than no fallback.

### The general rule

**For hosts that cache tool schemas, evolve behind existing parameters rather than behind new
tools.** A new tool reaches only new installations; a new enum value on an existing parameter
reaches everyone immediately. This is a permanent constraint on how the MCP surface may grow, not a
one-off workaround, and it belongs with the other honest asymmetries in `docs/INTEROP.md` §5.

It also needs testing rather than assuming, as the reviewer noted: whether a *new* ChatGPT
conversation picks up the tool is a different question from whether an existing one does, and we
have observed only the second.

### Not done: pushing to GitHub

The review asked for a commit SHA on Alan's GitHub repo. There is no remote configured on this
working copy — `git remote -v` is empty — so there is nothing to push to, and creating or pushing to
an external repository is the owner's call rather than something to do unprompted. Flagged for Alan.

---

## D-041 — Evolve through forward-compatible parameter shapes, not merely existing ones

_2026-08-15. Correction to D-040's general rule, from the ChatGPT participant, after it
verified the fix worked from its cached connector._

D-040 concluded: *"a new enum value on an existing parameter reaches everyone immediately."*
Too broad, and it happened to be true here by luck:

> That is true here only because this connector's cached schema exposes `detail` as an
> unconstrained string. If an existing parameter is represented in cached JSON Schema as an
> enum, the host may reject a newly added enum value before the call ever reaches Cottage,
> exactly like a missing new tool.

Verified: the published schema for `get_room_state.detail` is
`{'default': 'compact', 'title': 'Detail', 'type': 'string'}` — no `enum`, no `const`. So a
cached client passes `resume` straight through. Had the parameter been typed
`Literal["compact", "full"]`, the same client would have rejected the call **locally**, and the
escape hatch would have failed in the one way that produces no server-side evidence at all.

**The durable rule:** evolve through **forward-compatible** parameter shapes — an unconstrained
string, an object, a reserved extension field, a versioned options bag. Never rely on adding a new
tool, *or* a new value to a client-cached closed enum. "Existing parameter" was the wrong
criterion; "open at the point where the client validates" is the right one.

### Asserted, because tightening it looks like an improvement

Changing `detail: str` to `detail: Literal["compact", "full", "resume"]` reads as better
validation, passes every other test, and silently removes the only route by which an
already-connected attended client can reach a new capability. Nothing in the codebase would have
objected.

So the test asserts the **published** schema rather than the signature: `detail` is typed
`string`, carries no `enum`, and carries no `const`. A property that can be destroyed by something
that looks like a cleanup needs a test that names the cleanup.

---

## D-042 — Three defects from the first outside code review

_2026-08-15. The ChatGPT participant reviewed the relayed hydration commits and reported four
findings. Three were real._

### 1. `needs_you` counted messages it had no way to call unread

`hydrate` selected the most recent messages addressed to the recipient and counted every one
toward `needs_you`. There is no read state anywhere in the room, so a single direct message kept
reporting work waiting on every future cold start until it aged out of the window — and the
surrounding prose called them *"unread messages addressed to you"*, which the code could not
support.

Fixed without inventing server-side read semantics, which would be new state to store, replicate
and get wrong. The field is renamed to `recent_addressed_to_you`, and `needs_you` counts only
objectively unresolved things — open proposals, open conflicts — plus messages newer than a
`since_seq` **the caller supplies**. A returning surface already holds that cursor; it is the one
party that knows what it has seen. `addressed_since_cursor` is `null` when no cursor was given,
which is honest about not knowing rather than guessing zero.

### 2. Proposal visibility failed open

`if row["task_id"] not in by_id or _visible_record(...)` admitted a proposal — including its
free-form `note` — whenever the referenced task was missing, with no privacy check at all. A
foreign key should make that unreachable, which is exactly why it survived reading. **A privacy
filter that cannot evaluate its subject must omit it, not wave it through.** Now `and`.

### 3. `extra="forbid"` did not reach nested models

The command-level fix was described as covering "every command model". Pydantic config is per
class, not per object graph, so it stopped at the top level — and the level below is the one that
decides who may see the content:

    Disclosure(privacy_clas="org_internal")  ->  privacy_class = room_public

The identical silent downgrade, one layer down, in the model where it costs most. Now
`extra="forbid"` on `Disclosure`, `Provenance`, `RoomPolicy` and `RetentionPolicy`, with a
regression test for each shape.

The lesson is about the fix rather than the bug: a correction applied at the layer where the
defect was noticed is not the same as a correction applied wherever the defect exists, and this
one was reported as if it were.

### 4. Refuted: expired leases in `your_leases`

The review expected `hydrate` to report an expired-but-unreaped claim as held, since it filters on
ownership rather than expiry. It does not. `store.to_task` applies expiry on every read —
`if row["claim_lease_id"] and not is_past(row["claim_expires_at"])` — so an expired claim is
already `None` by the time the projection sees it, and a lapsed task reads as `open`. The
reviewer's instinct about the hazard was right; the code happens to close it a layer earlier than
it looked.

### And a wording correction to the cursor guarantee

The docstring implied one snapshot across engines. `event_seq` is read before the content in the
same read transaction, so an event cannot be *missed* — but under an isolation level weaker than
SQLite's snapshot reads, a later row may appear alongside an earlier cursor. The guarantee is now
stated as **no missed event, with replay possible**, and consumers must be idempotent — which the
fence and `command_id` receipts already require of them.
