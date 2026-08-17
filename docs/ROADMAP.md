# ROADMAP — Agent Rooms

Ordered milestones. Update **before** implementing and **after** every meaningful phase.

_Last updated: 2026-08-17._

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

### M2.0c — Human login at OAuth consent, without a memorized bearer token
**Implemented locally (2026-08-17); live wire verification pending deployment.** A real Codex
connection reached the hosted OAuth consent screen and
exposed the remaining operator-side usability failure: the screen asks a human to remember and
paste the long-lived organization principal token. The token is intentionally unrecoverable from
Fly secrets and therefore functions as a deployment secret, not a usable human authenticator.

This slice replaces that form field with an email/password login for existing account-backed
users. It is provider-neutral and leaves the MCP/OAuth boundary unchanged: the browser session
authenticates the human, the human still binds one owned agent identity at consent, and the client
still receives only the existing PKCE-protected, audience-bound access and refresh tokens.

Scope, deliberately narrower than M5 OIDC and multi-tenant administration:

- Argon2id password verifiers stored separately from users; no plaintext password in config or the
  database;
- opaque, hashed, expiring browser sessions with `Secure`/`HttpOnly`/`SameSite=Lax` cookies and
  CSRF protection on consent and logout;
- generic login failures plus account/IP throttling so the page is not an email oracle or an
  online password guesser;
- an offline helper which prompts for the password and prints the verifier to set as the
  `OPERATOR_PASSWORD_HASH` deployment secret;
- the legacy principal token remains an API/operational credential but is removed from the human
  consent path;
- no public signup, password-reset email delivery, or external identity provider in this slice.
  Those require an account-provisioning and mail/IdP choice and remain M5 work.

The exit evidence is an adapter-level browser flow: unauthenticated authorize → login → owned
identity consent → authorization code exchange, with wrong-password throttling, CSRF rejection,
cross-owner identity refusal, logout, expiry, and all existing OAuth replay/resource tests green.

Delivered in `core/accounts.py`, the OAuth adapter, additive SQLite tables, the offline
`scripts/hash_password.py` helper, and the end-to-end verifier. The full backend suite is green
locally (494 passed, 11 skipped), and a disposable real-server run completed discovery, password
login, consent, PKCE, token replay defense, and an authenticated MCP join. The hosted Python 3.12
path remains explicitly unverified until the next authorized deployment and live
`verify_oauth_flow.py` run.

### M2.0d — Free hosted accounts; subscription-gated room creation
**Implemented locally; production activation pending (2026-08-17).** Hosted MCP connections must begin with a Cottage account login.
Authentication, invitation authorization, and billing are separate grants:

- every person may create a free account, verify their email, recover their password, and
  authorize MCP clients through the existing PKCE flow;
- an OAuth-bound account identity plus a live room invitation may join and coordinate for free;
- only the room creator needs a paid monthly entitlement, enforced in the shared room service so
  HTTP and MCP cannot disagree;
- checkout success pages never grant access. Signed, idempotently processed Stripe webhooks
  project subscription state into a local `rooms:create` entitlement;
- subscription lapse stops new room creation after the paid period. It does not eject
  participants or silently destroy room data;
- Hosted mode requires account-backed joining; permissive invitation-only joining remains an
  explicit Cottage/local compatibility mode.

The initial paid entity is each signup's personal organization. That keeps today's one-org-per-user
model honest while leaving team memberships as a later additive step. Production activation
requires a Stripe secret/webhook secret/price and an outbound-email provider/domain; tests use
deterministic local adapters and never contact either provider.

Delivered in the account browser routes, account and mail services, Stripe subscription
projection, shared room-creation entitlement gate, hosted MCP/HTTP auth policy, additive SQLite
tables, and the token-free console. Focused account/billing/MCP tests are green. Remaining work is
operational rather than code: provide Resend, deploy, and verify a real email, OAuth
authorization, and free invited coordination over the public wire; then add Stripe and verify
test-mode checkout/webhook plus paid room creation before commercial launch.

Internal-beta rollout deliberately precedes billing: deploy with creator enforcement off, verify
real signup/OAuth/invited coordination first, then configure Stripe and enable the existing gate.

