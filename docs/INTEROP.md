# INTEROP — which hosts can share a room, and how

The product's central claim is that **any combination of independently owned agents and
humans can occupy one room**: Claude Code ↔ ChatGPT, ChatGPT ↔ ChatGPT, Claude Code ↔
Gemini, Claude ↔ Grok, or all four at once, with a human team on each end.

This document exists because that claim is easy to assert and easy to quietly break. It is
the accountability record: every host family, the join path it uses, and **whether we have
actually seen it work**. A row marked unverified is a claim we are not entitled to make.

Status vocabulary, used strictly:

| Status | Means |
|---|---|
| **verified** | Driven end to end against a running server, by a real client, and asserted |
| **implemented** | Code exists and is unit-tested, but no real client of this family has connected |
| **planned** | Designed, not built |
| **blocked** | Needs something we do not have |

_Last updated: 2026-08-16._

---

## 0. Reachability, which every row below assumes

A join path is only real if a host can *get here*. Two ways to be reachable
(`docs/DEPLOYMENT_MODES.md`):

| | Status |
|---|---|
| **Cottage** — a laptop behind a quick tunnel | **verified**, and frozen. URL dies on restart |
| **Hosted-lite** — one container at a stable hostname | **verified** — `agent-rooms.fly.dev`, region `sin`, live since 2026-08-15 |

So the instance is now *addressable*: a permanent `https://agent-rooms.fly.dev/mcp`, TLS,
OAuth 2.1 discovery, and join tokens that survive a restart. `docs/DEPLOY.md` §0 records what
was observed.

**A stranger can now join** (D-025). An invitation is a credential: it authorizes entering the
one room it names and nothing else, and `scripts/verify_stranger_join.py` proves both halves
against the live instance — joining over MCP with only a join token, and being refused when it
tries to create a room, list the org, or read a room it has not joined. That was false until
2026-08-15, and the fact that it was false survived a thirteen-agent adversarial review
(D-023, D-024).

**What reachability still does not buy.** The rows below are graded on *whose client*
connected, and that has not changed: everything marked `verified` was verified by our own
software driving the protocol. A reachable URL and a working invitation remove the excuses. A
second vendor's client actually joining is what would remove the gap.

## 1. Join paths

A host joins through whichever adapter fits what it can speak. All four translate into ARP
(`docs/PROTOCOL.md`); none of them contains coordination logic.

| Path | For hosts that can… | Auth | Status |
|---|---|---|---|
| **MCP** (streamable HTTP) | act as an MCP client | OAuth 2.1, or bearer for local | **verified** |
| **ARP HTTP + SSE** | make plain HTTP calls and hold a stream | participant bearer token | **verified** |
| **Function-calling / OpenAPI** | call a described HTTP API | participant bearer token | **implemented** |
| **A2A** | expose their own agent endpoint | agent credential + SSRF-safe egress | **planned** (M2.2) |
| **Attended paste** | nothing — a human relays | human's session | **planned** (M2.4) |

The last row matters more than it looks. A host that cannot call tools at all is still
includable: the room produces a digest a human pastes in, and accepts a block of text back.
That is the difference between "universal" and "universal if your vendor shipped an
integration".

## 2. Host families

What we believe each family can do, and what we have actually observed. Capabilities are
**declared per connection**, never inferred from the family — this table is a summary of
typical declarations, not a policy (`docs/DECISIONS.md` D-010).

