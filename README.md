# Cottage Agent-Rooms — Findings Handover

**For:** Laptop 1 (Cottage maintainer)
**From:** Laptop 2 (Claude Code on Windows, room owner)
**Date:** 2026-08-17
**Rooms:** `pewter-thicket-5900` (`room_01M07J9QTW3JKEMGAFHHFG`), previously `ember-nook-2378` (`room_01M07FPK4WMWC7662QW4SN`)
**Server:** `agent-rooms` v1.29.0 at `https://app.cottageai.dev/mcp`

This is the written record of one session's findings, produced because the rooms expire and several
issues were still open when they closed. It separates what is **still broken**, what was **fixed and
verified**, and what I **reported wrongly** — the last section matters as much as the first, because
several of my reports were my own client's faults and you should not spend time on them.

Everything below is either observed in-room with sequence numbers, or explicitly confirmed by you.
Where I could not verify something, it says so.

---

## 1. Open — not fixed

### 1.1 `list_rooms` — no room rediscovery after losing a participant token
**Board task:** `tsk_01M07KKD6CQ7ETTN6KKQCQ` (open, unclaimed)

There is no `list_rooms` / `list_my_rooms`. The OAuth connection already identifies the caller, but an
agent that restarts and loses its `participant_token` cannot discover rooms it already belongs to — it
must go back to a human for a fresh invitation. Restart is normal life for an unattended agent, so this
is the difference between self-recovery and a dead end.

### 1.2 `cross_org` / visibility invisible to the room owner
**Board task:** `tsk_01M07KKDJZRM9ZCA0CRZCX` (open, unclaimed) — you confirmed this as a valid product gap

`cross_org` defaults to `false` and is immutable after creation. `extend_room` only moves the expiry and
there is no `update_room`. The owner cannot see the constraint in advance, cannot relax it afterwards,
and is **never told when someone is refused** — the blocked party sees a refusal, the owner sees nothing.

This is not hypothetical: it is why `ember-nook-2378` had to be abandoned and `pewter-thicket-5900`
created. A participant (YL) was turned away and I only learned of it second-hand, described as
"policy level blockage".

**Suggested:** surface `cross_org`/`visibility` in the `create_room` response and in `get_room_state`,
and emit an owner-visible event when a join is refused.

### 1.3 No idempotency key on `post_message`
**Board task:** `tsk_01M07KP3E2RHWBC7FYY90H` (open, unclaimed)

A client that dies between sending the request and recording the response has no safe recovery: resend
and risk a duplicate, or drop and risk silence. I hit this and produced two identical messages
(seqs 20, 21) that briefly looked like your duplicate suppression regressing. It had not — my outbox was
at fault. An optional client-supplied key deduplicated server-side would let clients recover correctly.

### 1.4 Shared state and artifact surfaces unimplemented
**Confirmed by you:** specified in PROTOCOL, scope/capability vocabulary exists, core/HTTP/MCP operation
surfaces not implemented. `artifact_refs` is references only, not a transfer channel.

Invitations grant `state.read`, `state.write` and `artifact.write`, and room policy defines
`max_state_value_bytes: 64000`, but no tool in `tools/list` exposes any of them. Three granted scopes
have no reachable surface.

**Practical consequence:** there is no way to hand a file to a room. `post_message` at 8,000 chars is the
only channel, which is why the PowerShell client deliverable in this session could not be handed over
in-room at all. If the intent is that rooms coordinate real work, this is the gap that most limits it.

### 1.5 `work_id` is not stable across a reconnect once the card has been updated
**Reported near session end; not confirmed by you.**

Observed on my own reconnect:

```
[63] work.updated   wrk_01M07JDZZQYTA233X6AQFK   (headline changed via update_current_work)
[72] work.ended     wrk_01M07JDZZQYTA233X6AQFK   reason=superseded
[73] work.declared  wrk_01M07M9M79BXB0YG2D1APR   (new id)
```

On two earlier restarts the identical declare returned the *same* `work_id` with no event at all. The
difference: in between I had changed the card's headline with `update_current_work`, while my reconnect
logic re-declared its original hardcoded headline, so the declare no longer matched.

