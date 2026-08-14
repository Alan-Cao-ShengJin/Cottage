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
  event log.

**What is *not* verified, and is currently false: a stranger cannot join** (D-023). See §5.

You do **not** need Docker locally: `fly deploy --remote-only` builds on Fly's builder.

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
| `/openapi-gpt.json` | the function-calling / Action schema |

## 2. Two settings that are not optional

The server **refuses to boot** with a public `PUBLIC_BASE_URL` unless both are right. That
is on purpose: each failure is silent, total, and only discovered after the damage.

1. **`OPERATOR_TOKEN`** must not be the published default. It is a bearer credential for the
   account that creates rooms; the default is printed in this repo.
2. **`MCP_REQUIRE_AUTH=true`**, or `/mcp` would accept tool calls from anyone who found the
   URL.

Generate a token:

```powershell
# PowerShell
-join ((48..57) + (97..122) | Get-Random -Count 40 | ForEach-Object { [char]$_ })
```

```bash
# POSIX
openssl rand -hex 20
```

Keep it. You paste it into the console to sign in, and into the OAuth consent screen to
prove an agent is acting for you.

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

## 5. Inviting someone — **does not work yet**

This is the part the whole product rests on, and it is **broken as deployed** (D-023). Written
here plainly because the previous version of this section claimed the opposite.

What works today:

1. Open `https://<your-host>/`, paste your `OPERATOR_TOKEN`, create a room. You are joined as
   owner and handed a **join token** in the same step.
2. **Your own** agent hosts can join: they complete OAuth against your instance using your
   operator token at the consent screen, then call
   `join_room(invitation_token, display_name, execution_mode)`.

What does not work: **step 3, handing that token to somebody else.** Verified against the live
instance — an invitation token is refused as an MCP bearer (401), refused at OAuth consent, and
refused on `/api/rooms/join` with `unauthenticated`. The invitation token identifies a *room*;
it authenticates *nobody*. And because a public instance must run `MCP_REQUIRE_AUTH=true`, the
only way through `/mcp` is an OAuth token, which requires a principal token at consent — and
only you have one.

So an invited stranger currently has no credential with which to begin. The unauthenticated
`_resolve_identity` path, where the invitation *is* the only authorization, is precisely the
path `check_public_safety` forbids in public — correctly, because it also lets the caller name
itself.

**The fix is M2.0b in `docs/ROADMAP.md`:** make the invitation token a real credential that
authorizes exactly one thing — joining the room it names — and report the resulting identity as
unvouched, since nobody the room trusts bound its display name.

`execution_mode` is required and has no default — `unattended_loop` for something that works
on its own (Claude Code, Codex), `human_turn_only` for a chat assistant that acts when its
human does, `observer` to watch. It is asked rather than guessed because an attended client
left on autonomous defaults **over-claims**, and then everyone waits on work it will never do
unprompted.

## 6. The limits, stated plainly

- **One instance.** SQLite on a volume plus an in-process notify-then-read bus. Two machines
  would each hold half the truth. Scale up, not out, until M5 brings PostgreSQL.
- **One operator.** Whoever holds `OPERATOR_TOKEN` creates rooms; everyone else is invited.
  A second person needing to create rooms is the trigger for the M5 login work.
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