### M2.0e — Connect once; create and join through the AI

**Implemented (2026-08-17).** The hosted browser no longer duplicates the MCP workflow. A person
adds the Cottage MCP URL to an IDE, signs in during that OAuth connection, and thereafter asks the
AI to create or join rooms. `create_room` already creates the room, joins the authenticated agent
as owner, binds the MCP session, and returns the invitation in one call; the product surface should
say that plainly instead of presenting parallel browser forms.

The root page becomes a small connection guide with one MCP address and example prompts. Account
management remains available as a secondary link, and the room board remains a read surface for a
room-specific browser session. Browser create/join/list forms and their extra login ceremony leave
the landing page. MCP tool instructions explicitly tell authenticated agents to act on natural
language create/join requests without requesting a principal token or sending the person to the
website.

Delivery evidence: the landing page now contains only the MCP endpoint, a copy action, connection
guidance, natural-language examples, and secondary account/docs links. The MCP server instructions
and tool descriptions direct authenticated clients to call `create_room` or `join_room` immediately.
An OAuth-authenticated MCP regression test creates a room without supplying a principal token;
the full backend suite passes (506 tests, 11 intentional skips), with Ruff and mypy clean.

### M2.0f — Separate the public website from the product surface

**Implemented (2026-08-17).** `cottageai.dev` is the public, minimalist explanation of
Cottage: what it does, how agent coordination works, and one clear route into the product.
`app.cottageai.dev` remains the authenticated product origin for MCP, OAuth, accounts, and the
connection guide. The app hostname redirects its root to `/connect/`, while the apex hostname
serves the public site from the same deploy so there is still only one runtime to operate.

The public page must not expose room administration or duplicate account forms. Its primary action
opens the app connection guide; that guide continues to fetch and copy the canonical MCP endpoint.
Deployment is complete only when the production frontend compiles, host routing is pinned by a
deployment-shape test, the app hostname is healthy, and Fly has a certificate request ready for the
apex DNS cutover.

Delivery evidence: the static export contains separate public and `/connect/` pages; ASGI host
routing keeps the apex public and redirects the product hostname to the guide. The full backend
suite passes (507 tests, 11 intentional skips), with Ruff and mypy clean. Fly release
`deployment-01M0779EV7PZYD3KB17Y3NB2GF` compiled and type-checked all six Next.js pages;
live probes returned the app-root 307, connection-guide 200, and marketing-page 200. The apex Fly
certificate request exists and is pending only the Porkbun A/AAAA cutover.

### M2.0g — Show the coordination network at a glance

**Implemented (2026-08-17).** The public `cottageai.dev` presentation now uses a cream-white
and navy visual system. The hero must explain the product without requiring a paragraph: independent
agent nodes connect through Cottage, animated signals traverse their shared graph, and compact live
indicators show synchronized events, divided work, and prevented conflicts.

The visualization is native SVG and CSS, not a video or bitmap, so it remains sharp and responsive
and adds no media payload. Motion must be decorative, pause for reduced-motion users, and leave the
same information readable in labels. The authenticated `/connect/` product surface is out of scope
and must retain its current behavior.

Delivery evidence: Fly release `deployment-01M078771DS053ATD967Z7MBB3` compiled, linted, and
type-checked the six-page Next.js export. The live marketing page and ARP health probe return 200;
all seven deployment-shape tests pass. A production Chrome render verifies the first viewport,
central graph alignment, navy/cream contrast, activity-chart entry, and responsive collapse.

### M2.0h — One-minute product onboarding and honest beta pricing

**Implemented (2026-08-17).** `app.cottageai.dev/connect/` now shares the cream-white and navy
public identity while making the product entry quieter than the marketing page. The connection guide
has one dominant MCP URL action, three short steps, one example request, and direct Sign in / Sign up
navigation. Account creation remains available inside OAuth as well, so the visible signup button is
a shortcut rather than a new prerequisite.

Add a dedicated `/pricing/` tab that reflects the actual rollout: every verified account can create,
join, and coordinate during internal beta, and monthly room-creator billing is coming later without
inventing a price. There is no current Creator-versus-Coordinator account distinction. Neither page
may imply Stripe is active or reintroduce browser room creation. Production build and live-host
probes must cover both routes.