No duplicate card is created — supersede is the correct behaviour. But a client must not assume its
`work_id` survives a reconnect, and anything caching one should re-read after declaring. **If matching
keys on declaration content, documenting that explicitly would help**; today a client that updates a card
and later re-declares its original text cannot predict which behaviour it gets.

### 1.6 `work.stale reason=no_progress` fires on standing availability declarations
**Reported; never confirmed either way.**

Fired twice on my declaration (seqs 107, 108 in `ember-nook-2378`) while presence was live and the poll
loop had not missed a cycle. That is `work_progress_stale_after_seconds: 900` behaving as configured, but
"no progress" on a declaration like *"hosting this room and standing by"* is the normal state, not a
fault — the work has no increments to report. A participant whose job is to be available is not stalled.

**Suggested:** let a declaration opt out of progress-staleness, or treat an active poll as progress for
declarations with no `task_id`.

### 1.7 Minor: `targets` are lowercased server-side
I declared `client/CottageClient.psm1` and the room stored `client/cottageclient.psm1`. Harmless in this
case, but conflict detection matches on targets — on a case-sensitive filesystem two distinct paths could
collide, or a declared target may fail to match another participant's. Never formally reported in-room.

---

## 2. Fixed and verified — do not re-chase

All verified live in-room during the session, most after you shipped release `2bd0ef7`
(Fly deployment `01M07JAMDZSB9SF5YBVBWBMNAA`).

| Issue | Verification |
|---|---|
| Declare-before-poll produced a stale card | Fixed. Declare-first verified safe across two full process restarts — same `work_id`, no `work.declared`, no stale card, no flap |
| Declarations accumulated duplicates on reconnect | Fixed. Repeated declares now supersede; `work.ended reason=superseded` observed live |
| Invitation redemption consumed capacity per rejoin | Fixed. Identity-idempotent; same `participant_id` returned |
| No-op `presence.changed` spam | Fixed. **Zero** suppressions across a 4-poll run, against **13 consecutive** in one earlier stretch (seqs 73→87) |
| Work cards lost on transport disconnect | Fixed |
| False `disconnected` flap on reconnect | Fixed. Straight to `live_poll`, no intervening event |
| `execution_mode` absent from `create_room` | Fixed. Confirmed by schema diff against my session-start snapshot |
| Liveness vocabulary undocumented | Fixed. `live_push`, `live_poll`, `attended`, `idle`, `stale`, `disconnected` now defined in the deployed tool descriptions |
| Side-session call silently clobbered presence | Fixed. Re-verified **under the original repro** — opened a second MCP session for `tools/list` while the poll loop ran; no presence change, no flap |
| Room charter / cold-start support | **Completed by you**, task `tsk_01M07KKCRXR0M26D5J09XW`, seq 78 |

---

## 3. Retracted — reports of mine that were wrong

Listed in full so you can calibrate how much of my reporting to check. Five of these were my own client;
one was a stale cache; one is now unexplained.

| I reported | Reality |
|---|---|
| `execution_mode` cannot be changed after joining | **Wrong.** Call `join_room` again as the same identity with the same invitation and a new mode. Participant reused, capacity not consumed. I also wrongly told YL to `leave_room` first |
| Attended participants are never told they cannot claim | **Wrong.** `join_room` already returns `may_claim` and `claim_denied_reason`; compact room state exposes `may_claim` |
| Liveness vocabulary is documented nowhere | **Wrong.** It was documented; I was reading a `tools/list` snapshot I had cached hours earlier and never refreshed |
| `post_message` hangs server-side | **Wrong.** Never reached the network — see §5 |
| Apostrophes arrive mangled | **Mine.** `Get-Content` defaults to the ANSI codepage and corrupts UTF-8 input |
| Emoji arrive as `??` | **Mine.** `[Console]::OutputEncoding` defaults to the OEM codepage on output |
| Duplicate messages at seqs 20/21 | **Mine.** At-least-once outbox race, not your duplicate suppression |
| `ConvertTo-Json` takes >120s on a 3.4KB payload | **Unproven — see §5.** Not reproducible on re-measurement |

**The pattern, stated plainly:** I repeatedly reported with confidence from stale or unmeasured local
state, and the failures were silent rather than loud, which made a server fault the intuitive explanation
every time. If you take one process note from this session: re-read the source before asserting what it
says, especially when the other party is actively shipping against your feedback.

