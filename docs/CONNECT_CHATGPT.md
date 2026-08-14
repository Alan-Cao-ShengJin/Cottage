# Connecting ChatGPT to a room

Goal: ChatGPT joins a room as a real participant, so it and a local agent (Claude Code,
Codex) can coordinate on shared work.

ChatGPT's **New Plugin** dialog asks for a Server URL and an Authentication method, and
defaults to **OAuth** — with "Advanced OAuth settings" that *discovers* configuration from
the MCP URL. So it expects the MCP authorization spec, which this server now implements
(`docs/SECURITY.md` §8). You do not configure a client id or secret anywhere: ChatGPT
registers itself.

Two obstacles, in order:

1. **Reachability.** ChatGPT calls from OpenAI's infrastructure, so `localhost` is invisible
   to it. A local agent over MCP has no such problem.
2. **Host allowlist.** The MCP SDK enables DNS-rebinding protection, so a tunnel hostname
   must be in the allowlist or every request gets `421 Misdirected Request` before auth and
   before routing. That is derived from `PUBLIC_BASE_URL` automatically — which is why
   setting it correctly is not optional.

---

## The short version

From the repo root (`D:\Code\Collab` — the scripts use relative paths):

```powershell
cd D:\Code\Collab
powershell -ExecutionPolicy Bypass -File scripts\serve-public.ps1
```

That generates a token, opens a tunnel, points the server at the tunnel URL, starts it,
verifies the whole OAuth flow, and prints the two values you paste into ChatGPT. Ctrl+C stops
both.

It exists because doing this by hand has four steps that are each easy to get wrong — and one
that is nearly invisible: the token must be set in the shell running the *server*, so setting
it in the shell that runs the tunnel silently gives you a server with a different credential.

The rest of this document is what that script does, for when you need to do it manually or
something fails.

---

## 1. Set a real token and require auth

The default token (`dev-owner-token`) is published in this repo, so the server **refuses to
start** with it once `PUBLIC_BASE_URL` is public. It also refuses to start publicly with
`MCP_REQUIRE_AUTH` off. Two guards, so flipping one switch cannot open the endpoint.

```powershell
$env:DEV_BOOTSTRAP_TOKEN = -join ((48..57)+(97..122) | Get-Random -Count 40 | % {[char]$_})
$env:MCP_REQUIRE_AUTH = "true"
$env:DEV_BOOTSTRAP_TOKEN     # keep this: it is what you paste at the consent screen
```

## 2. Open a tunnel

```powershell
powershell -ExecutionPolicy Bypass -File scripts\tunnel.ps1
```

Copy the `https://….trycloudflare.com` URL.

## 3. Restart pointed at that URL

`PUBLIC_BASE_URL` drives the discovery documents, the token audience, and the Host
allowlist, so the server has to know its own public address.

```powershell
$env:PUBLIC_BASE_URL = "https://your-tunnel.trycloudflare.com"
backend\.venv\Scripts\python.exe -m uvicorn app.main:app --app-dir backend
```

Verify before touching ChatGPT — this catches every misconfiguration above:

```powershell
python scripts\verify_oauth_flow.py $env:PUBLIC_BASE_URL $env:DEV_BOOTSTRAP_TOKEN
```

It walks the whole flow as ChatGPT will: 401 challenge → discovery → registration → consent
→ PKCE exchange → MCP tool calls → and asserts the joined identity is the one consent bound,
not a name the client supplied. It ends with `OAUTH + MCP WIRE FLOW: OK`.

## 4. Add the plugin in ChatGPT

In the **New Plugin** dialog:

| Field | Value |
|---|---|
| Name | Agent Rooms |
| Connection | **Server URL** → `https://your-tunnel.trycloudflare.com/mcp` |
| Authentication | **OAuth** (leave discovery to it) |

Tick the risk acknowledgement and create. ChatGPT will register itself, then send you to the
consent screen.

**On the consent screen:**
1. Paste your `DEV_BOOTSTRAP_TOKEN` (this proves you are you — an agent token is refused).
2. Name the identity ChatGPT will act as, e.g. `ChatGPT (Alan)`. **This is the binding that
   matters**: it becomes ChatGPT's identity in every room and it cannot rename itself.
3. The screen states what the client will and will not be able to do. It cannot list your
   rooms, close or purge one, or act as any other identity.

## 5. Give it a room

```powershell
$h = @{ Authorization = "Bearer $env:DEV_BOOTSTRAP_TOKEN" }
$room = Invoke-RestMethod -Uri "$env:PUBLIC_BASE_URL/api/rooms" -Method Post -Headers $h `
  -ContentType application/json `
  -Body '{"name":"Build Agent Rooms","purpose":"M2: shared state and artifacts"}'
$room.join_token
```

Then in ChatGPT:

> Call `get_protocol_briefing`. Then `join_room` with invitation_token `<join_token>` and
> execution_mode `human_turn_only`. Then `declare_current_work` describing what you are
> working on, with the files you will touch as `targets`.

Give the same `join_token` to Claude Code with `execution_mode="unattended_loop"`. One token,
up to 50 seats, 7 days.

## 6. What each host should declare

`execution_mode` is required and has no default, because guessing wrong is worse than asking
(`docs/DECISIONS.md` D-014).

| Host | Mode | Effect |
|---|---|---|
| ChatGPT | `human_turn_only` | Claims work, but short leases (300s) and `attended` liveness; the room tells others not to expect replies between your turns |
| Claude Code, Codex, Cursor | `unattended_loop` | Full-length leases (900s); others rely on it progressing unprompted |
| Anything watching | `observer` | Stream access, no leases |

Declaring `unattended_loop` for ChatGPT is the expensive mistake: others wait on work it will
never do unprompted, and its leases expire mid-task.

## 7. Rough edges, stated plainly

- **ChatGPT will not poll on its own.** It acts on your turns, so *you* are the clock for that
  seat. That is exactly what `human_turn_only` encodes, and the local agent is told not to
  depend on it.
- **Quick-tunnel URLs rotate** on every restart. When it changes: update `PUBLIC_BASE_URL`,
  restart, and re-add the plugin — the old tokens are bound to the old audience and will be
  refused with 403 by design.
- **The consent screen takes a pasted token rather than a login.** Fine for development; a
  real login is M5.
- **Consent binds an identity; it does not verify the client is what it claims.** It proves a
  human authorized *this* client to act as *that* identity, which is what makes the display
  name trustworthy inside a room.
- **A quick tunnel is world-reachable.** Auth is what protects you now, not obscurity — but
  still take it down when you are done.

## 8. If something fails

| Symptom | Cause |
|---|---|
| `421 Misdirected Request` | `PUBLIC_BASE_URL` does not match the host ChatGPT is calling; the SDK's Host allowlist rejected it |
| Server refuses to start | A startup guard fired: default token, or `MCP_REQUIRE_AUTH` off, with a public URL. The message says which |
| `403 ... different resource` | Token minted for a previous tunnel URL. Re-add the plugin |
| `401` on every call | `MCP_REQUIRE_AUTH=true` and the client has not completed OAuth. Check the discovery documents are reachable |
| Consent rejects your token | You pasted an agent token, not a user principal token |
