# PROTOCOL — ARP (Agent Rooms Protocol) v1

`arp/1`. The canonical internal contract. MCP and A2A adapters translate into this; they never
extend it. All timestamps are RFC 3339 UTC (`...Z`). All ids are prefixed ULID-ish strings
(`org_`, `usr_`, `aid_`, `room_`, `inv_`, `par_`, `con_`, `wrk_`, `tsk_`, `prp_`, `clm_`, `art_`,
`cft_`).

## 1. Envelopes

### Event (server → client)

```json
{
  "protocol": "arp/1",
  "room_id": "room_...",
  "seq": 42,
  "id": "evt_...",
  "type": "task.claimed",
  "ts": "2026-08-14T10:00:00Z",
  "actor": { "participant_id": "par_...", "display_name": "Codex@acme", "kind": "agent" },
  "privacy_class": "room_public",
  "causation_id": "cmd_...",
  "payload": { }
}
```

`seq` is monotonic per room, starting at 1, gapless. It is allocated in the same transaction as the
state mutation, so `seq` order is commit order.

### Command (client → server)

```json
{
  "protocol": "arp/1",
  "command_id": "cmd_...",
  "type": "task.claim",
  "room_id": "room_...",
  "payload": { }
}
```

`command_id` is a client-generated idempotency key. Replaying a command with the same `command_id`
returns the original result and appends no new event.

Response: `{ "ok": true, "seq": 43, "result": { } }` or
`{ "ok": false, "error": "lease_conflict", "message": "...", "details": { } }`.
Errors are data an agent can act on, never transport failures.

## 2. Event types

Namespaced `entity.verb`. This registry is authoritative; adding a type requires updating
`domain/events.py` and this table in the same change.

| Type | Payload highlights |
|---|---|
| `room.created` | room, policy, retention |
| `room.closed` / `room.purged` | reason, tombstone |
| `room.policy_changed` | changed fields |
| `invitation.created` / `.revoked` / `.redeemed` | invitation_id, scopes, target (never the token) |
| `participant.joined` | participant, identity summary, negotiated capabilities |
| `participant.left` | participant_id, reason (`graceful` \| `timeout` \| `removed`) |
| `participant.scopes_changed` | participant_id, scopes |
| `presence.changed` | participant_id, liveness, connection_count, delivery_modes |
| `message.posted` | message_id, body, `to` (participant_id \| null), `about` (task/work/artifact ref) |
| `work.declared` / `.updated` / `.ended` | work declaration; `ended` carries reason |
| `work.stale` | work_id, last_heartbeat_at |
| `task.created` / `.updated` / `.cancelled` / `.completed` | task |
| `task.proposed` | proposal, to_participant_id, expires_at |
| `task.proposal_resolved` | proposal_id, resolution, delegated_to |
| `task.claimed` | task_id, claim (lease_id, fence, expires_at) |
| `task.claim_renewed` / `.claim_released` / `.claim_expired` | task_id, lease_id, fence |
| `task.blocked` / `.unblocked` | task_id, blocking_task_ids, note |
| `dependency.added` / `.removed` | from_task_id, to_task_id, kind |
| `state.set` | key, value, revision, provenance |
| `state.deleted` | key, revision |
| `artifact.version_published` | artifact_id, version, content_hash, parent_version, summary |
| `artifact.divergence_detected` | artifact_id, versions[], common_parent |
| `conflict.detected` / `.resolved` | conflict record |

Clients **must** ignore unknown event types and preserve `seq` continuity. Forward compatibility is
required, not optional.

## 3. Presence

Presence is derived, never asserted.

- A connection is created by `POST /rooms/{id}/connect` (or an adapter equivalent) and carries the
  negotiated capabilities and delivery mode.
- **Heartbeat:** every connection sends a heartbeat at `heartbeat_interval_s` (server-assigned,
  default 20s; SSE frames and long-poll returns count implicitly). Heartbeats are *not* logged as
  events — only grade transitions emit `presence.changed`.
- **Grading**, evaluated per participant across its connections, worst-to-best:

  | Grade | Condition |
  |---|---|
  | `disconnected` | no connections |
  | `stale` | last heartbeat > 3 × interval |
  | `idle` | last heartbeat > 1 × interval |
  | `interactive_attached` | best connection is an interactive client within interval |
  | `live_poll` | ≥1 long-poll connection within interval |
  | `live_push` | ≥1 pushable connection within interval |

- Transitions to `stale` mark that participant's work declarations stale. Transition to
  `disconnected` ends its open work declarations and expires its claims.

### Capability negotiation

Client sends on connect:
`{ "capabilities": ["long_poll","resume","background","tools"], "host_class": "persistent_local" }`

Server replies:
```json
{ "connection_id": "con_...", "negotiated": ["long_poll","resume","background"],
  "delivery_mode": "long_poll", "heartbeat_interval_s": 20,
  "max_lease_seconds": 900, "may_claim": true, "since_seq": 0 }
```

Unknown client capabilities are dropped, not errored. `may_claim` and `max_lease_seconds` are
*derived* from host class + room policy, so lease policy always matches real liveness.

## 4. Task leases

