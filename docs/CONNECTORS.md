# CONNECTORS — keeping one working while the server changes

_Operator guidance. Read this when a connector that used to work has stopped, or when
new tools appear in the room and one participant cannot see them._

**Provenance:** sections 1–4 were drafted by the Codex-backed companion worker
(`codex-cli/gpt-5.6-sol`) as task `tsk_01M02S4RYDM77PH4F33XW4` in room
`room_01M022GNSYC29CSPWDDYBC`, across four checkpoints at seq 211–225, and edited
lightly for house style. This document is the first piece of the product written
*through* the product. §5 is ours.

---

## 1. What goes stale

A connector may cache the server's tool list and tool schemas; when those change, new
tools remain invisible or calls use outdated arguments. Its OAuth client registration
can also expire or become invalid, causing authorization or client-registration
errors. A stale resource/audience binding typically produces token-audience or
access-denied failures, while an expired or revoked participant credential makes the
user appear unauthenticated or no longer joined.

**These failures look similar and are easy to misdiagnose as each other.** Refreshing
tool discovery will not repair credentials or OAuth bindings, and re-authorising will
not reveal a tool the client has never discovered.

## 2. How a human refreshes one

Removing and re-adding the connector is the reliable general way to repeat setup and
re-read the tool list. *Reconnecting* usually restores a dropped session using
existing configuration; *re-authorising* usually replaces or renews credentials.
Neither should be assumed to refresh tool discovery unless the host explicitly
documents that it does.

Vendor menus differ and change, so this deliberately describes the three *actions*
rather than any product's wording.

## 3. What the server owes the connector

Unknown tools must return a clear error, and removed tool names must never be reused
for unrelated behaviour. Arguments the server no longer accepts must be **rejected
rather than ignored**.

Silent tolerance is worse than a hard error because it makes a stale connector appear
successful while doing nothing — or doing the wrong thing — leaving operators without
a reliable diagnosis. This is the same rule `CommandMeta` already enforces with
`extra="forbid"`: an ignored field is a leak, a rejected command is a bad request
(D-024, D-026, D-027, D-030).

## 4. The versioning rule

Evolve schemas **additively**: keep existing arguments and meanings stable, and
introduce new behaviour through optional parameters with safe defaults. The cost,
stated plainly, is carrying legacy fields and compatibility logic longer than you
would like.

When a genuine break is unavoidable, publish a new tool name or an explicit major
version, keep the old tool available through a documented migration window, and
retire it only after connectors have had the chance to refresh.

This matches D-041: evolve through forward-compatible parameter shapes, not merely
existing ones.

## 5. What this room has actually seen go stale

Two, both observed rather than anticipated, and neither is a tool-schema problem —
which is itself the point of §1's warning that these failures wear each other's
clothes.

**A participant token that a rejoin rotated away** (D-056). `participants.token_hash`
is a single column, so redeeming an invitation for a seat that already exists
overwrites it. A control surface that reconnects therefore invalidates the credential
it — or a sibling process — was already holding, and the refusal it gets back says
*"Unknown or revoked token"*, which reads as a security event rather than a
lifecycle one. The ChatGPT participant hit this and correctly escalated it.

**A scope that a split removed from everyone already in a room** (D-053). Narrowing a
scope is a data migration, not a code change: anything computed as an *intersection*
with stored authority silently shrinks for every participant who stored theirs before
the change. The symptom was a worker that could claim work and then not report on it.

The lesson for this document: **a connector's most likely failure is not a stale tool
list.** It is a credential that stopped working for a reason nobody wrote down.