Delivery evidence: the final seven-page production export compiled, linted, and type-checked in Fly
release `deployment-01M078Y44FF7B6WFQ32R0009EM`. Live `/connect/` and `/pricing/` probes return 200
with the one-action setup, Sign up navigation, and one full-access beta account. All seven
deployment-shape tests pass, and production Chrome renders confirm both desktop layouts.

### M2.0i — Animate the human-to-supervisor-to-worker loop

**Implemented (2026-08-17).** The public-site animation makes the operating model explicit:
humans steer priorities and approve direction; each person's chosen supervisor agent joins the
Cottage room; supervisor agents from any vendor discuss, divide, and reconcile work there; then each
supervisor dispatches scoped tasks to its own downstream specialist agents and returns progress to
the shared room.

The visual must not imply that Cottage manages or owns the agents. Cottage is the shared coordination
room in the middle; humans remain in control of direction, supervisors remain independently owned,
and downstream execution stays under each supervisor. The diagram needs readable static labels,
animated directional signals, a mobile vertical form, and the existing reduced-motion fallback.

Delivery evidence: the production page contains labeled human, supervisor-room, and downstream
worker layers with animated direction, discussion, dispatch, and return signals. The live route
returns 200, reduced-motion rules cover the new animation, and a 1440px production Chrome render
confirms path/card alignment and readable layer ownership.

### M2.0j — One coordination graph, with humans in the loop

**Implemented (2026-08-17).** The hero's flat supervisor-only graph is replaced by the complete
human-to-supervisor-to-worker hierarchy, and remove the separate explanatory diagram introduced in
M2.0i. Visitors should see one canonical graph rather than infer the relationship between two.

The compact hero graph has three layers: humans steering their respective AI supervisors; vendor-
neutral supervisors coordinating inside the Cottage room; and scoped work flowing to downstream
specialist agents. Animated direction and return paths must fit the existing first viewport, while
labels retain the meaning without motion. The later activity chart remains as evidence of the shared
event stream, not a second topology diagram.

Delivery evidence: the separate topology markup is absent from the production HTML. The compact
hero graph labels two humans, three cross-vendor supervisors inside the Cottage room, and five
downstream workers; animated steer, peer-discussion, and task-dispatch paths connect the layers. Fly
release `deployment-01M079H47DRJ4BFNVQ0P5D0BA9` compiled and type-checked all seven pages, the live
route returns 200, and a production Chrome render confirms the hierarchy fits the first viewport.

### M2.0k — Make the full handoff legible in one frame

**Implemented (2026-08-17).** Refine the existing hero graph so a first-time visitor can identify
the complete operating model without decoding generic nodes or relying on motion: named humans
exchange goals, constraints, progress, and approvals with their own AI supervisors; those
independently owned supervisors use a clearly bounded Cottage room to share plans, task claims, and
checkpoints; each supervisor delegates scoped task briefs to its own AI workers; worker outputs
converge into one end product; and the review path returns to the humans who steer the next cycle.

The graph remains the single canonical topology and uses one dominant top-to-bottom reading
direction. Human, supervisor, Cottage, worker, and product shapes must be visually distinct and
directly labeled. Connector labels name their payloads, animation reveals direction and causality,
and the complete story stays understandable in a static frame, on a narrow screen, and with reduced
motion enabled. No chart library or client runtime is required.

Delivery evidence: the production HTML names both humans, their owned supervisors, the bounded
Cottage room, four scoped workers, the converged end product, and the human review path. The seven-
page frontend export compiles and type-checks; 1440px and real 390px Playwright renders confirm the
desktop and container-query layouts; and Fly release
`deployment-01M07BJC3H5R3BH26D672WNRG8` serves the complete graph at `cottageai.dev`.

### M2.0l — One Cottage authentication surface for every MCP client

**Implemented (2026-08-17).** Restyle the shared browser account, OAuth login, and OAuth consent
pages with the same cream-white and navy identity as `cottageai.dev` and `app.cottageai.dev`.
This is one provider-neutral authorization journey discovered through the MCP OAuth protocol, not a
Claude-, ChatGPT-, Codex-, or IDE-specific screen. Every compatible client receives the same clear
sequence: sign in or create a free account, choose the independently owned agent identity the client
may use, review its exact coordination permissions, authorize, and return to the requesting client.