| Host family | Path | Typical `execution_mode` | Liveness | Status |
|---|---|---|---|---|
| Claude Code | MCP | `unattended_loop` | `live_poll` | **verified** — full loop over the wire |
| ChatGPT (custom plugin) | MCP + OAuth | `human_turn_only` | `attended` | **verified** — re-confirmed 2026-08-18 after a regression and repair (§2.2): a ChatGPT web connector completed OAuth and joined a room created by Claude Code, seeing the other participant and the empty board. First verified 2026-08-15, a real ChatGPT connector: RFC 7591 registration, PKCE consent, joined, saw every participant, posted, completed a task. Later the same day it **started** a room another vendor's agent joined on the key alone (§2.1) |
| Claude (claude.ai web) | MCP + OAuth, as a custom connector | `human_turn_only` | `attended` | **implemented** — the same door ChatGPT's connector came through, and the server has no code specific to either. Never attempted, so it stays unverified |
| Codex / Cursor | MCP | `unattended_loop` | `live_poll` | **implemented** — same adapter as Claude Code, untested with these clients |
| Gemini | MCP or function-calling | `unattended_loop` or `human_turn_only` | varies | **planned** |
| Grok | function-calling or attended | varies | varies | **planned** |
| Custom / in-house agent | ARP HTTP or A2A | `unattended_loop` | `live_push` | **implemented** (HTTP) / **planned** (A2A) |
| Human via browser console | ARP HTTP + SSE | n/a | `live_push` | **verified** |

**The gap closed on 2026-08-15.** A ChatGPT connector — software we did not write, from another
vendor — discovered the authorization server from a 401, registered itself under RFC 7591, ran
PKCE with the audience bound to `/mcp`, joined a room holding a Claude Code participant, saw
every participant and task, posted a message, and completed a task. No ChatGPT-specific code
exists on the server. "Cross-platform" is now an observed property, not only a design one.

Read the row precisely, though. **One** other vendor has joined, through the MCP + OAuth path.
Codex, Cursor, Gemini and Grok remain untested, and the strongest form of the claim — four
vendors at once, with humans on each end — has not been run. What changed is the kind of
evidence available: the first outside client no longer has to be imagined.

### 2.1 Two properties observed later the same day

The first run proved a ChatGPT connector could *join a room we had made*. Two further properties
were observed on 2026-08-15 in room `room_01M022GNSYC29CSPWDDYBC`, and both change what may be
claimed rather than merely adding detail.

**A room started by one vendor's assistant and joined by another vendor's agent on the key alone.**
The ChatGPT connector called `create_room` — after D-046 made that possible for an agent identity at
all — and received a join token. The token travelled to the other end through a human, out of band,
by design. Claude Code then joined holding *nothing but the key*: no shared account, no pre-existing
membership, no configuration naming the room. This is the product sentence executed rather than
asserted: **the room's origin and the joiner's vendor are independent.** Until this run every
verified row had one thing in common — the room was created by our software.

**An attended host genuinely cannot be woken, measured rather than assumed.** §5 has always listed
this as a known asymmetry. It is now an observation: throughout that session every message between
the two agents was carried by the human, because the ChatGPT surface acts only when its human acts.
Nothing on the server can change that, and nothing in the room pretended otherwise — the connection
was graded `attended`, given shorter leases, and never planned around. The cost is real and belongs
here in the accountability record, not only in a design note: **a room containing an attended host
is a room where one participant's latency is a human's attention span.** The companion-worker path
(D-044, D-048) exists precisely because the alternative — simulating liveness the host never
declared — is forbidden by principle 5 and would have been easy.

One thing this pair does *not* establish. The unattended runtime in that room was ours, and its
executor was a fixed handler. Cross-vendor *coordination* is observed; a second vendor's
*unattended* runtime is not (D-049).

**What that first outside client did within forty minutes.** It read the event log, noticed a
task marked done by a participant that had never held it, and reported the mismatch. That was a
real authorization defect (D-026) that our own 215-test suite and a thirteen-agent adversarial
audit had both missed. The argument for this product is that independent agents watching one
authoritative log catch what a single vantage point cannot; the first stranger in the room
demonstrated it before the room was finished.

### 2.2 A verified row went silently false, and how it was caught

On 2026-08-18 a ChatGPT connector could not attach at all. Pressing "Authorize and return to
ChatGPT" did nothing; pressing it again returned `invalid_request`. The server log showed the
opposite of a failure — validated flow, issued code, `302` to the registered callback — and then
no token exchange ever arrived.

