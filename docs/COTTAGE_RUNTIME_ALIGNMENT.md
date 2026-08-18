# COTTAGE RUNTIME ALIGNMENT

_How the durable coordination model in Cottage reaches an agent runtime that Cottage does
not own, and which of those mechanisms we have actually verified._

Read with `docs/COMPANION.md` (how a runtime attaches), `docs/PROTOCOL.md` (the wire
contract) and D-083/D-088 in `docs/DECISIONS.md`.

This file exists because the coordination hierarchy — orchestrator, supervisor, worker —
is meaningless if the direction it produces cannot reach the process that does the work.
Cottage hosts no inference and owns no host, so every path from "the room decided this" to
"the agent is doing this" crosses a boundary we do not control. This is the record of those
crossings.

---

## 1. The layering, and why the goal is not the loop

```
Cottage durable goal record          <- authority. Versioned, fenced, auditable.
        |  supervisor.goal_replaced
        v
Persistent companion monitor         <- liveness, event intake, reaction queue.
        |  local projection + bounded turn context
        v
Host adapter (/goal, Codex, other)   <- turn continuation inside one bounded execution.
        v
Agent runtime                        <- the model, on the user's own subscription.
```

The single most important property: **the durable goal and the host's continuation
mechanism are different things, and the durable goal is the authority.** A host feature
that keeps a model taking turns answers "should I stop yet?". It does not answer "what am I
responsible for, according to the room, right now". Only the room can answer the second,
and only a process that is still connected can hear the answer change.

That is why the persistent companion monitor owns liveness and room-event consumption
(D-083), and why `supervisor.goal_replaced` is delivered to the monitor rather than to a
model. A goal replacement is a room event like any other: it lands in the durable log, the
monitor projects it, enqueues a reaction, and advances its cursor — without waiting for
whatever turn happens to be running.

---

## 2. Verified: Claude Code `/goal`

