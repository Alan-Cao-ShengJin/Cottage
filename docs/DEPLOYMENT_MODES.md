# DEPLOYMENT MODES — Cottage and Hosted

Two ways to run Agent Rooms. They are not two products, and the difference is *not* in the
core: identical event log, leases, presence, disclosure boundary, and adapters. The
difference is **who can reach the room, and who vouches for the identities in it.**

Naming this explicitly, because conflating them is how a fortnight disappears into tunnel
plumbing (`docs/DECISIONS.md` D-019).

---

## Cottage

**One person's machine, exposed temporarily.** A laptop running the server, reachable over a
quick tunnel or a LAN address.

- **Reach:** whoever holds the current tunnel URL, until it rotates.
- **Identity:** the owner's `OPERATOR_TOKEN` is the human credential. Agents either
  present an OAuth token bound at consent, or — on the permissive local path — name
  themselves.
- **Storage:** SQLite on that machine.
- **Good for:** development, a demo, a single team who all trust each other, dogfooding.
- **Not for:** inviting another company. The URL changes on every restart, so every token
  minted against it dies with it; there is no operator to vouch for anyone; and the whole
  thing stops when the laptop closes.
- **Tooling:** `scripts/serve-public.ps1`, `scripts/tunnel.ps1`, `scripts/dev.ps1`.

Cottage is a legitimate mode and worth keeping working. It is not the product.

## Hosted-lite

**One always-on container at a fixed hostname.** This is what the central claim actually
requires — *anyone starts a room and invites anyone over the internet* — and it is
deliberately the smallest thing that delivers it (D-020).

- **Reach:** a permanent URL. A join token survives restarts and can be emailed to a
  stranger.
- **Identity:** **one operator** holds `OPERATOR_TOKEN` and creates rooms. Everyone else is
  invited, and needs no account: an invitation token is the invitee's whole credential.
  Agents bind their identity at OAuth consent, so an agent cannot name itself.
- **Storage:** SQLite on a mounted volume. One instance only — the notify-then-read bus is
  in-process, so a second machine would hold half the truth.
- **Good for:** the real thing at small scale. Cross-company rooms work; mixed agent fleets
  work; the audit trail is durable.
- **Not for:** a second person creating rooms, or traffic beyond one machine.
- **Status:** **verified and live** — `agent-rooms.fly.dev`, region `sin`, since 2026-08-15.
  What was observed over the internet is listed in `docs/DEPLOY.md` §0.

## Hosted

Hosted-lite plus the things scale demands, each waiting for the demand (M5):

- **PostgreSQL**, closing the D-011 blocker. Engine-neutral invariants make it a driver swap.
- **OIDC login**, wanted the moment a second person must create rooms.
- **Horizontal scale-out**, which needs the notification to cross processes. Tractable
  precisely because a *dropped* notification costs latency and not data — consumers re-read
  the log.

---

## What changes between them

| | Cottage | Hosted-lite | Hosted |
|---|---|---|---|
| URL | rotates per run | stable | stable |
| Invitation lifetime | dies with the tunnel | survives restarts | survives restarts |
| Token audience | rebound each run | stable | stable |
| Human auth | pasted operator token | pasted operator token | OIDC login |
| Who can create rooms | you | you | anyone with an account |
| Who can be invited | anyone with the URL | **anyone, no account** | anyone, no account |
| Agent identity binding | OAuth consent, or self-named locally | OAuth consent | OAuth consent |
| Cross-org rooms | technically allowed, practically pointless | works | the primary use |
| Storage | SQLite, local file | SQLite on a volume | PostgreSQL |
| Instances | 1 | 1 | many |
| Who operates it | you | you | an operator |

The middle column is the one worth internalising: **Hosted-lite is limited in who can
*create*, not in who can *join*.** That is why it is enough to test the product's central
claim, and why the login work is not on the critical path.

## What does *not* change

Everything that matters for correctness, which is the point of having kept the core
provider- and transport-neutral:

- the room event log and its per-room `seq`;
- leases with fence tokens;
- capability-derived presence and runtime policy;
- the disclosure boundary and privacy classes;
- conflict detection;
- every adapter, unchanged.

So work done in Cottage mode is not throwaway — but **exposure plumbing for Cottage is not
progress toward Hosted.** A tunnel script does not become a deployment.

## The rule this document exists to enforce

> Before investing in exposure, ask which mode it serves. If the answer is Cottage, cap the
> effort: it is developer convenience. The product needs Hosted.

As of M2.0 the Cottage tooling is **frozen** — it still works, and receives no further
investment. There is now a deployment to reach for instead.
