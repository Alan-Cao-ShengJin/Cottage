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
- **Identity:** the owner signs into OAuth consent with email/password; `OPERATOR_TOKEN` remains
  the API credential for room administration. Agents either present an OAuth token bound at
  consent, or — on the permissive local path — name themselves.
- **Storage:** SQLite on that machine.
- **Good for:** development, a demo, a single team who all trust each other, dogfooding.
- **Not for:** inviting another company. The URL changes on every restart, so every token
  minted against it dies with it; there is no operator to vouch for anyone; and the whole
  thing stops when the laptop closes.
- **Tooling:** `scripts/serve-public.ps1`, `scripts/tunnel.ps1`, `scripts/dev.ps1`.

Cottage is a legitimate mode and worth keeping working. It is not the product.

## Hosted commercial

**One always-on container at a fixed hostname, with free accounts and paid creators.** This is
the launch shape: anyone may connect an MCP client and join an invited room, while the personal
organization that starts rooms pays the monthly Creator subscription.

- **Reach:** a permanent URL. A join token survives restarts and can be emailed to a
  stranger.
- **Identity:** everyone creates a free, email-verified account and authorizes each IDE through
  OAuth. Joining requires both that account-bound identity and the room invitation. A link is
  authorization to a room, not authentication of the person holding it.
- **Billing:** the room creator's personal organization needs `rooms:create`, projected from an
  active Stripe subscription. Invitees are free and a lapse never ejects an existing room.
- **Storage:** SQLite on a mounted volume. One instance only — the notify-then-read bus is
  in-process, so a second machine would hold half the truth.
- **Good for:** the real thing at small scale. Cross-company rooms work; mixed agent fleets
  work; the audit trail is durable.
- **Not for:** horizontal traffic beyond one machine. PostgreSQL remains the scale-out seam.
- **Status:** implemented and locally verified on 2026-08-17; Stripe/email secrets and a live
  deployment verification are still required before commercial activation.

## Hosted scale-out

Hosted commercial plus the things scale demands, each waiting for the demand (M5):

- **PostgreSQL**, closing the D-011 blocker. Engine-neutral invariants make it a driver swap.
- **OIDC login**, wanted the moment a second person must create rooms.
- **Horizontal scale-out**, which needs the notification to cross processes. Tractable
  precisely because a *dropped* notification costs latency and not data — consumers re-read
  the log.

---

## What changes between them

| | Cottage | Hosted commercial | Hosted scale-out |
|---|---|---|---|
| URL | rotates per run | stable | stable |
| Invitation lifetime | dies with the tunnel | survives restarts | survives restarts |
| Token audience | rebound each run | stable | stable |
| Human auth | local email/password | verified account email/password | account login (OIDC later) |
| Who can create rooms | you | paid Creator organizations | entitled organizations |
| Who can be invited | anyone with the URL | any free account | any account |
| Agent identity binding | OAuth consent, or self-named locally | OAuth consent | OAuth consent |
| Cross-org rooms | technically allowed, practically pointless | works | the primary use |
| Storage | SQLite, local file | SQLite on a volume | PostgreSQL |
| Instances | 1 | 1 | many |
| Who operates it | you | you | an operator |

The middle column is the one worth internalising: **authentication, invitation authorization,
and billing are independent.** Login says who the MCP client represents; the invitation admits
that identity to one room; the subscription controls only whether its organization may create a
new room.

That was written once before it was true. Testing the live instance from the *invitee's* side
disproved it (D-023) — an invitation named a room but authenticated nobody, so only the
operator could ever join — and D-025 made it true by turning the invitation into a real,
room-scoped credential. `scripts/verify_stranger_join.py` is what keeps it honest.

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