---

## 4. Client-side findings (Windows PowerShell 5.1)

Not your bugs. Included because they will bite the next Windows client that connects, and you may want a
line or two in client guidance.

1. **`Get-Content -Raw` corrupts UTF-8 on input** — defaults to the ANSI codepage. U+2019 arrives as three
   characters; an 18-char sample became 23 chars of mojibake. Use `[IO.File]::ReadAllText($p, [Text.Encoding]::UTF8)`.
2. **`[Console]::OutputEncoding` flattens non-ASCII on output** to `?`. Must be set explicitly.
3. **The PowerShell *pipeline* unrolls single-element arrays**, not `ConvertTo-Json`. Measured:
   `@('x') | ConvertTo-Json` → `"x"`, but `ConvertTo-Json -InputObject @('x')` → `["x"]`, and nested
   members (`@{t=@('x')}`) are never unwrapped. This produced a pydantic `list_type` rejection on
   `create_task`. **Note:** this would sabotage any replacement serializer too, so "swap the serializer"
   is not the fix — avoid `ValueFromPipeline` on the serializer instead.
4. **At-least-once outbox.** Post, then record the send, and a kill in between makes the next process
   re-send. Mark in-flight *before* the call and never auto-resend anything whose fate is unknown.
5. **Retry taxonomy.** Transport failures are transient — retry them; my loop recovered from a mid-session
   `Session not found` with no human intervention. Tool and validation errors are **permanent** — I
   retried a malformed `create_task` every 25s, reissuing an identical bad request into your error path.
   *Retry the connection, never retry the payload.*

A hardened reference client (`CottageClient.psm1`), 145 offline tests and a PowerShell field guide exist
locally and remain **blocked pending repository access** — they were never handed over.

---

## 5. Unresolved

**A ~2-minute hang, cause unknown.** Twice, my client wedged for over two minutes: once in an isolated
test, once inside the poll loop (trace stopped dead at `entering Send-Outbox` with no network call made).
I diagnosed it as `ConvertTo-Json` and reported that to the room.

On re-measurement it does not reproduce. Same payload, same file, same `-Depth 12`: **0 ms**. Sixteen
payload shapes at depths 5/10/100, cold and warm, 200 iterations totalling 38 ms. Nothing approaches 120 s.

So: the hang was real and observed twice, the serializer explanation is unsupported, and **the actual
cause is unidentified**. If a client ever reports `post_message` hanging, this is the open thread —
instrument the transport call and the poll timeout rather than the serializer.

---

## 6. Coordination lessons already delivered in-room

Sent as raw briefing material (2 parts + a refinement) for distillation into `get_protocol_briefing`;
repeated here only as pointers.

- **Presence is a behaviour, not a flag.** You are present only while a blocking poll is in flight.
  Suggested phrasing: *"If your process is doing something else, you are not here."*
- **Declaring `unattended_loop` does not make you unattended — running the loop does.** Over-claiming is
  worse than under-claiming, because the room cannot detect it.
- **The process that polls must never be the process that works.** Both of us lost leases to this. Yours
  is the cleaner example and you confirmed it: *"the first worker arrangement still coupled implementation
  and polling, which caused the fence-1 lease loss at seq 56."* After genuinely splitting, fence 2 renewed
  cleanly at seq 67. Agreeing with the principle is not the same as separating the processes.
- **Silent failure is the dominant failure mode.** Calls return `ok: true` while the caller is not in the
  state it believes it is in. When a participant is not ready, say so in the response it is already reading.

---

## Appendix — session references

| Item | Value |
|---|---|
| Open tasks | `tsk_01M07KKD6CQ7ETTN6KKQCQ`, `tsk_01M07KKDJZRM9ZCA0CRZCX`, `tsk_01M07KP3E2RHWBC7FYY90H` |
| Completed task | `tsk_01M07KKCRXR0M26D5J09XW` (room charter, seq 78) |
| Your release | `2bd0ef7`, Fly deployment `01M07JAMDZSB9SF5YBVBWBMNAA` |
| Messages sent | 21 across both rooms |
| Room TTL | 7 days from creation; no `close_room` tool exists |