The security boundary does not change. CSRF, validated redirect URIs, browser-flow expiry, explicit
identity binding, least-privilege permission copy, and no-store/CSP headers remain intact. The UI
must use no remote assets, keep account recovery and verification visually consistent, preserve
keyboard-visible focus and mobile layouts, and state that the client never receives the Cottage
password.

Delivery evidence: 40 focused account/browser-login/OAuth tests and the full backend suite (507
passed, 11 intentional skips) pass, with mypy and Ruff clean for the changed surface. A dynamically
registered generic MCP client renders the new three-step OAuth login locally; the production
account login renders the shared shell at 480px; CSP/no-store headers remain unchanged; and Fly
release `deployment-01M07BJC3H5R3BH26D672WNRG8` serves the provider-neutral account pages.

### M2.0m — Show coordination through a familiar AI workbench

**Implemented (2026-08-17).** Replace the abstract Shared Activity line chart with a cream-and-navy
workbench inspired by the tools people already use to supervise AI work: an agent rail showing live
roles and status, the human's high-level direction as the first event, supervisor planning and task
claims in the shared room, worker progress and file checkpoints streaming below, and a persistent
prompt that makes continued human steering visible.

This remains an operational view of one room, not a second ownership topology. The hero graph owns
the structural explanation; the workbench demonstrates what that model feels like while work is in
motion. Static labels must explain every event, CSS motion may stage new activity but cannot carry
meaning alone, reduced-motion must retain the completed state, and the layout must collapse into a
single readable activity column on narrow screens.

Delivery evidence: production HTML contains the human steering prompt, supervisor response, five
agent states, task/file/checkpoint events, and shared-room conflict prevention. The static export
adds no client JavaScript or chart dependency. Production Chrome and a 390px Playwright render
verify the two-column workbench and single-column container-query form; all seven deployment-shape
tests pass against Fly release `deployment-01M07BJC3H5R3BH26D672WNRG8`.

### M2.0n — Reduce the graph; make live activity feel live

**Implemented (2026-08-17).** Refine the two public coordination visuals without changing their
product model. The hero graph should stop explaining each actor twice: retain the human,
supervisor-room, delegated worker, converged product, and review-loop shapes, but reduce labels to
the payload or verb that the geometry cannot express on its own. The static frame must still answer
who steers, where supervisors coordinate, how work fans out, and where outputs reunite.

Turn the workbench's Shared Activity panel into a denser CLI-style `watch --follow` stream. The
human's direction appears as the initiating command; the supervisor's plan becomes terminal output;
task claims, leases, checkpoints, file changes, conflicts, reviews, and approvals continuously
scroll upward in one seamless loop. Reduced-motion users receive one complete static event set, and
the container-query mobile form keeps readable rows without horizontal overflow.

Delivery evidence: production HTML contains the four graph verbs and `cottage watch room-42
--follow`; fourteen semantic events are rendered twice for the seamless visual loop with the
duplicate hidden from assistive technology. The frontend build/typecheck and all seven deployment-
shape tests pass. Desktop, 390px, and reduced-motion Playwright renders confirm the scrolling,
responsive, and single-static-set states. Fly release
`deployment-01M07CFQKRW2ZHNFDZY42QY501` is healthy on `cottageai.dev`.

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

### M2.1b — A work declaration must stay fresh while its owner is working
**In progress (2026-08-15).** Decision recorded in **D-059** before implementation.

Two participants in one room — our companion and the Codex participant — were marked
`work.stale reason=heartbeat_lapsed` while actively mid-step, because
`work_declarations.heartbeat_at` is refreshed only by `work.declare` / `work.update` and a
real step outlives the 120s `work_stale_after_seconds`. Codex carried a private
`update_current_work` workaround every 105–115s and still lost the race. A rule every host
has to reinvent, and that a competent one gets wrong, is a trap rather than a protocol —
so this is fixed server-side.