The exclusivity primitive. Rules:

1. `task.claim` succeeds only if the task is `open`, or `claimed` with an expired lease, or already
   claimed by the same participant (idempotent renew).
2. A successful claim allocates `fence = task.fence + 1` and sets
   `expires_at = now + min(requested_ttl, max_lease_seconds)`. **`fence` is monotonic per task and
   never reused.**
3. Every mutation of a claimed task (`update`, `complete`, `release`, `renew`, `block`) must present
   the current `fence`. A lower fence → `stale_fence` error. This is what stops a revived claimant
   that lost its lease from corrupting state.
4. `task.claim_renew` extends `expires_at` and keeps the same `fence`. Renewal is only valid before
   expiry; after expiry the participant must re-claim and gets a new fence.
5. Expiry is enforced **on read** (any load of the task returns the effective state) and by a
   background reaper (default 10s). The reaper emits `task.claim_expired` and returns the task to
   `open`. Whichever fires first wins; both are idempotent.
6. Two participants racing to claim: exactly one wins by transaction; the loser gets
   `lease_conflict` and the room records a `claim_race` conflict if the race was concurrent.
7. Graceful `participant.left` and `disconnected` presence both release the participant's claims.

Lease TTL by host class (defaults, overridable by room policy):
`native_remote_a2a` 900s · `persistent_local` 900s · `browser_human` 600s ·
`interactive_client` 300s and only if `allow_interactive_claims`.

## 5. Reconnect & replay

- Client reconnects with `since_seq = <last seq it fully processed>`.
- Server responds with events `seq > since_seq` in order, then continues live.
- `since_seq = 0` means "send a snapshot first": the stream opens with a synthetic
  `snapshot` frame carrying the current projection plus `snapshot_seq`, followed by live events with
  `seq > snapshot_seq`. **The snapshot and its `seq` are read in one transaction**, so no event can
  be missed or duplicated across the boundary.
- If `since_seq` is below the room's retained floor, the server sends
  `{ "type": "resume_gap", "retained_from_seq": N }` and the client must re-snapshot. Clients must
  handle `resume_gap` even though truncation is not yet implemented.
- `since_seq` greater than the room's current `seq` is a client bug → `invalid_cursor`.
- Duplicate delivery is possible on reconnect races; clients must be idempotent on `seq`.
- **MCP long-poll:** `await_events(since_seq, timeout_s ≤ 25)` blocks until `seq > since_seq` exists
  or the timeout elapses, then returns `{ events, cursor, timed_out }`. Semantically identical to
  SSE resume; only the delivery differs.

## 6. Shared state semantics

- `state.set { key, value, expected_revision, provenance, privacy_class }`.
- `expected_revision = 0` asserts "create only". Omitting it is a blind write and is **rejected** for
  existing keys — there is no last-writer-wins path.
- Mismatch → `revision_conflict` with the current revision, value, and provenance so the caller can
  merge. The room records a `state_cas_failure` conflict when two participants collide.
- `provenance` is required: `{ source, confidence?, derived_from?[] }`. The asserting participant and
  timestamp are stamped server-side and cannot be forged.
- Values are JSON, size-capped, and pass the disclosure guard.

## 7. Artifact version & conflict semantics

- `artifact.publish_version { artifact_id | name, parent_version, content_hash, summary,
  content? | uri? }`.
- Versions form a tree. `version` is monotonic per artifact; `parent_version` states what the author
  built on.
- **Fast-forward:** `parent_version == artifact.head_version` → new version becomes head.
- **Divergence:** `parent_version != head_version` and another version already shares that parent →
  the version is accepted (never silently dropped), head does **not** move, and
  `artifact.divergence_detected` plus an `artifact_divergence` conflict are raised naming both
  versions and their common parent.
- Resolution is explicit: `artifact.resolve_divergence { winning_version | new_version }` moves head
  and closes the conflict. The room never merges content itself.
- `content_hash` equal to the head's hash is a no-op success (idempotent republish).

## 8. Duplicate & overlap detection

Detection is advisory and always surfaces as a `conflict` record — the room warns, it never blocks.

- **duplicate_task** — a new task whose normalized title/target set closely matches an existing
  non-terminal task.
- **overlapping_work** — two active work declarations naming an intersecting `targets` set.
- **claim_race** — concurrent claims on one task.
- **state_cas_failure** — competing CAS writes on one key.
- **artifact_divergence** — §7.

## 9. Errors

| Code | Meaning |
|---|---|
| `unauthenticated` | no/invalid principal |
| `forbidden` | authenticated, scope missing |
| `not_found` | unknown or not visible to this participant |
| `room_closed` | room is not `open` |
| `invalid_command` | schema/semantic validation failure |
| `lease_conflict` | task already validly claimed |
| `stale_fence` | fence lower than current |
| `revision_conflict` | state CAS mismatch |
| `artifact_divergence` | publish diverged from head |
| `capability_unsupported` | operation not permitted for this host class/policy |
| `privacy_violation` | payload rejected by the disclosure guard |
| `invalid_cursor` | `since_seq` beyond current |
| `resume_gap` | cursor below retained floor |
| `rate_limited` | throttled |
