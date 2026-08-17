# DEPLOY — getting a stable URL

This is **Hosted-lite** (`docs/DEPLOYMENT_MODES.md`): one always-on instance at a URL that
survives a restart. It is what makes the product's central claim true in practice — *anyone
starts a room and invites someone over the internet* — and it is deliberately the smallest
thing that achieves it (`docs/DECISIONS.md` D-020).

**What it is not yet:** PostgreSQL, multi-operator login, or more than one machine. Each is
in M5, wanted when something actually demands it. Skipping them costs a later day; doing
them first costs the days before anyone can join.

---

## 0. Status of this document

**Verified.** `agent-rooms.fly.dev` is live in `sin`, and the following was observed over the
public internet on 2026-08-15:

- `/healthz` returning `publicly_reachable: true`, `mcp_requires_auth: true`, `console: true`;
- the console served from `/`, `/room/`, and its `_next` assets, same origin as the API;
- `scripts/verify_oauth_flow.py` green end to end — 401 challenge, RFC 9728 + RFC 8414
  discovery, dynamic registration, consent binding the identity, PKCE rejection of a wrong
  verifier, code-replay refusal *with* revocation of what the replay bought, then MCP
  `initialize` and `join_room` where the spoofed display name lost to the token-bound one and
  the participant graded `attended`;
- a room created by the operator, and an idempotent replay of the same `command_id` returning
  the same room rather than minting a second;
- rooms and their `event_seq` surviving a redeploy unchanged, so the volume genuinely holds the
  event log;
- **a stranger holding only a join token joining over the internet**, over both ARP HTTP and
  MCP, becoming a working participant — and being refused everything else (D-025). This is the
  product's central claim, and §5 has the command that re-checks it.

You do **not** need Docker locally: `fly deploy --remote-only` builds on Fly's builder.

**Commercial account/billing update:** implemented and locally verified on 2026-08-17, but not
yet claimed as live. The next deployment must supply the Resend and Stripe values in §2.1 and
then repeat OAuth/MCP plus a Stripe test-mode webhook/checkout verification.

**The first deploy failed, and it is worth knowing why** (D-022). The container runs Python
3.12 while the dev venv is 3.10, and `aiosqlite` drives its connection from a worker thread
while the `isolation_level` property setter runs on the caller's thread — a same-thread
violation that 3.12 enforces and 3.10 does not. 179 green local tests could not see it. If you
change anything in `db/database.py`, deploy and watch `fly logs`; passing tests are not
sufficient evidence here.

## 1. What you are deploying

One container. A Node stage compiles the room console to static files; a Python stage serves
both the API and those files on a single port. One origin, so there is no CORS matrix and no
second deployment to keep in sync.

| Path | Serves |
|---|---|
| `/` | the room console (create a room, copy the join token, watch the board) |
| `/healthz` | liveness, plus the URLs this instance is advertising |
| `/mcp` | the MCP join path — this is what an agent host connects to |
| `/api/...` | the ARP HTTP + SSE surface |
| `/.well-known/oauth-*` | OAuth 2.1 discovery, so a client can register itself |
| `/account/...` | signup, verification, login, recovery, and billing management |
| `/billing/stripe/webhook` | signature-verified Stripe subscription projection |
| `/openapi-gpt.json` | the function-calling / Action schema |

## 2. Three settings that are not optional

The first two are enforced by public-startup guards. The third is required for a human to
complete OAuth login; without it the server starts but deliberately authenticates nobody.

1. **`OPERATOR_TOKEN`** must not be the published default. It is a bearer credential for the
   account that creates rooms; the default is printed in this repo.
2. **`MCP_REQUIRE_AUTH=true`**, or `/mcp` would accept tool calls from anyone who found the
   URL.
3. **`OPERATOR_PASSWORD_HASH`** must contain the Argon2id verifier for the password used at
   OAuth login. Generate it interactively so the password never enters shell history:

   ```powershell
   backend\.venv\Scripts\python.exe scripts\hash_password.py
   ```

Generate a token:

```powershell
# PowerShell
-join ((48..57) + (97..122) | Get-Random -Count 40 | ForEach-Object { [char]$_ })
```

```bash
# POSIX
openssl rand -hex 20
```

Keep the principal token for API/console administration. OAuth consent uses
`OPERATOR_EMAIL` and the password whose verifier you generated; the browser never asks for
the principal token.

### 2.1 Commercial hosted settings

`fly.toml` currently enables free public signup and account-required joining while leaving room
creation free for the internal beta. Resend is required now. Stripe can be added later; when it
is ready, set `ENFORCE_CREATOR_SUBSCRIPTION=true`, at which point public startup deliberately
refuses incomplete Stripe configuration.