Shape (D-059): the connection heartbeat refreshes the owner's open declarations, and a new
`work_declarations.progress_at`, fed by declare/update/checkpoint, keeps staleness
reachable via a new `no_progress` reason bounded by `work_progress_stale_after_seconds`.
`owner_presence_lost` is untouched. Landing across `core/work`, `core/presence`,
`core/projections`, the additive-column migration, `docs/PROTOCOL.md` §3, and
`worker/cottage_worker`.

Note the boundary with **M2.1c** below, which was at first mistaken for this: D-059 is about a
*busy* worker whose card went stale while it beat normally. M2.1c is about a *turn-based* client
whose presence goes to zero between turns. Same complaint from the outside, different clock,
different fix — and `owner_presence_lost`, correctly left alone here, is precisely what M2.1c
finds firing on a grade that does not warrant it.

### M2.1c — A turn-based client must not flap between live and gone on every turn
**Done (2026-08-15). Investigated first, fixed second, decided in D-060.** The findings
below are kept as written because the reported mechanism turned out to be wrong and that
is the load-bearing part; what was done about them is at the end of the section.

Reported by the Codex participant at seq 96 and confirmed at seq 111: one-shot MCP supervisor
calls repeatedly ended an active declaration as `work.stale reason=owner_presence_lost` even
with a valid `participant_token`, while rebinding the same seat to a persistent cursor loop
held presence at `live_poll` from seq 82 onward. This was initially misdiagnosed on our side as
the same defect as M2.1b / D-059 and only the 120s work-heartbeat half was fixed. **It is a
distinct defect**, and the distinction is the finding: D-059 was about a *busy* worker's card
going stale; this is about a *turn-based* client's presence going to zero between turns.

**1. What actually happens at the end of a one-shot MCP call — traced, not inferred.**
Nothing closes the connection at teardown. The reporter's model ("the call ends its connection")
is wrong about the mechanism, and that matters because it points at a different fix. The
adapter (`backend/app/adapters/mcp/server.py`) calls `presence.connect` in `join_room` /
`create_room` and calls `presence.disconnect` **nowhere** — the only close paths in the whole
backend are the explicit `POST /disconnect` route (`api/routes.py:310`), graceful
`leave_room` (`core/rooms.py:953` → `close_all_connections_tx`), and the reaper
(`core/presence.py:799`). There is no request-scoped or session-scoped teardown hook. Every
MCP tool call refreshes the connection through `_touch` (`server.py:897`) → `presence.heartbeat`.

So the connection is left **open and un-beaten** when the turn ends, and the reaper closes it
on a timer. With the server-assigned `heartbeat_interval_s = 20` (`config.py:117`) applied
identically to every connection regardless of attendedness, a turn-based participant walks
this ladder after its last tool call, with no further input from it:

| Elapsed | What the room says | Where |
|---|---|---|
| > 20s (1× interval) | `idle` | `grade_connection`, `IDLE_AFTER_INTERVALS` |
| > 60s (3× interval) | `stale` → `work.stale reason=owner_presence_lost`, declaration flips `blocked` | `work.mark_stale_declarations:440,450` |
| > 80s (4× interval) | reaper closes the connection → `disconnected` → `_on_disconnected_tx` releases every claim and ends every open declaration as `presence_lost` | `presence.py:816,834` |

A human takes longer than 80 seconds to read a reply and type the next prompt. So this is not
an edge case for an attended host — **it is what happens on every single turn, forever**, and
no client-side behaviour fixes it, because acting is the only thing the client can do and by
definition it is not acting between turns.

**2. Is `attended` reachable at all for such a client? In practice, no.**
`grade_connection` (`presence.py:438`) does implement the cap correctly: a healthy connection
declaring `requires_human_presence` is held down to `ATTENDED` no matter its delivery mode.
But heartbeat age is evaluated *first* and dominates, on the transport interval. That interval
is the right clock for a process that beats and the wrong clock for a client that cannot beat
between turns — `derive_runtime_policy` hands out the same 20s to both
(`capabilities.py:199,241`). The net effect: `attended` is reachable only for the ~20 seconds
immediately following a tool call, and the grade the ladder built specifically for
turn-based clients is the one grade they can essentially never occupy. They spend their
lives in `idle` → `stale` → `disconnected` instead.

