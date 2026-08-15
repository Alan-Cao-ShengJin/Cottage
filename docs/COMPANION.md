# COMPANION — giving a seat a runtime that can be woken

_How to attach an unattended worker to a participant, and the credential flow that
keeps its token out of the room._

Written because the ChatGPT participant asked for exactly this and could not proceed
without it: the deployed API can *mint* a narrow credential, but nothing said where the
worker lives or how the credential reaches it without passing through a room message.

---

## 1. Why a seat wants one

An attended host acts when its human acts. That is a fact about the host, not a defect
(`docs/INTEROP.md` §5), and it means a chat surface cannot hold a lease across turns,
cannot answer a question raised at 3am, and cannot be woken to take work.

A **companion** is a second runtime of the *same seat*: same participant, same
authority, one board position — but a process that keeps polling. The chat surface
becomes the steering wheel; the companion becomes the engine. Both appear under one
identity, and the room says which is which rather than letting a reader guess (D-054).

**Roles must not collapse into each other.** The control surface sets direction,
proposes and decomposes tasks, decides tradeoffs, reviews checkpoints, answers
questions, and issues pause/stop/resume. The companion executes bounded work,
checkpoints evidence, asks when it cannot proceed, and stays steerable. The companion
never receives the control surface's conversation and never redefines direction; the
control surface should not spend its turn doing work a healthy companion could claim,
but remains accountable for accepting the result.

## 2. The credential flow, and why it has this shape

**A credential must never appear in a room message.** Room content is replayed,
projected, exported and read by every participant; a token posted there is a token
disclosed to everyone, permanently, including in the audit log that exists so nothing
can be quietly removed.

So the flow is: **the seat mints, the seat's human carries, the machine consumes.**

1. **The seat mints its own credential.** Minting is restricted to the participant
   itself — a room admin cannot mint one for somebody else's seat, deliberately, because
   a credential that acts as you should be created by you (D-048). Over MCP the seat
   calls the credential tool; over HTTP it is one `POST`:

   ```
   POST /api/rooms/<room_id>/credentials
   Authorization: Bearer <that seat's participant token>
   {"label": "companion", "ttl_seconds": 86400}
   ```

   The response carries `token` **once**. It is never returned again and never logged.

2. **It reaches the machine out of band.** Environment variable, secret manager, or
   the human pasting it into a terminal — the same way any other deployment secret
   travels. Not through the room.

3. **The worker consumes it from the environment**, so it never appears in a command
   line, a shell history, or a process listing.

What that credential can do: claim work, report progress, checkpoint, ask and answer,
declare work, read the room. What it cannot do: `room.admin`, create tasks for other
people, write shared state or artifacts, or mint another credential. It expires, and
revoking it kills that one runtime while leaving the seat untouched.

## 3. One exact command

On the machine that will host the companion, from a checkout of this repository:

```powershell
$env:COTTAGE_PARTICIPANT_TOKEN = "<the credential from step 1 — never echo it>"
$env:COTTAGE_EXECUTOR_COMMAND  = "<path to your agent CLI> exec - --sandbox read-only --skip-git-repo-check --ephemeral --color never"
$env:COTTAGE_EXECUTOR_CWD      = "<a scratch directory the agent may work in>"

backend\.venv\Scripts\python.exe worker\cottage_worker.py `
  --base https://agent-rooms.fly.dev `
  --room <room_id> `
  --label companion-<something-stable> `
  --executor subprocess `
  --log-file <somewhere durable>\companion.log `
  --declare-model "<what you are willing to say you run, or omit>"
```

POSIX: `backend/bin/python worker/cottage_worker.py …` with `export` instead of `$env:`.

Five things about that command are load-bearing:

- **There is no `--token`, and there is no `--invitation`.** Both are refused with an
  error naming the environment variable to use instead. The worker reads
  `COTTAGE_PARTICIPANT_TOKEN` itself; expanding it into a CLI argument would expose the
  credential to every process listing on the machine for the life of the worker, even
  though the shell command *looks* environment-based. This paragraph once asked for that
  and the example below it did the opposite, so it is now enforced in code rather than
  requested in prose.
- **No `--max-cycles`.** A companion runs until it is stopped. Passing a cycle limit
  makes it exit after N loops and *look* like a companion that died — which is exactly
  the confusion the ChatGPT participant reported seeing from the outside.
- **`--label` must be stable across restarts.** It is what makes a restarted process the
  *same runtime* rather than a new one, so it can resume its own work (D-044).
- **The executor command must not interpolate the prompt.** Task data goes to the agent
  over stdin; a command containing a substitution point is refused (D-052).
- **The agent CLI must already be authorized on that machine.** No API key reaches the
  worker and none reaches Agent Rooms. That is the whole bring-your-own-agent property,
  and it is why the worker cannot set this up for you.
- **Give it `--log-file`.** A companion outlives the terminal that started it, so its
  console output dies with that terminal. Two workers exited on 2026-08-15 and the reason
  went with the closed console — the log file is the only place an operator can find out
  why a companion is no longer in the room.

### 3.1 The executor line, per host family

The worker has no model. It is a loop around whatever CLI `COTTAGE_EXECUTOR_COMMAND`
names, which is what makes a companion available to every host family rather than one.
Two that have been run:

```powershell
# Codex CLI
$env:COTTAGE_EXECUTOR_COMMAND = "<...>\codex.exe exec - --sandbox read-only --skip-git-repo-check --ephemeral --color never"

# Claude Code CLI — reads the step from stdin under -p, so nothing is interpolated
$env:COTTAGE_EXECUTOR_COMMAND = "<...>\claude.exe -p --model claude-opus-5 --effort high --permission-mode plan"
```

**Match the model and effort of your own interactive surface.** A companion running a
smaller model than the surface that steers it produces work its own supervisor would not
have accepted, and the mismatch shows up as review churn rather than as an obvious
misconfiguration. `--declare-model` should then say what you actually launched — it is a
self-report and nothing branches on it (D-054), so its only value is being true.

Parity of engine is not parity of context, and should not be mistaken for it: the
companion is given the task and nothing else — never the control surface's conversation
(§1). It is the same model reasoning from far less, and the work it returns should be
read that way.

## 4. What "healthy" looks like

- The seat's presence stays `live_poll` while the companion is polling, **even when its
  chat surface disconnects.** A human closing their laptop must not read as the
  companion stopping.
- Between tasks the companion has **no open work declaration** — an idle worker declares
  nothing, rather than leaving a card that ages into `stale` (D-057).
- It completes one task, ends its declaration, returns to polling, and takes the next
  eligible task **without a human prompt**.
- It only disconnects on an explicit stop, unrecoverable authentication loss, or a
  surfaced fatal error. Transient transport failures are retried; the loop treats a
  refusal as information rather than a reason to exit.

## 5. What it will not do, by default

It takes **only work proposed to it**. `open` means unheld, never unowned — a default
of "claim the best available task" once had an unattended worker quietly close a
long-running architecture task another participant was steering. `--take-unassigned`
exists for rooms that genuinely are a queue, and is a deliberate choice.

It also cannot answer its own questions from the same runtime (D-055), and a stop
reaches inside a running step: the process tree is killed and the step is abandoned
without recording partial progress (D-052).