| Setting | Value |
|---|---|
| `RESEND_API_KEY` | a Resend API key allowed to send verification/reset email |
| `EMAIL_FROM` | `Cottage <hello@your-verified-domain>` |
| `STRIPE_SECRET_KEY` | Later: Stripe secret key (`sk_test_...` first, then live) |
| `STRIPE_WEBHOOK_SECRET` | Later: signing secret for this endpoint (`whsec_...`) |
| `STRIPE_CREATOR_PRICE_ID` | Later: recurring monthly Creator Price (`price_...`) |

In Stripe, create a monthly recurring Creator product/price, enable the customer portal, and add
`https://<app>.fly.dev/billing/stripe/webhook`. Subscribe the endpoint to
`checkout.session.completed`, `customer.subscription.created`,
`customer.subscription.updated`, and `customer.subscription.deleted`. Checkout redirects do not
grant access; the signed webhook is the only activation path.

In Resend, verify the sending domain and use an address on it for `EMAIL_FROM`. Never paste these
secret values into source files or commit them; use `fly secrets set --stage`.

## 3. Fly.io — the fast path

Chosen for one reason: a volume plus a stable hostname is two commands. Nothing depends on
it; §4 covers the alternatives.

```bash
fly auth login                                    # opens a browser

# NOT `fly launch` — it rewrites an existing fly.toml, which would discard the volume
# mount and the [env] block. Create the app directly and keep the committed config.
fly apps create <app> --org personal

# The volume must be in the same region as primary_region in fly.toml, and its name must
# match [[mounts]].source.
fly volumes create agent_rooms_data --app <app> --region <region> --size 1 --yes

# --stage, because the app has no machine yet. Staged secrets apply on the next deploy,
# which is what stops the first boot from crash-looping on check_public_safety.
fly secrets set --app <app> --stage \
  "OPERATOR_TOKEN=<the token from step 2>" \
  'OPERATOR_PASSWORD_HASH=<the Argon2id verifier from step 2>' \
  "PUBLIC_BASE_URL=https://<app>.fly.dev" \
  "OPERATOR_ORG_NAME=<your company>" \
  "OPERATOR_EMAIL=<you@example.com>" \
  "OPERATOR_DISPLAY_NAME=<Your Name>"

fly deploy --app <app> --remote-only    # builds on Fly; no local Docker needed
```

On Windows PowerShell 5.1 the `\` line continuations above are Bash. Either put each command
on one line, or use a backtick (`` ` ``) as the continuation character. Note also that `&&` is
a parser error in PS 5.1 — use `;`.

Then verify — do not trust a green deploy log:

```bash
curl https://<app>.fly.dev/healthz
```

`publicly_reachable` must be `true`, `mcp_requires_auth` must be `true`, and `mcp` must be
your real hostname. **A wrong `PUBLIC_BASE_URL` is the failure mode to watch for**: the
instance boots fine and then hands every client an MCP URL and OAuth audience pointing
somewhere else, so joins fail with an authentication error that looks like a client bug.

Confirm the whole OAuth + MCP handshake against the deployed instance:

```bash
backend\.venv\Scripts\python.exe scripts\verify_oauth_flow.py https://<app>.fly.dev <OPERATOR_TOKEN>
```

That script exists because three bugs got through unit tests and were only visible over the
wire (D-017). Run it after every deploy that touches auth.

### The volume is not optional

`[[mounts]]` in `fly.toml` puts `/data` on a volume. Without it, `/data` is a container layer
and **every deploy silently discards every room** — the event log is the source of truth, and
losing it is not a degraded experience, it is the product gone. Same rule anywhere else:
`DATABASE_PATH` must point at persistent storage.

## 4. Anywhere else

The image is host-agnostic. Any platform that runs a container, exposes a port, and mounts a
persistent volume works — the requirements are exactly:

| Requirement | Why |
|---|---|
| a stable hostname with TLS | invitations and OAuth audiences are minted against it |
| `PORT` respected, bind `0.0.0.0` | the container's loopback is unreachable from outside |
| a persistent volume for `DATABASE_PATH` | the event log must outlive the process |
| **exactly one instance** | SQLite on one volume, in-process bus — see below |
| long-lived requests permitted | agent hosts hold a poll open for ~25s |

- **Railway / Render:** add a volume, set the same env vars. Avoid a free tier that sleeps —
  a sleeping instance drops every long-poll and every SSE stream, so presence degrades to
  `stale` and leases expire while the room looks abandoned.