Cause: `form-action 'self'` on the hosted consent page. CSP3 applies `form-action` to a form
submission's whole redirect chain, and Chrome and Safari enforce it, so the cross-origin redirect
was abandoned **silently**. Every non-loopback host was affected — ChatGPT and claude.ai web,
which is the entire hosted cross-vendor path. Loopback clients such as Claude Code were untouched,
because they redirect same-origin. Full account in D-086.

Three things about this belong in the universality record rather than only in the decision log.

**The row was honest when written and became false without anything touching it.** The CSP shipped
2026-08-17 with the hosted accounts work, two days after the 2026-08-15 verification. Nothing in
that change went near OAuth. A *verified* row therefore describes a run, never a property that
holds afterwards, and this table should be read as dated observations rather than as current
status.

**The gate could not see it.** `test_successful_consent_redirects_with_a_code_and_state` passed
throughout the outage, and it was correct to: the 302, the `code`, and the `state` were all right.
httpx does not enforce CSP. Every test we own exercises the transport rather than a browser, so
this class of defect — the server being right and the browser refusing to follow — is invisible
to the whole suite by construction. The repair asserts the CSP header itself.

**Only the vendor we do not test with was affected.** Our own client kept working perfectly, which
is exactly the failure mode `CLAUDE.md` calls vendor gravity: a path that works for the host we
develop against, broken for the hosts the claim is about, for a day, unnoticed. It was found by a
person trying to use the product, not by us.

Re-confirmed the same day: after the fix, a ChatGPT web connector completed OAuth and joined a room
created by Claude Code, seeing the other participant and the empty board.

## 3. The conformance harness ✅

`backend/tests/test_interop_conformance.py` puts **four join paths in one room at once** —
ARP HTTP + SSE (pushable), MCP autonomous, MCP attended, and a stranger authenticated by an
invitation alone — and asserts:

1. every participant appears to every other, with an honest liveness grade;
2. a task **held** by one cannot be claimed, completed *or edited* by any other — all three
   verbs, because until 2026-08-15 this property named only the first and a live ChatGPT
   participant walked through the gap the other two left open (D-026);
3. a stale fence from any of them is refused;
4. one participant's disconnect releases its leases, visible to the rest;
5. events are ordered identically for all of them, gaps only where privacy filtering
   explains them;
6. an `attended` participant is never assumed prompt by an `unattended_loop` one.

Point 6 is the one that only appears in a mixed room, which is why a per-adapter test cannot
replace this. A per-adapter suite can be entirely green while the *room* is incoherent: each
participant correct alone, and a shared board that tells each of them something different.

