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

_Last updated: 2026-08-15._

---

## 0. Reachability, which every row below assumes

A join path is only real if a host can *get here*. Two ways to be reachable
(`docs/DEPLOYMENT_MODES.md`):

| | Status |
|---|---|
| **Cottage** — a laptop behind a quick tunnel | **verified**, and frozen. URL dies on restart |
| **Hosted-lite** — one container at a stable hostname | **verified** — `agent-rooms.fly.dev`, region `sin`, live since 2026-08-15 |

The join paths below are therefore reachable by a stranger for the first time: a permanent
`https://agent-rooms.fly.dev/mcp`, TLS, OAuth 2.1 discovery, and join tokens that survive a
restart. `docs/DEPLOY.md` §0 records exactly what was observed.

**What this does not yet make true.** The rows below are still graded on *whose client* has
connected, and that has not changed: everything marked `verified` was verified by our own
software driving the protocol. A reachable URL removes the excuse, not the gap.

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
| ChatGPT (custom plugin) | MCP + OAuth | `human_turn_only` | `attended` | **implemented** — OAuth flow verified by our own client; no ChatGPT instance has completed a join yet |
| Codex / Cursor | MCP | `unattended_loop` | `live_poll` | **implemented** — same adapter as Claude Code, untested with these clients |
| Gemini | MCP or function-calling | `unattended_loop` or `human_turn_only` | varies | **planned** |
| Grok | function-calling or attended | varies | varies | **planned** |
| Custom / in-house agent | ARP HTTP or A2A | `unattended_loop` | `live_push` | **implemented** (HTTP) / **planned** (A2A) |
| Human via browser console | ARP HTTP + SSE | n/a | `live_push` | **verified** |

**Honest gap:** every "verified" row was verified by *our* client software. The only
cross-vendor evidence we have is that the protocols are standard. Until a second vendor's
client actually joins, "cross-platform" is a design property, not an observed one.

## 3. The conformance harness

`scripts/verify_oauth_flow.py` proves one path end to end. That is not enough for a claim
about combinations, so M2.1 adds a harness that puts **N host families in one room
simultaneously** and asserts:

1. every participant appears to every other, with an honest liveness grade;
2. a task claimed by one is refused to all others with `lease_conflict`;
3. a stale fence from any of them is refused;
4. one participant's disconnect releases its leases, visible to the rest;
5. events are ordered identically for all of them, gaps only where privacy filtering
   explains them;
6. an `attended` participant is never assumed prompt by an `unattended_loop` one.

Point 6 is the one that only appears in a mixed room, which is why a per-adapter test cannot
replace this.

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
- **MCP has no server-initiated wake channel.** `await_room_events` is a server-side blocking
  long-poll, described to the model as a poll. An A2A participant in the same room genuinely
  gets pushed to. Both are correct; the room reports which is which.
- **Display name is only trustworthy where a credential bound it.** With OAuth, a human bound
  the identity at consent and the agent cannot rename itself. On the local bearer path the
  name is self-chosen — fine inside a room you own, not fine across companies.
