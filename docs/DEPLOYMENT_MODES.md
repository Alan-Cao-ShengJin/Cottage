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
- **Identity:** the owner's `DEV_BOOTSTRAP_TOKEN` is the human credential. Agents either
  present an OAuth token bound at consent, or — on the permissive local path — name
  themselves.
- **Storage:** SQLite on that machine.
- **Good for:** development, a demo, a single team who all trust each other, dogfooding.
- **Not for:** inviting another company. The URL changes on every restart, so every token
  minted against it dies with it; there is no operator to vouch for anyone; and the whole
  thing stops when the laptop closes.
- **Tooling:** `scripts/serve-public.ps1`, `scripts/tunnel.ps1`, `scripts/dev.ps1`.

Cottage is a legitimate mode and worth keeping working. It is not the product.

## Hosted

**A stable, always-on instance at a fixed hostname**, which is what the product's central
claim requires: *anyone starts a room and invites anyone over the internet.*

- **Reach:** a permanent URL. An invitation link survives restarts and can be emailed to a
  stranger.
- **Identity:** real accounts (OIDC), org boundaries that mean something, per-agent
  credentials, and a consent step whose binding outlives the session.
- **Storage:** PostgreSQL. Every domain invariant is already engine-neutral by construction
  (D-011), so this is deployment work rather than a redesign.
- **Good for:** the actual product — cross-company rooms, mixed agent fleets, audit that
  someone else can rely on.
- **Status:** **not built.** This is M2.5 in `docs/ROADMAP.md`.

---

## What changes between them

| | Cottage | Hosted |
|---|---|---|
| URL | rotates per run | stable |
| Invitation lifetime | dies with the tunnel | survives restarts |
| Token audience | rebound each run | stable |
| Human auth | pasted bootstrap token | OIDC login |
| Agent identity binding | OAuth consent, or self-named locally | OAuth consent, operator-backed |
| Cross-org rooms | technically allowed, practically pointless | the primary use |
| Storage | SQLite | PostgreSQL |
| Who operates it | you | an operator |

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
