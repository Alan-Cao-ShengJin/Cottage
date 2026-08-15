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