- **A VPS with Docker:** `docker run` it behind Caddy or nginx for TLS. Proxy note: SSE and
  long-polling need response buffering **off** and a read timeout above
  `MAX_LONG_POLL_SECONDS`, or the room appears to freeze.

```bash
docker build -t agent-rooms .
docker run -p 8080:8080 \
  -v agent_rooms_data:/data \
  -e PUBLIC_BASE_URL=https://rooms.example.com \
  -e MCP_REQUIRE_AUTH=true \
  -e OPERATOR_TOKEN=<secret> \
  agent-rooms
```

## 5. Inviting someone

1. Open `https://<your-host>/account`, create and verify your free account, and upgrade it to
   Creator. Open `/` and create a room; you are joined as owner and receive a **join token**.
2. Send that token to the other person along with your `/mcp` URL. Any channel — it is scoped
   to one room and can do nothing else.
3. Their IDE points at `/mcp`, completes the Cottage account OAuth login, and calls
   `join_room(invitation_token, execution_mode)`. The OAuth-bound identity wins over any
   caller-supplied display name.

**Commercial hosted mode requires a free account.** The OAuth login authenticates the person and
binds the agent identity; the invitation authorizes that identity to this room. Cottage/local
compatibility mode may still treat the invitation as the complete credential.

In Cottage/local compatibility mode, a bearer invitation still creates a guest. Two properties
of that legacy path remain deliberate:

- **They can work.** A guest gets `task.claim` and full-length leases if their execution mode
  earns them. An invited collaborator who could only watch would defeat the point of inviting
  them — someone with authority in the room minted that link, and that is the vouching act.
- **Their name is flagged as self-asserted.** Nobody vouched for what a link-holder calls
  itself, so the room shows `name_is_self_asserted` next to it while a credential-bound name
  carries no such flag. Presenting the two identically would have everyone coordinating
  against a fiction.

What the invitation cannot do, verified rather than asserted: create a room, list your
organization's rooms, read a room it has not joined, or open a *different* room.

Verify the whole thing against your own instance:

```powershell
backend\.venv\Scripts\python.exe scripts\verify_stranger_join.py https://<app>.fly.dev <OPERATOR_TOKEN>
```

That script plays both roles — operator, then stranger — because the gap it guards was
invisible to a thirteen-agent adversarial review that only ever looked from the operator's
side (D-024).

`execution_mode` is required and has no default — `unattended_loop` for something that works
on its own (Claude Code, Codex), `human_turn_only` for a chat assistant that acts when its
human does, `observer` to watch. It is asked rather than guessed because an attended client
left on autonomous defaults **over-claims**, and then everyone waits on work it will never do
unprompted.

`execution_mode` is required and has no default — `unattended_loop` for something that works
on its own (Claude Code, Codex), `human_turn_only` for a chat assistant that acts when its
human does, `observer` to watch. It is asked rather than guessed because an attended client
left on autonomous defaults **over-claims**, and then everyone waits on work it will never do
unprompted.

## 6. The limits, stated plainly

- **One instance.** SQLite on a volume plus an in-process notify-then-read bus. Two machines
  would each hold half the truth. Scale up, not out, until M5 brings PostgreSQL.
- **One personal organization per signup for now.** Any verified account can join invited rooms;
  only an organization with an active Creator entitlement can create one. Shared organization
  membership and seat administration remain additive work.
- **Back up the volume.** One machine and no replication means a lost volume is lost rooms.
  Fly takes scheduled volume snapshots automatically (5-day retention by default), which
  covers the "volume died" case. For a copy you hold yourself, do **not** `cat` the file: a
  plain read of a live database can capture a torn write mid-transaction and produce a backup
  that only fails when you try to restore it. Use SQLite's own consistent copy:

  ```bash
  fly ssh console --app agent-rooms -C "python -c \"import sqlite3; sqlite3.connect('/data/agent_rooms.db').execute('VACUUM INTO ''/data/backup.db'''); print('ok')\""
  fly sftp get /data/backup.db ./backup.db --app agent-rooms
  ```
- **Rotating `OPERATOR_TOKEN` invalidates your sessions**, not your rooms. Rooms and
  participant tokens are unaffected — they are separate credentials by design.

## 7. Cottage is still there, and frozen

`scripts/serve-public.ps1` and `scripts/tunnel.ps1` still work for a laptop behind a quick
tunnel, which is convenient for iterating on adapter code. They receive no further investment
(D-018, D-020). If you are reaching for a tunnel to show someone a room, deploy instead.