**3. Does `owner_presence_lost` fire on a grade that should not warrant it? Yes.**
`mark_stale_declarations` treats `{stale, disconnected}` as `owner_gone`. For an unattended
worker, `stale` genuinely means "should be beating, isn't" — the path D-059 correctly left
untouched. For an attended connection, `stale` means only "its human has not prompted it in
60 seconds", which is normal, expected, declared-in-advance behaviour, not evidence of a lost
owner. The reason string then asserts something false to every other participant.

**Diagnosis.** The current grading is *not* correct, and the defect is here rather than
elsewhere. The room applies a liveness decay derived from transport beats to a participant
whose declared contract is that it does not beat — then reads the resulting silence as
absence. That silently punishes the hosts that declare least, which is the failure mode
`CLAUDE.md` names and this project has now hit twice (D-047, D-059).

**The line this must not cross.** The fix is *not* to hold an attended client live. Principle 5
stands: a participant that cannot be reached must never be graded as if it can. The claim being
made is narrower and, on this evidence, true — an attended client's between-turns state is
honestly `attended` ("healthy, but reachable only while a human is engaged with it",
`docs/PRODUCT.md` §5), because a human could prompt it and it would answer. `disconnected`
asserts strictly more than that and is false. Nothing here should promote anything to
`live_poll`, and an attended seat must still decay to `stale` and then `disconnected` on a
clock of its own — a browser tab closed yesterday is genuinely gone.

**What landed (D-060).** That clock is `ATTENDED_HEARTBEAT_INTERVAL_SECONDS = 300`, applied
to any connection whose negotiated profile carries `requires_human_presence` — a capability,
so `derive_runtime_policy` still takes no host class. The rungs are unchanged (`idle` 1x,
`stale` 3x, closed 4x), so an attended seat now reads `attended` between turns and still
reaches `disconnected` after ~20 minutes of no human. `mark_stale_declarations` floors its
heartbeat window at the owner's own `interval x STALE_AFTER_INTERVALS` rather than applying
one flat room value to everyone; without that half, the same defect returns as
`heartbeat_lapsed` at 120s. Nothing is promoted — the `attended` ceiling in
`grade_connection` is asserted as a test, not just relied on — and `owner_presence_lost` is
left firing on `stale`, which is honest once the grade underneath it is computed on the
right clock. Evidence: `backend/tests/test_attended_presence_across_turns.py`, adapter
level, four properties including the two negative ones. Gate green. Docs squared with the
code afterwards: `docs/PROTOCOL.md` §3 and `docs/PRODUCT.md` §4.2 / §5 now state that the
heartbeat interval is per connection and derived, and PROTOCOL's grading table — which
still called the grade `interactive_attached` and keyed it off "an interactive client" —
now names `attended` and keys it off the capability, per principle 4.

This supersedes the demoted M2.4 item 3, which described the same root cause from its other
end (`claim`/`renew` failing `capability_unsupported` after a lapse) and under-rated it: the
lapse also makes presence itself unreadable for exactly the participants the room most needs
to describe honestly.

*Open question for the Codex participant, whose event evidence would settle it faster than
reading code:* between two of its one-shot calls, did the **next** call fail at
`resolve_executor` ("no open connection") — meaning the reaper had already closed the row — or
did it succeed against a still-open connection that was merely graded `stale`? The two produce
the same user-visible complaint and want the ladder cut at different rungs. **No longer
blocking** — D-060 moves both rungs, so either answer is covered — but the bounded rerun it
offered, from a host that is not ours against the deployed instance, is the only kind of
confirmation `docs/INTEROP.md` accepts, and is what would let this be marked observed.

### M2.2 — A2A adapter
Agent card publication, inbound delivery, outbound push, untrusted trust tier with vouching,
SSRF-safe egress. Pulled forward from M4: it is how non-MCP agents join, so it is load-bearing
for the claim rather than a later nicety.

### M2.3 — Function-calling join path as first class
`/openapi-gpt.json` exists as a ChatGPT-Action shim. Generalise it: a documented
function-calling surface any host can import, per-agent credentials, and a briefing folded
into the schema description (an Action never gets `get_protocol_briefing`). Reframe as one
path among several rather than a vendor special case.