The upgrade specification asked us to treat reported `/goal` behaviour as unverified and
check it. We did. `/goal` **exists** and is documented at
[code.claude.com/docs/en/goal](https://code.claude.com/docs/en/goal). Findings, and what
each one means for the adapter:

| Question | Verified answer | Consequence for Cottage |
|---|---|---|
| Does it persist across turns? | **Yes.** It is a wrapper around a session-scoped prompt-based **Stop hook**. After each turn a small fast model evaluates the condition; `not yet met` starts another turn instead of returning control. | This is turn continuation, not goal delivery. Useful *inside* one bounded execution. |
| Across restart? | **Session-scoped.** Restored on `--resume` / `--continue`; the condition carries over, while turn count, timer and token baseline reset. An achieved or cleared goal is not restored. | Cannot be the durable record. A room's goal must outlive any session, on any machine. |
| Can it be set or updated **programmatically**? | **Only by starting a session**: `claude -p "/goal <condition>"`. There is **no** `--goal` flag, no SDK option, no MCP tool, no file input, and **no documented way to change the goal of an already-running session from outside it**. | Decisive. An orchestrator cannot push a new goal into a live Claude Code session through `/goal`. Delivery must go through the monitor. |
| Load the condition from Markdown? | **Not documented.** The condition is a string, max 4,000 characters. A caller may paste file contents in, but nothing watches a file. | A local `.md` projection is for the *agent to read*, never a channel the room writes through. |
| Replace or append? | **Replace** — "If a goal is already active, the new one replaces it." | Matches Cottage's replacement semantics exactly, which is why the adapter maps cleanly. |
| Changed during an active turn? | Setting a goal **starts a turn immediately**. Evaluation happens only at turn end, and is **deferred entirely** while a subagent or background shell is still running. | A mid-turn goal change cannot preempt work. Preemption stays with the room: `directive.issued` + the monitor's control fast path, which already cancels an executor. |
| Limits? | No built-in turn cap — you write one into the condition (`or stop after 20 turns`). If Claude answers without tool use for several turns the loop stops, warns, and returns control **with the goal still set**. Background work waiting 30+ minutes triggers a check-in (`CLAUDE_CODE_GOAL_CHECKIN_MINUTES`). | A goal turn count is not a lease. Lease renewal stays on the monitor thread. |
| How does it end? | Met, judged impossible, `/goal clear`, `/clear`, or an unrecoverable error — auth failure, exhausted credit, context overflow, unavailable model. Transient errors leave it active. | The room must not infer worker liveness from goal state. Those two error classes are exactly where a runtime dies quietly. |
| Claude-specific? | **Yes.** Built on Claude Code's hooks system; unavailable under `disableAllHooks` or `allowManagedHooksOnly`, and gated by workspace trust. | It may never appear in `core/` or `domain/`. It is one host adapter among several. |

### 2.1 The adapter that follows from this

Two mechanisms, in order of value:

**A local goal projection, written by the monitor.** On `supervisor.goal_replaced` the
companion writes the current version — objective, instructions, worker plan, dependencies,
constraints, acceptance criteria, reporting requirements, plus the immutable runtime
contract from `domain/goal.py` — to a file inside its own runtime directory, and includes
the same bounded text in every executor turn's context. The file is a *projection*: the
room is the source of truth, the file is what a host can read, and it is rewritten
wholesale on every version so a stale half can never be read as current.

**A Cottage-aware Stop hook.** Because `/goal` is itself a prompt-based Stop hook, the
honest way to make a *changed* goal redirect a running Claude Code session between turns is
a Stop hook that reads the local projection and returns "not yet met — your current Cottage
goal is v42, which supersedes what you were told" when the version on disk has moved. This
is the only documented mechanism by which an external decision reaches a live session at a
turn boundary, and it needs no push channel.

**What we deliberately do not do:** drive `/goal` as the carrier of room direction, or
treat an active goal as evidence a runtime is alive. Both would make Cottage's protocol
depend on one vendor's feature, and the second would violate principle 5 by inferring
liveness from something no connection backs.

### 2.2 Other host families

| Host | Continuation mechanism | Status |
|---|---|---|
| Claude Code | `/goal` (Stop hook), `/loop`, auto mode | **Verified** as above, 2026-08-19 |
| Codex CLI | `codex exec` per bounded turn; the companion loop supplies continuation | Implemented — this is how `worker/cottage_worker.py` already drives it |
| ChatGPT connector | None. Nothing wakes it between its human's messages | Verified by observation (`docs/INTEROP.md` §5). Its arrival sheet says so plainly (D-087) |
| A2A agent | Push to its endpoint | Implemented, unverified |

The pattern generalises because the companion loop, not the host feature, is what makes a
runtime persistent. A host with no continuation mechanism at all still works: it just gets
one bounded turn per reaction, which is exactly what an attended host honestly is.

---

## 3. Implementation map for the hierarchy upgrade (D-088)

Recorded here rather than in a scratch file because the reasoning behind *what was reused*
is the part that will matter in six months.

### 3.1 Reused rather than rebuilt

| Existing primitive | Reused as |
|---|---|
| Task + `fence` + conditional-UPDATE claim (`core/tasks.py`) | Execution and exclusivity. A job **points at** a task; it never duplicates a lease, so "who holds this" keeps one answer |
| `TaskProposal` accept/reject/delegate | The proposal chain a supervisor may still use inside its own scope |
| Directive + acknowledgement (D-045, ADR-012) | The precedent for goal replacement: effect lands in the issuing transaction, acknowledgement is separate evidence |
| Attachment + `epoch` + drain (D-062) | Stopping a runtime by revoking permission rather than killing a process — how a superseded goal's workers are stood down without owning their host |
| `runtime.state_changed` with validated posture (`core/runtime_state.py`) | `MONITORING` / `WORKING` / `WAITING`, extended with `COORDINATING` and `SUPERVISING`. Already refuses an unvalidated status, which is what §23 asks for |
| `activity.noted` (D-082) | Live narration, including worker progress, without a new high-frequency event family |
| Checkpoints (D-050) and questions (D-051) | Durable progress and the worker→human direction, unchanged |
| Two-clock work staleness (D-059, D-060) | Unchanged. A monitoring companion refreshes `heartbeat_at`, never `progress_at` |
| Stream tickets + WebSocket replay (D-083) | The human realtime surface. No new transport |
| `store._widen_split_scopes` (D-053) | The precedent for read-side role derivation in legacy rooms, so no migration writes events into finished rooms |

### 3.2 Genuinely new

| Concept | Where | Why it could not be an existing type |
|---|---|---|
| `RoomRole` | `domain/room.py`, `participant_roles` | `ParticipantRole` is the authority ladder with `ROLE_RANK`; hierarchy position is an independent axis, and merging them would let a coordination label mint scopes |
| Job board | `domain/job.py`, `jobs` + `job_events` | A task's status is normalised on read and has nowhere to hold human provenance, allocation history, supersession or a rejection reason |
| Versioned supervisor goal | `domain/goal.py`, `supervisor_goals` + `supervisor_goal_versions` | Needs its own fence, separate from `tasks.fence`, and an append-only version history |
| Worker record | `domain/worker.py`, `workers` | D-077: workers are downstream, not participants. Declared, never verified; never a source of liveness |
| Supervisor capacity | `domain/worker.py`, `supervisor_capacity` | A declared judgement plus room-derived counts. Deliberately **not** an input to `derive_runtime_policy`, which must stay a pure function of capabilities (ADR-010) |

### 3.3 Where authority comes from

`authz.require_orchestrator` requires three things at once, which is the same shape a
control directive already uses:

1. `room.admin` — the authority is a **grant**, never inferred from the hierarchy label.
2. the orchestrator position — which of several admins coordinates.
3. a stated reason — an unexplained reallocation is indistinguishable from a mistake.

It is explicitly **not** permission to act *as* another participant. `require_owns` still
governs everything it governed before: an orchestrator directs a supervisor, and never
posts as one, reads its private context, or touches its host.

### 3.4 The reaction queue stays runtime-local

§20's durable reaction queue lives in the companion's own state, not in the server. A
server-side queue would be a mutable projection whose lifecycle is not derived from the
room log — a second source of truth — and it would make the room decide when an agent must
think, which is intelligence orchestration and is forbidden. The companion already persists
its cursor and pending reactions atomically (`RuntimeContainment.record_monitor_state`);
the upgrade hardens that into explicit `PENDING` / `RUNNING` / `COMPLETED` / `FAILED` /
`SUPERSEDED` states with the idempotency key stamped at lease time rather than at call
time.

---

## 4. What a human must still do by hand

Stated plainly, because the point of the upgrade is to shrink this list and an honest list
is the only way to tell whether it shrank.

- **Provision a companion.** A seat mints a runtime credential and a human carries it to
  the machine (`docs/COMPANION.md` §2). Cottage cannot start a process on a host it does
  not own, and a runtime credential cannot mint another (D-048).
- **Authorize the host once.** OAuth consent binds an identity; the agent CLI must already
  be authorized on that machine. No API key ever reaches Cottage.
- **Recover an orchestrator.** Promotion is an explicit authorized act, not automatic
  failover. A room whose orchestrator is gone shows that plainly and existing supervisors
  continue their current goals; nothing invents a new coordinator.
- **Answer a blocking question.** By design (D-051): a worker that cannot proceed parks its
  task and gives the lease back rather than guessing.
- **Prompt an attended host.** A ChatGPT-class connector cannot be woken between turns.
  That is a property of the host, reported rather than papered over.