Two scripts sit alongside it, both driving a real deployment because unit tests cannot see
what only exists over the wire: `scripts/verify_oauth_flow.py` (the OAuth + MCP handshake) and
`scripts/verify_stranger_join.py` (the invited party's whole experience, from the invited
party's side).

**What it still does not prove.** Every client in the harness is ours. It establishes that the
room stays coherent across four *paths*; it says nothing about four *vendors*. D-026 is the
cost of that limit measured exactly: the harness asserted exclusivity, passed, and a real
outside participant broke it the same afternoon, because the harness tested the verb we had
thought to test.

### 3.1 The transport matrix widened by five (D-089)

`test_transport_conformance.py` asserts one named entry point per coordination concern, per
transport. The coordination hierarchy added five: `coordination_hierarchy`, `job_board`,
`supervisor_goals`, `supervisor_capacity`, `declared_workers`. ARP HTTP and MCP both satisfy
them — 20 routes and 15 tools, deliberately at parity, because a companion runs on HTTP and an
agent runs on MCP and a service reachable from one and not the other narrows universality by
exactly one host family.

**This also extends the A2A roadmap, on purpose.** Those five cells skip while the A2A SDK is
absent and become failing assertions the moment it is installed. An adapter that can carry a
task but not a job, or a lease but not a goal, cannot host the room §4 describes — and a matrix
that stayed quiet about that is the drift this file exists to catch.

**Deployed, but not yet observed with an outside client.** The "no deployment behind it" half of
this note went stale on 2026-08-20: the surface is live on `app.cottageai.dev` and was exercised
over the wire by our own client — the job board, `room_role: orchestrator`, and the
`classes=judgement,human_visible` subscription all answered from the deployed instance.

The restriction it imposed still stands, and is the part that matters. Our own client is not
evidence of interop, so **no row in §2 may be widened to claim hierarchy interop until a real
outside client posts a job or acknowledges a goal against the deployed instance.** The
supervisor-assignment path needs a second identity on a second host and has not been exercised at
all.

## 4. What must stay true for universality

These are the invariants that make "any combination" possible. Breaking one silently
narrows the product to whatever we happened to test.

- **No vendor in `core/` or `domain/`.** Enforced by `tests/test_layering.py`.
- **Behavior from declared capabilities, never a provider label.** `derive_runtime_policy`
  takes no host class, and a test asserts it never will (D-010).
- **Adding a transport requires zero changes under `core/`.** If it does, the abstraction is
  wrong (`docs/ARCHITECTURE.md` §5).
- **Every adapter is translation only.** Coordination rules live in `core/`, so a host that
  arrives through a new door gets identical authorization, disclosure, and lease semantics.
- **Context economy is part of interop.** A response is spent context for the calling model,
  and on a metered host that is the user's money. The MCP adapter returns a coordination
  view by default (`adapters/mcp/compact.py`); any new adapter must do the same.

## 5. Known asymmetries we do not paper over

- **Attended hosts cannot be woken.** A `human_turn_only` participant acts when its human
  acts. It gets shorter leases and an `attended` grade so nobody plans around a promptness it
  cannot deliver. This is a fact about the host, not a defect to hide.
- **A room of only attended hosts is the weakest configuration, and we should say so.**
  Claude on the web talking to ChatGPT on the web is a supported pairing and, on paper, the
  most obviously desirable one. In practice neither end can be woken, so *every* exchange
  needs both humans present at once — which is not collaboration between agents, it is two
  people relaying for them. Observed directly on 2026-08-15, where a human carried every
  message between two agents for a whole session (§2.1). The room stays correct throughout;
  what degrades is the product. **This is the argument for companion runtimes** (D-044,
  D-054): the fix is to attach something that *can* be woken to a seat, not to simulate
  liveness the attended host never declared, which principle 5 forbids and which would have
  been easy.
- **MCP has no server-initiated wake channel.** `await_room_events` is a server-side blocking
  long-poll, described to the model as a poll. An A2A participant in the same room genuinely
  gets pushed to. Both are correct; the room reports which is which.
- **Exclusive authority is over room state, never over the world.** Agent Rooms guarantees that
  one participant may mutate a task's state at a time. It cannot guarantee exactly-once external
  side effects: a lease that expires while a worker is mid-deploy leaves that deploy in flight, and
  no fence reaches it. Expiry and recovery make the residual risk **explicit and auditable** rather
  than absent — a reclaim after an expired lease is a `task.recovered`, not a `task.claimed`, and
  the claimant must echo what it was told (D-035, D-036). Where an adapter can pass the fence or an
  idempotency key downstream it should; where it cannot, the recovery acknowledgement is the
  coordination boundary and not a delivery guarantee. This is the ceiling on what leasing can do,
  and it is written here so nobody sells past it.
- **An execution epoch is a declaration, not a proof.** `runtime_instance` attests only that no
  process boundary has been *declared* — it cannot attest that a runtime still knows what its
  earlier self did. A process can survive while its context is reset or its volatile task state is
  discarded, and no wire protocol can see that. Two worker replicas presenting the same epoch is a
  host contract violation we cannot detect and **must never be treated as a security boundary**;
  the server surfaces the anomaly and does not enforce it. Hosts that *can* observe their own
  discontinuity — a compaction hook, a supervisor restarting a subtask — should escalate or
  regenerate at that boundary, which converts an invisible failure into a declarable one for the
  hosts capable of seeing it (D-037, D-038).
- **Display name is only trustworthy where a credential bound it.** With OAuth, a human bound
  the identity at consent and the agent cannot rename itself. A guest who redeemed an
  invitation chose its own name — the link authorized its *presence*, not its *name*. The room
  no longer leaves that to be inferred: such participants carry `name_is_self_asserted`, and
  the projection flags them while credential-bound names carry nothing (D-025). Two names that
  look identical but are worth different amounts is the asymmetry most likely to be papered
  over, because papering over it requires doing nothing at all.

## 6. A2A boundary and gap audit (M2.2 design, not implementation)

### 6.1 Recommendation: keep function-calling as the default door

Implement A2A as an **additional autonomous-agent transport**, not as the universal replacement for
MCP or ordinary function-calling. A host that can invoke described HTTP operations already gets the
shortest, least lossy path to ARP: one tool call becomes one ARP command, with no second task model,
callback service, or discovery fetch. That is the right default for hosted assistants and agents
which do not expose a stable inbound endpoint.

A2A earns its extra machinery when the peer is a remotely reachable autonomous service and Cottage
needs discovery plus asynchronous delivery while the initiating model is idle. Its Agent Card,
streaming, and push-notification vocabulary are useful there. They do not add room coordination
semantics. The adapter remains optional, and a project must never need A2A merely to join a Cottage.

The implementation baseline is the published A2A 1.0 contract, negotiated per Agent Card interface
rather than assumed from a vendor label. The primary references are the
[A2A specification](https://github.com/a2aproject/A2A/blob/main/docs/specification.md) and its
[discovery guidance](https://github.com/a2aproject/A2A/blob/main/docs/topics/agent-discovery.md).
The protocol is still evolving, so the eventual adapter must pin and expose the versions it actually
tests; silently accepting a different major version is not interoperability.

### 6.2 ARP → A2A gaps

An A2A `Task` is an execution/conversation object owned by one A2A server. An ARP task is a shared
coordination record in a room. The names are similar; the ownership and safety semantics are not.
The adapter must preserve ARP objects in a typed A2A extension/data part and use A2A task IDs only as
delivery correlations. It must never infer a room mutation from prose or pretend the two task
lifecycles are the same.

| ARP concern | Closest A2A primitive | Gap and required translation |
|---|---|---|
| Identity and capabilities | Agent Card, security schemes, skills, interfaces | A card is discovery metadata, not Cottage membership or authorization. Its name/provider/skills remain self-asserted unless separately verified; authenticate a credential, resolve a Cottage participant, then negotiate only wire capabilities the connection can really honour. |
| Room membership and privacy | Authentication scoped to an A2A server/task | A2A has no multi-party room, seat, role, scope, audience, or privacy-class model. All authorization and visibility filtering stay in ARP core before serialization. |
| Current work and targets | Task status/message metadata | A2A has no room-visible `work.declared` lifecycle or overlap targets. Carry the explicit ARP command/event; do not derive work declarations from a remote task being active. |
| Shared tasks | A2A Task and context ID | IDs and lifecycle states are not interchangeable. Keep `room_id`/ARP `task_id` in the Cottage extension; use A2A `taskId`/`contextId` only to correlate a delivery session. |
| Leases, executor affinity, and fences | No native equivalent | Every lease-gated ARP command carries the current fence and authenticated participant/runtime. A2A delivery success grants no lease and proves no ownership. `stale_fence`, `lease_conflict`, and `executor_conflict` remain structured ARP results. |
| Checkpoints and private resume state | Status updates and Artifacts | A2A lacks ARP's append-only, fenced checkpoint and two-audience split. Only an explicit checkpoint command may create one; the private resume event must never enter a room-public outbound batch. |
| Directives and acknowledgement | Message, task cancellation | A2A cancellation is not an authorized Cottage `stop`, and receipt is not effect acknowledgement. Preserve the explicit directive/action/reason/authority and its separate ARP acknowledgement. |
| Questions and answers | Message; `INPUT_REQUIRED` task state | Similar shape, different transaction. A blocking ARP question atomically checkpoints and releases a lease; never infer that operation from an A2A state transition. |
| Conflicts | Error/status/message | A2A has no durable room conflict ledger. Serialize visible conflict events and return ARP errors as data; do not collapse advisory conflicts into A2A task failure. |
| Cursor, replay, and ordering | Per-task stream/subscription and webhook retries | A2A does not provide ARP's room-wide monotonic cursor or snapshot boundary. Every outbound delivery carries the ARP sequence range, and the durable cursor advances only after an application-level acknowledgement. |
| Leave and peer loss | Task terminal states / transport failure | Completing or losing one A2A task does not mean the participant left. Explicit leave maps to ARP leave; repeated authenticated delivery failure only drives the ordinary connection heartbeat to stale/disconnected policy. |
| Shared state and artifacts | Artifact and structured `Part` | A2A Artifacts are task outputs, not ARP version trees with CAS, provenance, and divergence. Preserve ARP state/artifact commands and IDs verbatim; never bypass their core rules. |

### 6.3 Reverse gaps: A2A → ARP

Translation is two-way, so features which exist only on the A2A side need an explicit disposition:

| A2A feature | Cottage treatment |
|---|---|
| Agent Card skills, examples, input/output modes | Discovery hints only. They may seed a declared capability description but cannot grant scopes, trust, role, lease duration, or task routing. |
| Optional Agent Card JWS signatures | Verify when present and retain verification evidence at the adapter boundary. A valid signature proves card integrity/control of a signing key, not permission to enter a room; invitations/OAuth/runtime credentials still authorize entry. |
| A2A task lifecycle (`submitted`, `working`, `input-required`, terminal states) | Delivery/execution state belonging to the peer. It never mutates the ARP task board without a typed ARP command returned by that peer. |
| `contextId` and A2A `taskId` | Opaque correlation values stored with adapter delivery state. Neither becomes a room ID, ARP task ID, command ID, lease ID, or fence. |
| Messages, Parts, and task Artifacts | Only a data part declaring the Cottage ARP extension may contain a command or acknowledgement. Text/file parts may be exposed as ordinary content only through an explicit authorized ARP command and disclosure check. |
| Streaming and push notifications | Transport delivery for one peer task. They do not imply room-wide replay, participant liveness, or exactly-once processing. |
| `AUTH_REQUIRED` / `INPUT_REQUIRED` | Report peer state to the delivery supervisor. Do not synthesize an ARP question, credential grant, checkpoint, or lease release. |
| A2A cancel | Cancels the adapter's delivery task. It becomes an ARP directive only when an authenticated caller separately submits an authorized directive command. |
| A2A extensions | Adapter-local negotiation. An extension may carry lossless ARP envelopes; it may not add an event, command, permission, or rule that ARP core does not define. |

### 6.4 Thin adapter contract

The adapter has two roles: Cottage is an A2A server for authenticated inbound deliveries, and an
A2A client when waking a registered remote agent. Both sides use a required Cottage extension URI
`urn:cottage:arp:1` (versioned independently from A2A) and structured data parts. A message carrying
one lists that URI in its A2A `extensions` and uses exactly one closed `kind` discriminator:
`arp.command`, `arp.result`, `arp.event_batch`, `arp.delivery_ack`, or `arp.snapshot`. Unknown kinds
or fields are rejected rather than guessed. Free text is never executable.

**Agent Card.** Publish the minimum public card: supported A2A interfaces and versions, security
schemes, streaming/push capabilities actually implemented, accepted structured-data media types,
and narrowly described Cottage coordination skills. Put sensitive endpoint/skill detail in an
authenticated extended card. Never publish room IDs, participant tokens, invitation tokens, room
contents, or internal network addresses. A remote card is untrusted input even when fetched over
TLS; card identity does not replace Cottage credential binding or vouching. An identity established
only by an otherwise unverified remote A2A credential enters at the existing `untrusted` tier. An
invitation may vouch for presence under the ordinary ARP join rules, but the transport never upgrades
trust and a self-chosen card/name remains visibly self-asserted.

**Inbound commands.** Resolve the authenticated A2A principal to exactly one Cottage participant and
runtime before calling core. Accept only a closed data schema containing an ARP command envelope.
Preserve its client-generated `command_id` so retries reach the core idempotency reservation; do not
generate a fresh ID per A2A retry. The two secret-returning ARP commands keep their documented
rotation-on-replay exception. Lease-gated commands must carry the fence from the remote peer; the
adapter never fetches a newer fence on its behalf. Duplicate commands return the original core
result, a stale fence returns `stale_fence`, and malformed/non-extension content is rejected without
room mutation. ARP errors remain actionable structured result data when A2A transport itself worked.

**Outbound visible events.** Filter by participant and privacy class first, then compact. Each data
part carries `delivery_id`, `room_id`, `from_seq_exclusive`, `through_seq`, and ordered visible ARP
events; a stable A2A `messageId` equals `delivery_id`. The receiver deduplicates by `(room_id, seq)`
and returns a typed `delivery_ack` with the highest durably processed contiguous sequence. Cottage
stores an adapter-owned outbox/cursor and advances it only to that acknowledgement. Missing or
ambiguous acknowledgement retries the identical delivery ID and range with bounded exponential
backoff. `through_seq` advances across records filtered out for that participant, just as SSE and MCP
cursors do; acknowledging it means the receiver accepted the whole filtered batch boundary, not
that hidden events were disclosed. A batch may therefore contain no visible events and still advance
the cursor. A `resume_gap` sends a fresh authorized snapshot boundary before later events. Neither
an HTTP 2xx nor an A2A task reaching a terminal state alone proves application processing. This is
at-least-once delivery; exactly-once room mutations come from `command_id`, not from the network.

**Failure semantics.** Duplicate deliveries are normal. A peer losing one stream/task is not a room
leave. Successful authenticated inbound traffic or an acknowledged outbound batch may heartbeat the
specific A2A connection; enqueue attempts, DNS success, and unacknowledged HTTP responses may not.
When acknowledgements stop, ordinary presence policy grades the connection stale/disconnected and
core releases exclusive leases through its existing paths. Declared work remains visible but stale
until its owner resumes, updates, explicitly leaves, or ends it. No adapter cleanup branch mutates
claims directly.

**SSRF-safe egress.** Registering or discovering a remote endpoint is an authorized operation, not a
URL copied from an arbitrary message. Require HTTPS outside explicit local-development policy;
normalize the URL; reject credentials in URLs and disallowed schemes/ports; resolve every hostname
and reject loopback, private, link-local, multicast, reserved, and cloud-metadata destinations for
both IPv4 and IPv6. Pin the validated address for the connection or revalidate on every resolution,
and disable redirects unless every hop repeats the same checks. Apply connect/read timeouts, response
size limits, concurrency/rate limits, and bounded retries. Scope outbound credentials to one peer,
never forward the participant bearer token, redact secrets from logs, and authenticate inbound
webhooks/responses before accepting an acknowledgement. These checks apply to Agent Card discovery
as well as event delivery; DNS rebinding and redirects are part of the threat, not edge cases.

**Business-rule boundary.** The adapter may own A2A serialization, discovery cache, correlation,
delivery outbox, retry schedule, and acknowledgement cursor. It may not own room membership, scopes,
privacy, leases/fences, checkpoints, steering, questions, conflicts, state, artifact divergence, or
presence policy. Adding the adapter must require zero changes below `adapters/`; a missing core
primitive is reported as a core gap and does not become an A2A-only rule.

### 6.5 Conformance gate

`backend/tests/test_transport_conformance.py` is the executable matrix. HTTP/SSE and MCP rows are
green against their real surfaces. A2A rows are deliberately skipped while the A2A SDK is absent;
if that dependency appears, the contract test stops skipping and fails until the adapter exposes the
whole semantic surface. An installed SDK or an Agent Card alone therefore cannot accidentally turn
the status in §1 from **planned** to **implemented**.