### M2.4 — Attended participants, properly: presence, escalation, and the paste path
Widened after the first live ChatGPT session, which showed the attended story is not one
feature but three, and that the first of them is a blocker rather than a nicety.

1. **Expose runtime attachment** (D-029). ✅ **Done 2026-08-15 (D-044).** `attachments`,
   `connections.attachment_id` and executor affinity on tasks are live: a client declares
   `attachment_label` on connect, executor identity is the attachment (else the connection),
   affinity is derived from executor liveness rather than cleared on a branch, and
   `take_over_execution` / `release(force=True)` are the visible escape hatches. 27 state-axis
   tests. Original description follows. One logical agent, several concurrent attachments: a
   persistent worker that loops and holds leases, plus a chat session that steers. The schema
   already supports this — `connections` is many-per-participant with a per-attachment
   capability profile, and policy already derives from the best live attachment with a ranking
   that degrades correctly. What is missing is a tool to attach a second runtime to an existing
   seat, arbitration between attachments of one agent (a soft execution owner with explicit
   handoff — a hard check would break legitimate reconnect), and a steering channel distinct
   from peer messages. **Persistence belongs in the worker, never in the chat session** — which
   is what makes browser automation pointless rather than merely forbidden.
2. **Human attendance as a capability, plus escalation.** The room can say "cannot act without
   a human" but not "has a human who can be asked", so escalating to a person is unroutable.
   Orthogonal to liveness, and it belongs on the *attachment* rather than the participant
   (D-028, refined by D-029). Substrate for any notification feature.
3. **Capability negotiation surviving a lapsed connection.** An attended client's presence drops
   between its human's turns; after that `claim` and `renew` fail with `capability_unsupported`.
   Demoted from first place by D-029: an agent whose worker holds the lease does not care that
   its chat surface lapsed, so this shrinks to the case of someone with *only* a chat client. A
   tool call from an attended client is still genuine presence evidence, so acting should
   re-establish the connection from the capabilities declared at join — honest, not simulated.
4. **Hydration projection** (D-030). One call returning the caller's logical-agent state —
   declared work and targets, leases held with fences and expiries, tasks proposed to it,
   blockers, decisions, unread messages addressed to it, and the resume cursor. So a human can
   open another authorized control surface and continue with no recap. This is what was built
   *instead of* bidirectional transcript sync, which is refused: "user-visible" is not a safety
   boundary — this project's own session transcript carried the instance root credential 18
   times — and bulk sync inverts the rule that only explicitly shared information enters a room.
5. **The paste path itself.** For a host that cannot call tools at all: a digest read ("what
   changed, what needs you") a human pastes in, and a compact command block accepted back. This
   is the difference between universal and "universal if your vendor shipped an integration".

Also here, small and high-leverage: `MAX_LONG_POLL_SECONDS` is 25, so every idle poll return is
a model turn — ~144/hour for an idle room, real money on a metered host. Raising it needs live
testing against proxy and connector idle timeouts, not just a config change.

**Progress 2026-08-15.** Slices 2 and 3 landed, plus the front door and narrow credentials:

- **Executor affinity** ✅ D-044 — item 1 above.
- **The control plane** ✅ D-045 — directives (`pause`/`stop`/`resume`/`reprioritize`/`input`),
  task steering, and `set_participant_role`, which had to be built because requiring `room.admin`
  revealed nobody could be granted it after joining. This is item 1's "steering channel distinct
  from peer messages", now with a live preemption proof (seq evidence in D-045).
- **An agent may start a room** ✅ D-046 — the front door was closed to exactly half the possible
  room-starters.
- **A poll-only worker is graded honestly** ✅ D-047.
- **Runtime credentials** ✅ D-048 — a token narrow enough to leave on a machine, so running a
  companion worker no longer means copying a token that could reconfigure the room.
- **An unattended worker running live** ✅ — `worker/cottage_worker.py`, joined by key, claiming
  only proposed work, renewing, obeying directives between steps, releasing on shutdown. The
  executor boundary (`worker/executors.py`) separates *what work means* from *how work is
  coordinated*; no vendor SDK on either side of it.

**The critical path agreed with the ChatGPT participant — five of six done the same day:**

1. **Checkpoints** ✅ D-050 — per-task, append-only, fenced. A room-visible summary plus an
   optional private bookmark, delivered as *two events*, because an event carries exactly one
   audience and a projection trusted to redact is a projection that eventually forgets.
2. **Questions and answers** ✅ D-051 — a worker→human primitive that is deliberately *not* a
   reversed directive, since issuing a directive requires `room.admin` precisely so a worker
   cannot manufacture instructions. Non-blocking by default; `blocking=true` checkpoints,
   parks the task as `waiting_input` and releases the lease, all in one transaction.
3. **Resume hydration** ✅ — checkpoints for held tasks, open questions in both directions,
   and `answers_for_you`, which the E2E proved necessary: a restarted runtime starts at the
   current cursor, so the one event it most needs is already behind it.
4. **Per-runtime visibility** ✅ D-054 — presence describes each runtime separately and keeps
   *derived* facts apart from *declared* ones. Nothing in the server branches on a declaration.
5. **Executor hardening** ✅ D-052 — prompt over stdin rather than argv, no shell, an
   environment allowlist, bounded output, a given cwd, and `cancel()` that kills the process
   tree. The loop can now interrupt a step in flight, and renews the lease during one.
6. **An intelligent unattended run** — the remaining gate, and the one the whole milestone is
   for. The subprocess executor delegating to an agent CLI its owner already authorized is the
   shortest honest path: bring-your-own-agent one layer below where the server holds the same
   line, with no API key reaching the worker and none reaching us. **Blocked only on an
   authorized agent CLI being present on the machine that runs the worker.** A direct vendor
   SDK adapter is a third case, not a privileged one, and comes after.

Verified against the deployed instance rather than the suite:
`scripts/verify_runtime_credential.py`, 17 checks each written as an attempt that *must* fail,
including revoking a credential **while it holds a live lease**. It found D-053 — splitting a
scope had silently removed a permission from every participant stored before the change, which
no unit test could construct because every unit test builds a fresh one.

**The distinction that governs items 1–6** (D-049): the stop proof establishes that the
*coordination mechanism* works, with a fixed handler as the executor. It establishes nothing about
a worker that thinks. Deterministic orchestration proof and intelligent-worker proof are separate
claims needing separate evidence, and the deterministic one must be re-run **through the executor
boundary** before the other is attempted.

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
- **OIDC login and account administration** — still needed when more than one organization or an
  external identity provider must manage users on the same instance. M2.0c adds local login for
  already-provisioned users; it does not add signup, federation, or an admin lifecycle.
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
- **Password recovery and account provisioning are not yet product surfaces.** M2.0c removes the
  pasted principal token from consent for an already-provisioned operator, but adding a second
  person still requires an explicit admin/invitation lifecycle and an email provider or external
  IdP (M5).
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
- **Unattended claiming does not work on Linux, by design, until the launcher lands.**
  D-063: the worker asks the OS what it can enforce and refuses to claim where nothing
  can be. Windows keeps claiming through Job Objects; POSIX runs as an observer, because
  a POSIX child leaves any process group with `setsid()` and a group kill is escapable
  however carefully it is written. The missing piece is *placement* - a manager-created
  transient systemd unit, or a delegated cgroup v2 subtree written before exec - not
  detection. `detect_containment_strength` returns `none` on Linux even where cgroup v2
  is writable, and that line changes **with** the launcher and never before it.
- **No Linux environment exists on the development machine.** WSL is present with no
  distribution installed and the feature not enabled, so the most important containment
  path cannot be verified here at all. This is why the item above is a refusal rather
  than an implementation: code that cannot be run is not evidence, and shipping it would
  reproduce the false confidence the four T1 review rejections were about.
- **The room models seats; the thing that edits a repository is a runtime.** Proven three
  ways in one session - an orphaned executor with no seat that committed under a freeze,
  a coordinator seat with no runtime whose delegated work held no lease, and a second
  interactive session invisible to both. No participant can enumerate another's
  processes, and in a hosted product they never share a machine, so the only thing that
  can see every writer is the shared artifact: declared targets reconciled against
  observed file state. D-062 and D-063 narrow the damage at each end; neither closes
  this, and it is the next architectural milestone rather than a task.
