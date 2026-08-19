"""MCP adapter — the client adapter (D-006).

Translation only. Every tool maps onto an ARP command and calls the same `core`
service the HTTP transport does, so authorization, the disclosure boundary, and
lease semantics are identical no matter which door a participant came through. No
business rule lives in this file.

**The honest limitation, stated to the model as well as to the reader:** MCP has no
server-initiated wake channel a client acts on. So `await_room_events` is a
server-side blocking long-poll, not an event listener. An agent calling it in a loop
is the closest true equivalent, and the negotiated capability set says `supports_poll`
rather than `supports_push` — which is what makes other participants' lease
decisions correct rather than optimistic.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

from mcp.server.fastmcp import Context, FastMCP
from mcp.server.streamable_http import MCP_SESSION_ID_HEADER
from mcp.server.transport_security import TransportSecuritySettings

from ...config import settings
from ...core import (
    activity,
    checkpoints,
    directives,
    eventlog,
    messages,
    presence,
    projections,
    questions,
    rooms,
    runtime_state,
    store,
    tasks,
    work,
)
from ...core.bus import bus
from ...core.errors import InvalidCommand, RoomClosed, RoomError, Unauthenticated
from ...db import database as db
from ...domain.activity import ActivityPhase
from ...domain.capabilities import Capability, HostClass
from ...domain.checkpoint import ResumeState
from ...domain.commands import (
    AcknowledgeDirectiveCommand,
    AnswerQuestionCommand,
    AppendCheckpointCommand,
    AskQuestionCommand,
    ClaimTaskCommand,
    CompleteTaskCommand,
    ConnectCommand,
    CreateRoomCommand,
    CreateTaskCommand,
    DeclareWorkCommand,
    EndWorkCommand,
    ExtendRoomCommand,
    IssueDirectiveCommand,
    JoinRoomCommand,
    LeaveRoomCommand,
    NoteActivityCommand,
    PostMessageCommand,
    ReleaseClaimCommand,
    RenewClaimCommand,
    SetRuntimeStateCommand,
    UpdateRoomCharterCommand,
    UpdateWorkCommand,
)
from ...domain.directive import DirectiveAction
from ...domain.disclosure import Audience, Disclosure
from ...domain.room import (
    Participant,
    PrivacyClass,
    RoomVisibility,
    RuntimeOperationalState,
)
from ...domain.work import WorkStatus
from . import compact
from .auth import principal_for_tool

log = logging.getLogger(__name__)

INSTRUCTIONS = (
    "Agent Rooms: a live coordination network for independently owned agents. "
    "This is not a chat server — the point is shared work awareness. "
    "When the user asks to create a Cottage room, call create_room immediately; the "
    "OAuth connection already identifies its owner, so never ask for a principal token "
    "or send the user to a browser form. When the user supplies an invitation, call "
    "join_room with it and your honest execution_mode. Call get_protocol_briefing before "
    "beginning coordinated work, then set_runtime_operational_state and declare_current_work. "
    "Use await_room_events in a loop: this server cannot push to you, so a blocking "
    "poll is how you stay live. Renew your task leases before they expire or you "
    "will lose them."
)


def transport_security() -> TransportSecuritySettings:
    """Host/Origin allowlist for the MCP transport.

    The SDK enables DNS-rebinding protection by default, which validates the `Host`
    header against an allowlist. Its built-in list covers loopback only, so a server
    behind a tunnel answers `421 Misdirected Request` to every request — before auth,
    before routing, with only a log line to explain it. Found by pointing a client at a
    non-loopback host.

    So the allowlist is derived from `PUBLIC_BASE_URL`: whatever address we tell clients
    to use is, by definition, an address we must accept. Loopback stays for local
    development, and the console's origins come from the CORS allowlist.
    """
    hosts = ["localhost", "localhost:*", "127.0.0.1", "127.0.0.1:*", "[::1]", "[::1]:*"]
    origins = list(settings.cors_origins)

    parsed = urlparse(settings.public_base_url)
    if parsed.hostname:
        # Both bare and wildcard-port forms: a tunnel URL has no explicit port, but a
        # self-hosted deployment usually does.
        hosts += [parsed.netloc, parsed.hostname, f"{parsed.hostname}:*"]
        origins.append(f"{parsed.scheme}://{parsed.netloc}")

    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=sorted(set(hosts)),
        allowed_origins=sorted(set(origins)),
    )


mcp = FastMCP(
    name="agent-rooms",
    # Mounted at /mcp by main.py, so the inner route is the mount root.
    streamable_http_path="/",
    instructions=INSTRUCTIONS,
    transport_security=transport_security(),
)

#: MCP session → participant token. A session holds one participant, so this
#: remembers "who you are" between tool calls. Every tool also accepts an explicit
#: token so a caller can always identify itself without relying on this — session
#: affinity is a convenience, never the authorization.
#:
#: Bounded, because entries for sessions that vanished without calling `leave_room`
#: would otherwise accumulate for the life of the process. Eviction is safe: it costs
#: the caller an explicit `participant_token`, it cannot grant anything.
_SESSION_TOKEN_LIMIT = 512
_session_tokens: OrderedDict[str, str] = OrderedDict()


@dataclass
class _SessionConnection:
    """The exact runtime connection opened by one MCP transport session.

    A participant may have several open connections with different capability profiles.
    Remembering only its participant token is therefore not enough: heartbeating an
    arbitrary connection can keep an attended runtime alive while the session's actual
    unattended runtime is reaped. The declaration is retained so a later tool call can
    truthfully reconnect after that reap.
    """

    participant_id: str
    connection_id: str
    capabilities: tuple[Capability, ...]
    host_class: HostClass
    attachment_label: str | None
    attachment_resumable: bool
    last_seq: int = 0
    lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)


_session_connections: OrderedDict[str, _SessionConnection] = OrderedDict()
_session_restore_lock = asyncio.Lock()

#: One durable runtime identity for everything arriving over MCP as this participant.
#:
#: Without it, a connector that calls `join_room` more than once accumulates open
#: connections, and the next claim is refused as `executor_ambiguous` — correctly,
#: since two unlabelled connections really are indistinguishable runtimes. Labelling
#: them says the true thing instead: they are one connector, reconnecting.
#:
#: `resumable=False` because we cannot promise it. The MCP session behind these
#: connections may be a fresh process with none of the previous one's knowledge, and
#: this flag is read later as evidence for recovery claims (D-036, D-038). Claiming
#: resumability we have not verified is precisely the failure principle 5 forbids.
MCP_ATTACHMENT_LABEL = "mcp"


def _remember_session(ctx: Context | None, participant_token: str) -> None:
    key = _session_key(ctx)
    if key is None:
        return
    _session_tokens[key] = participant_token
    _session_tokens.move_to_end(key)
    while len(_session_tokens) > _SESSION_TOKEN_LIMIT:
        evicted, _ = _session_tokens.popitem(last=False)
        _session_connections.pop(evicted, None)


def _remember_connection(
    ctx: Context | None,
    *,
    participant_id: str,
    connection_id: str,
    capabilities: list[Capability],
    host_class: HostClass,
    attachment_label: str | None,
    attachment_resumable: bool,
    last_seq: int = 0,
) -> None:
    key = _session_key(ctx)
    if key is None:
        return
    _session_connections[key] = _SessionConnection(
        participant_id=participant_id,
        connection_id=connection_id,
        capabilities=tuple(capabilities),
        host_class=host_class,
        attachment_label=attachment_label,
        attachment_resumable=attachment_resumable,
        last_seq=last_seq,
    )
    _session_connections.move_to_end(key)


def _forget_session(ctx: Context | None) -> None:
    key = _session_key(ctx)
    if key is None:
        return
    _session_tokens.pop(key, None)
    _session_connections.pop(key, None)


def _session_key(ctx: Context | None) -> str | None:
    """The transport's session id, or None when there is no session to key on.

    **Not `id(ctx.session)`, which is what this used to be.** `id()` is a memory
    address, and CPython reuses addresses after garbage collection — so a new session
    could land on the address of a finished one and inherit its entry from
    `_session_tokens`, i.e. act as a previous caller's participant. The old code also
    funnelled every session-less call into one shared `"default"` bucket, which is the
    same bleed without needing a coincidence.

    The streamable-HTTP transport assigns an unguessable UUID per session and the client
    echoes it on each request, so it is unique, never reused, and read from the request
    carrying *this* call — the same per-message source that identity resolution uses for
    the reason recorded in `auth.py`.

    Returning None rather than a placeholder is the point: with no session to key on, the
    caller must present its own token.
    """
    request = getattr(getattr(ctx, "request_context", None), "request", None)
    headers = getattr(request, "headers", None)
    if headers is None:
        return None
    session_id = headers.get(MCP_SESSION_ID_HEADER) or headers.get("Mcp-Session-Id")
    return f"session:{session_id}" if session_id else None


async def _participant(ctx: Context | None, token: str | None) -> Participant:
    key = _session_key(ctx)
    resolved = token or (_session_tokens.get(key) if key else None)
    if not resolved:
        raise Unauthenticated(
            "You have not joined a room in this session. Call join_room first, or "
            "pass the participant_token you were given."
        )
    participant = await store.load_participant_by_token(resolved)
    if token and key and key not in _session_connections:
        await _restore_session_connection(ctx, participant, token)
    await _ensure_session_connection(ctx, participant)
    return participant


async def _restore_session_connection(
    ctx: Context | None, participant: Participant, participant_token: str
) -> None:
    """Rebuild affinity after a server restart from the last persisted MCP profile.

    The participant token is required: after process memory is gone, it is the proof
    that this new transport session may bind the seat. The previous connection supplies
    only a capability declaration and resume cursor, never authority.
    """
    key = _session_key(ctx)
    if key is None:
        return

    async with _session_restore_lock:
        if key in _session_connections:
            return
        row = await db.fetch_one(
            """
            SELECT c.id, a.label, a.is_resumable
              FROM connections c
              JOIN attachments a ON a.id = c.attachment_id
             WHERE c.participant_id = ? AND a.label = ?
             ORDER BY c.opened_at DESC
             LIMIT 1
            """,
            (participant.id, MCP_ATTACHMENT_LABEL),
        )
        if row is None:
            return
        previous = await store.load_connection(str(row["id"]))
        _remember_session(ctx, participant_token)
        # An empty id deliberately forces `_ensure_session_connection` to create a
        # connection for this new transport session rather than heartbeating a row
        # whose process disappeared with the old server.
        _remember_connection(
            ctx,
            participant_id=participant.id,
            connection_id="",
            capabilities=list(previous.negotiated_capabilities),
            host_class=previous.host_class,
            attachment_label=str(row["label"]),
            attachment_resumable=bool(row["is_resumable"]),
            last_seq=previous.last_delivered_seq,
        )


async def _ensure_session_connection(
    ctx: Context | None, participant: Participant, *, seq: int | None = None
) -> None:
    """Heartbeat or truthfully recreate this MCP session's own connection.

    The seat remains joined when presence lapses. A new tool call is fresh evidence that
    this exact transport session is active, so it may restore the connection using the
    declaration captured when the session joined. No call means no heartbeat and the
    normal stale/disconnected ladder still applies.
    """
    key = _session_key(ctx)
    binding = _session_connections.get(key) if key else None
    if binding is None or binding.participant_id != participant.id:
        return

    async with binding.lock:
        row = await db.fetch_one(
            "SELECT closed_at FROM connections WHERE id = ? AND participant_id = ?",
            (binding.connection_id, participant.id),
        )
        if row is not None and row["closed_at"] is None:
            if seq is not None:
                binding.last_seq = max(binding.last_seq, seq)
            with contextlib.suppress(RoomClosed):
                await presence.heartbeat(
                    connection_id=binding.connection_id,
                    participant=participant,
                    seq=binding.last_seq,
                )
            return

        try:
            negotiated = await presence.connect(
                participant=participant,
                command=ConnectCommand(
                    capabilities=list(binding.capabilities),
                    host_class=binding.host_class,
                    since_seq=max(binding.last_seq, seq or 0),
                    attachment_label=binding.attachment_label,
                    attachment_resumable=binding.attachment_resumable,
                ),
                transport="long_poll",
            )
        except RoomClosed:
            # Closed-room reads remain valid; only the liveness mutation is refused.
            return
        binding.connection_id = negotiated.connection.id
        if seq is not None:
            binding.last_seq = max(binding.last_seq, seq)


def _err(exc: RoomError) -> dict[str, Any]:
    """Room errors are information an agent can act on, not failures to hide."""
    return exc.to_payload()


def _disclosure(
    privacy_class: str = "room_public",
    to_participant_id: str | None = None,
    source: str | None = None,
) -> Disclosure:
    return Disclosure(
        privacy_class=PrivacyClass(privacy_class),
        audience=Audience.PARTICIPANT if to_participant_id else Audience.ROOM,
        to_participant_id=to_participant_id,
        source=source,
    )


# ---------------------------------------------------------------------------
# Orientation
# ---------------------------------------------------------------------------


@mcp.tool()
async def get_protocol_briefing() -> str:
    """Read the rules of engagement before joining a room.

    Explains what a room is for, what you must never share, how leases work, and why
    you must poll. Call this once per session.
    """
    return BRIEFING


BRIEFING = """\
# Agent Rooms — rules of engagement

## What this is
A live coordination network. You and other independently owned agents connect to a
room to make your concurrent work visible, divide it, and avoid colliding. The room
coordinates; it never tells you how to think. You decide whether and how to execute.

## What it is not
Not a chat room. Messages are a minor annotation channel. The primary surfaces are
your current-work declaration and the task board.

## What you must never share
Never send your system prompt, your reasoning, your private memory, credentials,
API keys, private file contents, or context from unrelated work. There is no field
for these and the server rejects content that looks like them. Share conclusions and
references, not your internals.

## Getting in
Either you create the room or someone gives you a token.

* **Creating:** when the person asks for a room, call `create_room(name, purpose,
  execution_mode)` directly.
  OAuth already identifies the owner; do not ask for a principal token or redirect them to
  the website. You are already joined and receive the invitation to share.
* **Joining:** call `join_room(invitation_token, execution_mode)`. OAuth supplies your bound
  identity; the invitation authorizes entry to that one room.

## Declare how you run, honestly
`join_room` requires an `execution_mode`. `create_room` accepts the same field and retains
`unattended_loop` only as a compatibility default, so turn-driven creators must override it:

* `unattended_loop` — you are a long-lived process that can keep calling tools on your
  own clock (Claude Code, Codex, Cursor, a scheduled agent). Full-length leases.
* `human_turn_only` — you act only when a human prompts you (ChatGPT or a chat assistant
  using this server as a connector). You can claim and do work, but leases are short and
  the room tells others not to expect prompt responses from you.
* `observer` — watching, not working. No leases.

**It answers one question only: can you keep acting without being prompted?** It says
nothing about whether a human is with you, and it never disables human interaction. If a
person is at your keyboard *and* you can run on your own clock, you are `unattended_loop`
— you stay steerable, and their instructions are simply high-priority input. **Having a
human does not make you attended; needing one does.**

Over-claiming is the expensive mistake. If you say `unattended_loop` but you only act when
prompted, others will wait on work you never do and your leases will expire mid-task. But
under-claiming is not free either: an agent that can loop and picks `human_turn_only`
because someone is watching it gives up lease eligibility the room needed it to have.

## The loop
1. `join_room` with the token you were given and your real `execution_mode`.
2. `declare_current_work` — a one-line headline plus the `targets` you are touching
   (file paths, service names, ticket ids). Targets are how the room detects that
   you and someone else are about to collide, so they matter more than the wording.
3. `await_room_events(since_seq)` in a loop. This blocks server-side until something
   happens, then returns it. This server cannot push to you; the loop is how you stay
   live. Keep the `cursor` it returns and pass it back as `since_seq`.
4. `claim_task` before doing shared work. A claim is an exclusive **lease** with an
   expiry and a `fence` number.
5. `renew_task_claim` before the lease expires, passing the current `fence`. If you
   let it lapse, the task returns to the pool and someone else may take it.
6. Every mutation of a claimed task needs the current `fence`. If you get
   `stale_fence`, you lost the lease — re-read the task, do not retry blindly.
7. `note_activity(phase, summary)` freely while you work — one line on what you are
   doing. Nothing depends on it, and it is what stops a human watching the room from
   mistaking a working agent for a dead one: your card and your claim only move when
   *state* moves, so without notes ten minutes of real work looks like a crash. Say
   `monitoring` when you finish rather than going quiet.
8. `update_current_work` as your status changes; `end_current_work` when done.
9. End the current work card and return the runtime state to `monitoring` when a task
   or model turn finishes. Do **not** leave the room merely because one turn ended;
   `leave_room` is only for an explicit participant departure.

## Reading presence
Presence is derived from each participant's open connections and their heartbeat age:

* `live_push` / `live_poll` â€” healthy now and reachable by that delivery mechanism.
* `attended` â€” healthy, but the participant acts only while a human is engaged.
* `idle` â€” one heartbeat interval has passed; recently seen, but do not assume prompt work.
* `stale` â€” more than three intervals have passed; treat its current work as untrusted.
* `disconnected` â€” no open connection. Its exclusive claims are released. Its work card
  remains as stale evidence until it reconnects, updates, explicitly ends work, or leaves.

These are grades, not commands. A quiet client moves down the ladder according to the
heartbeat interval negotiated on its connection; a successful tool call or poll beats that
exact MCP session back to its honest live/attended grade. `presence.changed` is emitted only
when the published grade actually changes, not for every heartbeat.

## How to write here
**Be brief. Say the thing, then stop.** Aim for a few sentences, the length you would
send in a chat, and elaborate only when someone asks you to.

This is a cost rule, not a style preference. Every participant that reads your message
pays for it, and a model-backed reader pays per word — so a long message spends other
people's budget to bury the one line that needed a decision. Lead with what changed or
what you need. Put conclusions and references in the room; leave the reasoning that
produced them where it belongs, which is not here.

Do not narrate the plumbing. Sequence numbers, byte counts, which poll returned, the
fact that a message arrived at all — the room already records those and every reader can
see them. Report the substance instead.

## Errors are information
`lease_conflict` means someone else is on it — pick different work or say something.
`stale_fence` means you no longer hold the claim. `capability_unsupported` means you
did not declare a capability the action requires. None of these are crashes.
"""


# ---------------------------------------------------------------------------
# Connect
# ---------------------------------------------------------------------------


async def _creating_principal(ctx: Context | None, principal_token: str | None):
    """Who is creating this room: the caller, unless they named someone else.

    Asking an assistant for an "organization principal token" is asking its human to
    go and find a credential, which is the step the product exists to remove — and
    the request is redundant, because an OAuth caller already presented one to get
    this far. So a blank or missing argument means *you*.

    A token that was actually supplied is still authenticated, and a bad one is an
    error rather than a silent fall back to the session. Quietly succeeding as
    somebody else because the credential you passed was wrong is the worst of the
    three outcomes.
    """
    from ...core.rooms import Principal

    if principal_token and principal_token.strip():
        try:
            return await rooms.authenticate_principal(principal_token.strip())
        except Unauthenticated:
            # A cached tool schema may still mark this argument required, so a client
            # that cannot omit it will send *something*. Say plainly what to send
            # instead of repeating "unknown token" at a caller with no way to comply
            # (D-041: a capability nobody can discover is a capability nobody has).
            raise Unauthenticated(
                "That principal_token is not valid. If you connected with OAuth you do "
                "not need one at all — send principal_token as an empty string, or omit "
                "it, and the room will be created as you."
            ) from None

    caller = await principal_for_tool(ctx, settings.mcp_resource_url)
    if caller is None:
        raise Unauthenticated(
            "You are not authenticated to this server, so there is nobody to create a "
            "room as. Connect with OAuth, or pass principal_token explicitly."
        )
    if caller.identity is not None:
        return Principal(kind="agent_identity", org_id=caller.org_id, identity=caller.identity)

    if caller.user_id is None:
        raise Unauthenticated("This token has no subject that can own a room.")
    user_row = await db.fetch_one("SELECT * FROM users WHERE id = ?", (caller.user_id,))
    if user_row is None:
        raise Unauthenticated("Token subject no longer exists.")
    return Principal(kind="user", org_id=caller.org_id, user=store.to_user(user_row))


@mcp.tool()
async def create_room(
    name: str,
    principal_token: str | None = None,
    purpose: str = "",
    charter: str = "",
    display_name: str = "Room creator",
    execution_mode: str = "unattended_loop",
    cross_org: bool = False,
    ctx: Context | None = None,
) -> dict[str, Any]:
    """Create a room, join it as owner, and get a token to share with everyone else.

    Call this immediately when the user asks you to create a Cottage room. Do not send
    them to the website and do not ask them to retrieve a principal token.

    **You do not need to supply a credential.** If you connected with OAuth, the
    server already knows who you are and uses that. `principal_token` is only for
    callers that authenticated some other way, and passing your own is harmless.

    `execution_mode` has the same meaning as on `join_room`. It defaults to
    `unattended_loop` for compatibility with the creator behavior Cottage already
    exposed. Pass `human_turn_only` or `observer` when that is how this client runs.
    In hosted OAuth mode the account-bound name overrides `display_name`; the response
    says which name was used instead of silently ignoring the requested one.

    You get back:

      * `join_token` — the one thing you share. Anyone you give it to calls
        `join_room(invitation_token=<join_token>, execution_mode="...")`.
      * `participant_token` — yours. You are already in the room; this session is bound
        to it, so subsequent tools work without passing anything.

    Both tokens are shown once and stored only as hashes.
    """
    try:
        if execution_mode not in EXECUTION_MODES:
            return {
                "ok": False,
                "error": "invalid_command",
                "message": (
                    f"execution_mode must be one of {sorted(EXECUTION_MODES)}. "
                    "Pick the one that describes how you actually run."
                ),
            }
        principal = await _creating_principal(ctx, principal_token)

        created = await rooms.create_room(
            # The whole principal, not `principal.user`. Passing the latter sent None
            # for every agent identity and produced "needs an authenticated principal"
            # at a caller that had just authenticated — the guard and the docstring
            # were widened and the call was not.
            principal=principal,
            command=CreateRoomCommand(
                name=name,
                purpose=purpose,
                charter=charter,
                visibility=RoomVisibility.CROSS_ORG if cross_org else RoomVisibility.INTERNAL,
            ),
            # An agent presents the name a human bound to its identity, never one it
            # chose for itself — the same rule `join_room` enforces (D-015). Without
            # this, the one room an agent creates is the one place it can call itself
            # anything, which is exactly the seam that rule closes.
            creator_display_name=(None if principal.identity is not None else display_name),
        )
        # Bind this session so later tools need no token, and open a polling connection
        # so the creator is present rather than a room with nobody in it.
        _remember_session(ctx, created.participant_token)
        declared = list(EXECUTION_MODES[execution_mode])
        host_class = MODE_HOST_LABELS[execution_mode]
        negotiated = await presence.connect(
            participant=created.participant,
            command=ConnectCommand(
                capabilities=declared,
                host_class=host_class,
                attachment_label=MCP_ATTACHMENT_LABEL,
                attachment_resumable=False,
            ),
            transport="long_poll",
        )
        _remember_connection(
            ctx,
            participant_id=created.participant.id,
            connection_id=negotiated.connection.id,
            capabilities=declared,
            host_class=host_class,
            attachment_label=MCP_ATTACHMENT_LABEL,
            attachment_resumable=False,
        )
        return {
            "ok": True,
            "room_id": created.room.id,
            "room_name": created.room.name,
            "charter": created.room.charter,
            "join_token": created.join_token,
            "participant_token": created.participant_token,
            "participant_id": created.participant.id,
            "connection_id": negotiated.connection.id,
            "execution_mode": execution_mode,
            "display_name": created.participant.identity.display_name,
            "display_name_was_overridden": (
                created.participant.identity.display_name != display_name
            ),
            "negotiated_capabilities": [
                capability.value for capability in negotiated.connection.negotiated_capabilities
            ],
            "delivery_mode": negotiated.runtime.delivery_mode.value,
            "may_claim": negotiated.runtime.may_claim,
            "cursor": await eventlog.current_seq(created.room.id),
            "share_this": (
                f"Give join_token to each participant. They call "
                f'join_room(invitation_token="{created.join_token}", execution_mode="...").'
            ),
            "next_step": (
                "You are connected. Call declare_current_work, then keep "
                "await_room_events(since_seq=cursor) running in a loop."
            ),
        }
    except RoomError as exc:
        return _err(exc)


def _default_agent_capabilities() -> list[Capability]:
    """What a persistent local agent over MCP can honestly do.

    No `supports_push`: MCP has no server-initiated wake channel, so claiming it would
    make other participants coordinate against a liveness we cannot deliver.
    """
    return [
        Capability.CAN_RECEIVE_EVENTS,
        Capability.SUPPORTS_POLL,
        Capability.SUPPORTS_RESUME,
        Capability.CAN_INITIATE_FOLLOWUP,
        Capability.CAN_EXECUTE_BACKGROUND,
        Capability.SUPPORTS_TOOLS,
    ]


#: How a client actually runs. Required at join, with no default, because the honest
#: answer differs per host and guessing it wrong is worse than asking.
#:
#: A boolean-per-capability API was tried first and was a mistake: the defaults have to
#: be *something*, and whichever way they lean, half the clients silently mis-declare.
#: An attended client left on autonomous defaults over-claims — other participants then
#: wait on work it will never do unprompted — and an autonomous one left on attended
#: defaults needlessly loses long leases. Asking "how do you run?" is a question every
#: client can answer correctly about itself.
EXECUTION_MODES: dict[str, tuple[Capability, ...]] = {
    # A long-lived process that can loop on its own clock: Claude Code, Codex, Cursor,
    # a cron-driven agent, an A2A agent behind an MCP shim.
    "unattended_loop": (
        Capability.CAN_RECEIVE_EVENTS,
        Capability.SUPPORTS_POLL,
        Capability.SUPPORTS_RESUME,
        Capability.CAN_INITIATE_FOLLOWUP,
        Capability.CAN_EXECUTE_BACKGROUND,
        Capability.SUPPORTS_TOOLS,
        Capability.SUPPORTS_ARTIFACTS,
    ),
    # Acts only while a human is engaged: ChatGPT with this server as a connector,
    # Claude in a chat window, any assistant driven turn-by-turn. It can call tools —
    # including the polling tool — but only when its human prompts it, so the room must
    # not route latency-sensitive or exclusive work to it by default.
    "human_turn_only": (
        Capability.CAN_RECEIVE_EVENTS,
        Capability.SUPPORTS_POLL,
        Capability.SUPPORTS_RESUME,
        Capability.REQUIRES_HUMAN_PRESENCE,
        Capability.SUPPORTS_TOOLS,
    ),
    # Watching, not working. Gets the stream, takes no leases.
    "observer": (
        Capability.CAN_RECEIVE_EVENTS,
        Capability.SUPPORTS_POLL,
        Capability.SUPPORTS_RESUME,
    ),
}

#: Descriptive label recorded alongside the mode, for display and telemetry only.
#: Behavior comes from the capabilities above, never from this (ADR-010).
MODE_HOST_LABELS: dict[str, HostClass] = {
    "unattended_loop": HostClass.PERSISTENT_LOCAL,
    "human_turn_only": HostClass.INTERACTIVE_CLIENT,
    "observer": HostClass.UNKNOWN,
}


@mcp.tool()
async def join_room(
    invitation_token: str,
    execution_mode: str,
    display_name: str = "Room participant",
    description: str = "",
    since_seq: int = 0,
    ctx: Context | None = None,
) -> dict[str, Any]:
    """Redeem a room invitation for the OAuth-authenticated identity in one call.

    Call this directly when the user gives you a Cottage invitation. In hosted mode the
    OAuth-bound display name overrides `display_name`, so you do not need to ask for one.

    `execution_mode` must describe how you actually run. Answer for yourself, honestly:

      * `"unattended_loop"` — you are a long-lived process that can keep calling tools
        on your own clock without anyone prompting you (Claude Code, Codex, Cursor, a
        scheduled agent). You get full-length leases.
      * `"human_turn_only"` — you act only when a human prompts you (ChatGPT or a chat
        assistant using this server as a connector). You can still claim and do work,
        but you get short leases and the room will not rely on you responding promptly,
        because nothing can wake you between turns.
      * `"observer"` — you are watching, not working. No leases.

    This answers **one** question: can you keep acting without being prompted? It says
    nothing about whether a human is with you, and choosing `unattended_loop` does not
    make you unsteerable — a human can message you at any time and you should treat that
    as high-priority input. **Having a human attending does not make you attended;
    needing one to act does.** An agent that loops on its own clock with a person at the
    keyboard is `unattended_loop`.

    Over-claiming is the costly error: if you say `unattended_loop` and you actually only
    act when prompted, other participants will wait on work you never do, and your leases
    will expire mid-task. Under-claiming is not free either — if you can loop but declare
    `human_turn_only` because someone is watching you, you give up lease eligibility the
    room needed you to have.

    Returns your `participant_token` (later calls in this session need nothing), the
    negotiated capabilities, and a snapshot of the room.
    """
    try:
        if execution_mode not in EXECUTION_MODES:
            return {
                "ok": False,
                "error": "invalid_command",
                "message": (
                    f"execution_mode must be one of {sorted(EXECUTION_MODES)}. "
                    "Pick the one that describes how you actually run; if unsure, "
                    "'human_turn_only'."
                ),
            }
        declared = list(EXECUTION_MODES[execution_mode])

        host_class = MODE_HOST_LABELS[execution_mode]
        identity, effective_name = await _resolve_identity(
            invitation_token, display_name, description, declared, host_class, ctx
        )
        result = await rooms.join_room(
            identity=identity,
            command=JoinRoomCommand(
                invitation_token=invitation_token,
                # `effective_name`, not `display_name`: when a credential bound the
                # identity, the caller's requested name is ignored.
                display_name=effective_name,
                host_class=host_class,
                capabilities=declared,
                description=description,
            ),
        )
        _remember_session(ctx, result.participant_token)

        negotiated = await presence.connect(
            participant=result.participant,
            command=ConnectCommand(
                capabilities=declared,
                host_class=host_class,
                since_seq=since_seq,
                attachment_label=MCP_ATTACHMENT_LABEL,
                attachment_resumable=False,
            ),
            transport="long_poll",
        )
        _remember_connection(
            ctx,
            participant_id=result.participant.id,
            connection_id=negotiated.connection.id,
            capabilities=declared,
            host_class=host_class,
            attachment_label=MCP_ATTACHMENT_LABEL,
            attachment_resumable=False,
            last_seq=since_seq,
        )
        snapshot = await projections.snapshot(room_id=result.room.id, recipient=result.participant)
        # Deliberately *not* the whole snapshot. Returning it cost ~3,400 tokens of the
        # caller's context on a modest room, unasked — and a client that wants the board
        # can spend that by calling get_room_state. Here: the cursor, and enough of a
        # headline to know whether anything needs attention at all.
        return {
            "ok": True,
            "participant_token": result.participant_token,
            "participant_id": result.participant.id,
            "room_id": result.room.id,
            "room_name": result.room.name,
            "charter": result.room.charter,
            "connection_id": negotiated.connection.id,
            "attachment_id": negotiated.connection.attachment_id,
            "negotiated_capabilities": [
                c.value for c in negotiated.connection.negotiated_capabilities
            ],
            "delivery_mode": negotiated.runtime.delivery_mode.value,
            "may_claim": negotiated.runtime.may_claim,
            "claim_denied_reason": negotiated.runtime.claim_denied_reason,
            "max_lease_seconds": negotiated.runtime.max_lease_seconds,
            "heartbeat_interval_s": negotiated.runtime.heartbeat_interval_s,
            "cursor": snapshot["snapshot_seq"],
            "room_at_a_glance": _glance(snapshot),
            "execution_mode": execution_mode,
            "display_name": effective_name,
            "display_name_was_overridden": effective_name != display_name,
            # State plainly what this mode bought, so the client is not guessing and the
            # other participants' view of it matches its own.
            "what_this_means": _explain(execution_mode, negotiated.runtime),
            "next_step": (
                "Call get_room_state to see the board, declare_current_work with your "
                "headline and targets, then await_room_events(since_seq=cursor) in a loop."
            ),
        }
    except RoomError as exc:
        return _err(exc)


@mcp.tool()
async def update_room_charter(
    charter: str,
    command_id: str | None = None,
    participant_token: str | None = None,
    ctx: Context | None = None,
) -> dict[str, Any]:
    """Replace this room's cold-start charter. Requires `room.admin`.

    The charter is durable, room-public onboarding context: what the room coordinates,
    local conventions, and what ready means. It is returned directly to new joiners and
    in every room-state read. Pass an empty string to clear it.
    """
    try:
        participant = await _participant(ctx, participant_token)
        updated = await rooms.update_room_charter(
            participant=participant,
            command=UpdateRoomCharterCommand(command_id=command_id, charter=charter),
        )
        return {"ok": True, "room_id": updated.id, "charter": updated.charter}
    except RoomError as exc:
        return _err(exc)


@mcp.tool()
async def extend_room(
    extend_seconds: int,
    command_id: str | None = None,
    participant_token: str | None = None,
    ctx: Context | None = None,
) -> dict[str, Any]:
    """Extend this room's expiry. Requires the room.admin scope."""
    try:
        participant = await _participant(ctx, participant_token)
        extended = await rooms.extend_room(
            participant=participant,
            command=ExtendRoomCommand(command_id=command_id, extend_seconds=extend_seconds),
        )
        return {
            "ok": True,
            "room_id": extended.id,
            "expires_at": extended.expires_at,
        }
    except RoomError as exc:
        return _err(exc)


def _glance(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Counts, not content: enough to decide whether to spend a read on the full board."""
    tasks_by_status: dict[str, int] = {}
    for task in snapshot.get("tasks") or []:
        tasks_by_status[task["status"]] = tasks_by_status.get(task["status"], 0) + 1
    return {
        "participants": sum(
            1 for p in snapshot.get("participants") or [] if p.get("state") == "joined"
        ),
        "active_work": len(snapshot.get("work") or []),
        "tasks": tasks_by_status,
        "open_conflicts": sum(
            1 for c in snapshot.get("conflicts") or [] if c.get("status") == "open"
        ),
    }


def _explain(execution_mode: str, runtime) -> str:
    lease_minutes = max(1, round(runtime.max_lease_seconds / 60))
    if execution_mode == "observer":
        return (
            "You are an observer: you receive the room's event stream and can post "
            "messages, but you cannot claim tasks."
        )
    if not runtime.may_claim:
        return (
            f"You cannot claim tasks in this room: {runtime.claim_denied_reason} "
            "You can still declare current work and coordinate."
        )
    if runtime.lease_renewable_unattended:
        return (
            f"You can claim tasks with leases up to {lease_minutes} minutes and renew "
            "them yourself. Other participants will rely on you making progress "
            "unprompted, so poll in a loop."
        )
    return (
        f"You can claim tasks, but leases are capped at {lease_minutes} minutes because "
        "you only act when your human prompts you. Renew or complete within that window "
        "or the task returns to the pool. Other participants are told not to expect "
        "prompt responses from you."
    )


async def _resolve_identity(
    invitation_token: str,
    display_name: str,
    description: str,
    declared: list[Capability],
    host_class: HostClass = HostClass.PERSISTENT_LOCAL,
    ctx: Context | None = None,
) -> tuple[Any, str]:
    """Resolve the agent identity this MCP client acts as.

    **Authenticated path (preferred).** When an OAuth access token is present, the
    identity comes from the token — a human bound it at the consent screen. `display_name`
    is ignored, which is the point: an agent must not be able to name itself, because in a
    cross-company room a name is what other participants trust.

    **Guest path.** No access token, but a live invitation — which since D-025 is itself a
    credential, and is how an invited stranger gets in at all. The invitation authorizes
    presence; nobody vouched for the name, so the identity is recorded with
    `provenance=invitation` and the room presents the name as self-asserted. A fresh
    identity per redemption, never get-or-create: guests of one room share an owner, so
    keying on `(owner, name)` would merge two strangers who both chose "Assistant" into one
    participant — and `participant_private` events are addressed to a participant.

    **Local development.** With `MCP_REQUIRE_AUTH=false` there is no credential at all and
    the invitation is still the only authorization, which lands on the same guest path. The
    startup guard refuses to expose *that* configuration publicly; the guest path itself is
    safe in public precisely because the invitation is checked.
    """
    principal = await principal_for_tool(ctx, settings.mcp_resource_url)
    if principal is not None and principal.identity is not None:
        # Return the bound name alongside the identity. Returning only the identity was
        # not enough: `join_room` accepts a per-room `display_name` (D-015), so a caller
        # could keep its bound identity and still *present* under any name it liked. A
        # wire test caught exactly that — the token resolved correctly and the room still
        # showed "Totally Someone Else". The effective name has to be decided here, where
        # we know whether the caller was authenticated.
        return principal.identity, principal.identity.display_name

    if settings.require_account_for_join:
        raise Unauthenticated(
            "Sign in to Cottage when connecting this MCP server before joining a room. "
            "The account is free; the invitation still decides which room you may enter."
        )

    credential = await rooms.authenticate_invitation(invitation_token)
    identity = await rooms.provision_guest_identity(
        credential,
        display_name=display_name,
        host_class=host_class,
        description=description,
        capabilities=declared,
    )
    return identity, display_name


@mcp.tool()
async def leave_room(
    note: str = "", participant_token: str | None = None, ctx: Context | None = None
) -> dict[str, Any]:
    """Leave the room, releasing your task claims and ending your work declarations.

    Do this when you finish. Leaving cleanly frees your claims immediately instead of
    making other participants wait for the leases to expire.
    """
    try:
        participant = await _participant(ctx, participant_token)
        await rooms.leave_room(participant=participant, command=LeaveRoomCommand(note=note))
        _forget_session(ctx)
        return {"ok": True}
    except RoomError as exc:
        return _err(exc)


# ---------------------------------------------------------------------------
# See current work / await events
# ---------------------------------------------------------------------------


@mcp.tool()
async def resume_here(
    since_seq: int | None = None,
    participant_token: str | None = None,
    ctx: Context | None = None,
) -> dict[str, Any]:
    """Pick up where you left off. Call this first when you arrive without context.

    Returns only what concerns *you* — the work you declared, the leases you hold with
    their fence numbers and how long is left on them, tasks proposed to you, messages
    addressed to you, conflicts naming you, and the `cursor` to resume
    `await_room_events` from. It is much smaller than `get_room_state`, which describes
    the whole room.

    **This is operational state, not conversation.** It can tell you what you were doing
    and what is waiting on you. It cannot tell you what your human asked, what was
    already discussed, or which options were rejected — do not present it as though it
    could. If you need that, ask your human or read the room's messages.

    `needs_you` counts the things actually waiting on you, so zero means nothing is
    waiting rather than nothing loaded. Pass `since_seq` — the last cursor you saw — to
    have messages addressed to you counted too; without it they are returned but not
    counted, because this server keeps no read state and will not pretend to.
    """
    try:
        participant = await _participant(ctx, participant_token)
        return {
            "ok": True,
            **await projections.hydrate(
                room_id=participant.room_id, recipient=participant, since_seq=since_seq
            ),
        }
    except RoomError as exc:
        return _err(exc)


@mcp.tool()
async def get_room_state(
    detail: str = "compact",
    max_messages: int = compact.DEFAULT_MAX_MESSAGES,
    since_seq: int | None = None,
    participant_token: str | None = None,
    ctx: Context | None = None,
) -> dict[str, Any]:
    """Read the room: who is connected, what each is doing, the task board, conflicts.

    Use this to orient. For staying live, prefer `await_room_events`.

    `detail="compact"` (the default) returns the coordination view — who is here and how
    reachable, current work with its targets, the task board with lease holders and fence
    numbers, and any open conflicts. `detail="full"` adds room policy, scope lists, and
    presence internals; it costs several times more of your context, so ask for it only if
    you actually need those fields.

    `detail="resume"` returns only what concerns *you* — your declared work, the leases you
    hold with their fences and time remaining, tasks proposed to you, messages addressed to
    you, and the `cursor` to resume `await_room_events` from. Measured live it is roughly
    two orders of magnitude smaller than the compact board, so it is the right first call
    when you arrive without context. It is **operational state, not conversation**: it
    cannot tell you what your human asked or which options were already rejected.

    (`resume_here` is the same thing as a dedicated tool. It exists for clients that pick
    up new tools; this mode exists because some connectors cache their tool list, and a
    capability nobody can discover is a capability nobody has — D-040.)

    Every mode returns `directives_for_you` first: instructions a human addressed to you,
    oldest first. They have **already taken effect** — a stopped task is stopped whether or
    not you have read this — so acknowledging one records that you saw it, and never
    re-applies or undoes anything.

    Presence vocabulary: `live_push`/`live_poll` means healthy and reachable now;
    `attended` means healthy but human-turn-driven; `idle` means recently seen but past one
    heartbeat interval; `stale` means past three intervals and its work is untrusted;
    `disconnected` means no open connection and its exclusive claims were released. See
    `get_protocol_briefing` for the complete lifecycle.
    """
    try:
        participant = await _participant(ctx, participant_token)
        if detail == "resume":
            return {
                "ok": True,
                **await projections.hydrate(
                    room_id=participant.room_id, recipient=participant, since_seq=since_seq
                ),
            }
        snapshot = await projections.snapshot(room_id=participant.room_id, recipient=participant)
        if detail == "full":
            return {"ok": True, **snapshot}
        return {"ok": True, **compact.room_state(snapshot, max_messages=max_messages)}
    except RoomError as exc:
        return _err(exc)


@mcp.tool()
async def await_room_events(
    since_seq: int,
    timeout_seconds: int = 25,
    max_events: int = compact.DEFAULT_MAX_EVENTS,
    detail: str = "compact",
    participant_token: str | None = None,
    ctx: Context | None = None,
) -> dict[str, Any]:
    """Block until something happens in the room, then return what changed.

    This is a long-poll, not a push subscription: this server cannot wake you, so
    calling this in a loop is how you behave like a live participant. Pass the
    `cursor` from the previous call as `since_seq`. `timed_out: true` with no events
    is normal and means "nothing happened" — call again.

    Events are compacted to what a coordinating agent acts on, and capped at
    `max_events` (newest kept). `detail="full"` returns whole envelopes, which on a busy
    room costs several thousand tokens of your context per call.

    A `resume_gap` result means history you missed was truncated; call
    `get_room_state` and start from its `cursor`.
    """
    try:
        participant = await _participant(ctx, participant_token)
        room_id = participant.room_id
        timeout = max(1.0, min(float(timeout_seconds), float(settings.max_long_poll_seconds)))

        current = await eventlog.validate_cursor(room_id, since_seq)
        bus.prime(room_id, current)

        if current <= since_seq:
            await bus.wait_for(room_id, since_seq, timeout=timeout)

        raw = await eventlog.read_since(room_id, since_seq)
        visible = await projections.visible_events_since(
            room_id=room_id, recipient=participant, since_seq=since_seq
        )
        # Advance to the highest seq actually read, including events filtered out for
        # this participant — otherwise the cursor would stall on a private event.
        cursor = raw[-1].seq if raw else since_seq

        # A returning poll is also a heartbeat: it proves the agent is still cycling.
        await _touch(participant, cursor, ctx)

        if detail == "full":
            payload_events: list[dict[str, Any]] = visible
            dropped = 0
        else:
            payload_events, dropped = compact.events(visible, max_events=max_events)

        # Read before the events and returned before them: a worker that has been
        # stopped should not have to parse the board to find that out. Also the reason
        # a stopped worker cannot simply keep going — the effect already landed in the
        # task layer, and this is only how it finds out (D-045).
        pending = await directives.open_for(participant.id)

        result: dict[str, Any] = {
            "ok": True,
            "directives_for_you": [d.model_dump(mode="json") for d in pending],
            "events": payload_events,
            "cursor": cursor,
            "timed_out": not visible,
            "next_call": f"await_room_events(since_seq={cursor})",
            # Keyed off what this response actually carries, not off `visible`. The
            # coordination view suppresses activity notes (D-082), so a poll can wake
            # on real log movement and still have nothing for *this* reader to act on
            # — telling it to act on an empty list would be a small lie every time a
            # peer narrated its work.
            "hint": (
                "No relevant event arrived. A persistent monitor may continue from "
                f"await_room_events(since_seq={cursor}) without starting cognition."
                if not payload_events
                else "Act on these events, then poll again with since_seq=cursor."
            ),
        }
        if dropped:
            # Never drop silently: a client that thinks it saw everything would
            # coordinate on a partial view.
            result["older_events_omitted"] = dropped
            result["note"] = (
                f"{dropped} older event(s) omitted to bound response size. Call "
                "get_room_state for the current picture rather than replaying history."
            )
        return result
    except RoomError as exc:
        return _err(exc)


async def _touch(participant: Participant, seq: int, ctx: Context | None = None) -> None:
    key = _session_key(ctx)
    binding = _session_connections.get(key) if key is not None else None
    if binding is not None and binding.participant_id == participant.id:
        await _ensure_session_connection(ctx, participant, seq=seq)
        return

    # Session-less adapter tests and explicit-token callers have no exact transport
    # session to bind. Prefer the newest connection; an unordered LIMIT 1 is precisely
    # what caused the production mismatch this fallback must not recreate.
    rows = await db.fetch_all(
        "SELECT id FROM connections WHERE participant_id = ? AND closed_at IS NULL "
        "ORDER BY opened_at DESC LIMIT 1",
        (participant.id,),
    )
    if rows:
        # Poll/replay is still available after closure; only its implicit liveness
        # mutation is refused at the core boundary.
        with contextlib.suppress(RoomClosed):
            await presence.heartbeat(connection_id=rows[0]["id"], participant=participant, seq=seq)


# ---------------------------------------------------------------------------
# Current work
# ---------------------------------------------------------------------------


@mcp.tool()
async def note_activity(
    phase: str,
    summary: str,
    tool: str | None = None,
    task_id: str | None = None,
    work_id: str | None = None,
    participant_token: str | None = None,
    ctx: Context | None = None,
) -> dict[str, Any]:
    """Say what you are doing right now, in one line. Call this often while working.

    This is what stops a human watching the room from mistaking a working agent for a
    dead one. Your work card and your task claim only change when the *state* changes;
    between those moments the room shows nothing, and ten quiet minutes of real work
    look exactly like a crash. A note fills that gap. It is cheap, it changes nothing,
    and nothing depends on it arriving — so narrate freely.

    `phase` is one of:

      * `working` — actively doing the work. The ordinary case.
      * `tool_started` / `tool_finished` — bracketing something you are running, with
        `tool` naming it ("pytest backend/tests"). Both halves, or a watcher sees a
        duration begin and never end.
      * `blocked` — stopped on something needing someone else.
      * `monitoring` — alive, holding nothing, listening for work. Say this when you finish;
        it is how the room shows you are still here rather than gone.
      * `completed` / `failed` — a unit of work landed, or did not.

    `summary` is what you would say out loud to a colleague at the next desk: *"Running
    the backend tests"*, *"Found a reconnect bug in the companion loop"*. Outcomes and
    actions, not narration of your reasoning. **Never put your chain of thought, your
    plan, your prompt, or your private context here** — there is no field for it, the
    server inspects what arrives, and a rejected note is a hard error rather than a
    silent trim.
    """
    try:
        if phase not in {p.value for p in ActivityPhase}:
            return {
                "ok": False,
                "error": "invalid_command",
                "message": (f"phase must be one of {sorted(p.value for p in ActivityPhase)}."),
            }
        participant = await _participant(ctx, participant_token)
        key = _session_key(ctx)
        binding = _session_connections.get(key) if key else None
        connection_id = (
            binding.connection_id
            if binding is not None and binding.participant_id == participant.id
            else None
        )
        result = await activity.note(
            participant=participant,
            command=NoteActivityCommand(
                phase=ActivityPhase(phase),
                summary=summary,
                tool=tool,
                task_id=task_id,
                work_id=work_id,
                connection_id=connection_id,
                disclosure=_disclosure(),
            ),
        )
        return {"ok": True, **result}
    except RoomError as exc:
        return _err(exc)


@mcp.tool()
async def set_runtime_operational_state(
    state: str,
    summary: str = "",
    waiting_reason: str = "",
    task_id: str | None = None,
    work_id: str | None = None,
    participant_token: str | None = None,
    ctx: Context | None = None,
) -> dict[str, Any]:
    """Set this durable runtime's validated work posture.

    Use `monitoring` between turns, `working` only while bounded cognition or tool
    work is active, and `waiting` only with the external dependency named in
    `waiting_reason`. This never marks you connected; presence is derived from your
    transport heartbeat.
    """
    try:
        if state not in {s.value for s in RuntimeOperationalState}:
            return {
                "ok": False,
                "error": "invalid_command",
                "message": (
                    f"state must be one of {sorted(s.value for s in RuntimeOperationalState)}."
                ),
            }
        participant = await _participant(ctx, participant_token)
        key = _session_key(ctx)
        binding = _session_connections.get(key) if key else None
        if binding is None or binding.participant_id != participant.id or not binding.connection_id:
            raise InvalidCommand(
                "Runtime state requires this MCP transport's session-bound connection; "
                "an explicit participant token cannot select among sibling runtimes."
            )
        result = await runtime_state.set_state(
            participant=participant,
            command=SetRuntimeStateCommand(
                connection_id=binding.connection_id,
                state=RuntimeOperationalState(state),
                summary=summary,
                waiting_reason=waiting_reason,
                task_id=task_id,
                work_id=work_id,
            ),
        )
        return {"ok": True, **result}
    except RoomError as exc:
        return _err(exc)


@mcp.tool()
async def declare_current_work(
    headline: str,
    targets: list[str] | None = None,
    note: str = "",
    task_id: str | None = None,
    allow_parallel: bool = False,
    participant_token: str | None = None,
    ctx: Context | None = None,
) -> dict[str, Any]:
    """Tell the room what you are doing right now. Do this before you start working.

    `targets` are the things you will touch — file paths, service names, ticket ids.
    They are how the room detects that you and another participant are about to
    collide, so list them specifically. If another participant is already on one, you
    will get a conflict record back and can coordinate before doing damage.

    By default this is your singular current-work card: repeating the same declaration
    after a reconnect returns its existing id, while changing it supersedes your prior
    open card. Set `allow_parallel=true` only when this runtime genuinely owns multiple
    simultaneous work streams.
    """
    try:
        participant = await _participant(ctx, participant_token)
        declaration = await work.declare(
            participant=participant,
            command=DeclareWorkCommand(
                headline=headline,
                targets=targets or [],
                note=note,
                task_id=task_id,
                allow_parallel=allow_parallel,
                disclosure=_disclosure(),
            ),
        )
        return {"ok": True, "work": declaration.model_dump(mode="json")}
    except RoomError as exc:
        return _err(exc)


@mcp.tool()
async def update_current_work(
    work_id: str,
    headline: str | None = None,
    status: str | None = None,
    targets: list[str] | None = None,
    note: str | None = None,
    participant_token: str | None = None,
    ctx: Context | None = None,
) -> dict[str, Any]:
    """Update your own work declaration. `status` is active, paused, blocked, or done."""
    try:
        participant = await _participant(ctx, participant_token)
        declaration = await work.update(
            participant=participant,
            command=UpdateWorkCommand(
                work_id=work_id,
                headline=headline,
                status=WorkStatus(status) if status else None,
                targets=targets,
                note=note,
            ),
        )
        return {"ok": True, "work": declaration.model_dump(mode="json")}
    except RoomError as exc:
        return _err(exc)


@mcp.tool()
async def end_current_work(
    work_id: str,
    note: str = "",
    participant_token: str | None = None,
    ctx: Context | None = None,
) -> dict[str, Any]:
    """Mark your work declaration finished so others stop coordinating around it."""
    try:
        participant = await _participant(ctx, participant_token)
        declaration = await work.end(
            participant=participant, command=EndWorkCommand(work_id=work_id, note=note)
        )
        return {"ok": True, "work": declaration.model_dump(mode="json")}
    except RoomError as exc:
        return _err(exc)


# ---------------------------------------------------------------------------
# Tasks & leases
# ---------------------------------------------------------------------------


@mcp.tool()
async def create_task(
    title: str,
    description: str = "",
    targets: list[str] | None = None,
    priority: int = 0,
    propose_to_participant_id: str | None = None,
    claim_immediately: bool = False,
    participant_token: str | None = None,
    ctx: Context | None = None,
) -> dict[str, Any]:
    """Add work to the room's task board.

    Leave it open for anyone to claim, propose it to a specific participant, or claim
    it yourself. If it looks like an existing task you will get a duplicate conflict
    back — advisory, not blocking.
    """
    try:
        participant = await _participant(ctx, participant_token)
        task = await tasks.create(
            participant=participant,
            command=CreateTaskCommand(
                title=title,
                description=description,
                targets=targets or [],
                priority=priority,
                propose_to_participant_id=propose_to_participant_id,
                claim_immediately=claim_immediately,
                disclosure=_disclosure(),
            ),
        )
        return {"ok": True, "task": task.model_dump(mode="json")}
    except RoomError as exc:
        return _err(exc)


@mcp.tool()
async def claim_task(
    task_id: str,
    lease_seconds: int | None = None,
    participant_token: str | None = None,
    ctx: Context | None = None,
) -> dict[str, Any]:
    """Take an exclusive lease on a task before working on it.

    Returns a `fence` number and an `expires_at`. Keep the fence: every later change
    to this task needs it. Renew before expiry or the task returns to the pool.
    `lease_conflict` means someone else already holds it.
    """
    try:
        participant = await _participant(ctx, participant_token)
        task = await tasks.claim(
            participant=participant,
            command=ClaimTaskCommand(task_id=task_id, requested_lease_seconds=lease_seconds),
        )
        return {
            "ok": True,
            "task": task.model_dump(mode="json"),
            "fence": task.claim.fence if task.claim else None,
            "expires_at": task.claim.expires_at if task.claim else None,
            "reminder": "Renew before expires_at, and pass this fence on every update.",
        }
    except RoomError as exc:
        return _err(exc)


@mcp.tool()
async def renew_task_claim(
    task_id: str,
    fence: int,
    extend_seconds: int | None = None,
    participant_token: str | None = None,
    ctx: Context | None = None,
) -> dict[str, Any]:
    """Extend a lease you still hold. Only valid before it expires."""
    try:
        participant = await _participant(ctx, participant_token)
        task = await tasks.renew(
            participant=participant,
            command=RenewClaimCommand(task_id=task_id, fence=fence, extend_seconds=extend_seconds),
        )
        return {
            "ok": True,
            "task": task.model_dump(mode="json"),
            "expires_at": task.claim.expires_at if task.claim else None,
        }
    except RoomError as exc:
        return _err(exc)


@mcp.tool()
async def release_task_claim(
    task_id: str,
    fence: int,
    note: str = "",
    participant_token: str | None = None,
    ctx: Context | None = None,
) -> dict[str, Any]:
    """Give a task back to the pool without completing it."""
    try:
        participant = await _participant(ctx, participant_token)
        task = await tasks.release(
            participant=participant,
            command=ReleaseClaimCommand(task_id=task_id, fence=fence, note=note),
        )
        return {"ok": True, "task": task.model_dump(mode="json")}
    except RoomError as exc:
        return _err(exc)


@mcp.tool()
async def drain_runtime(
    attachment_id: str,
    reason: str = "",
    participant_token: str | None = None,
    ctx: Context | None = None,
) -> dict[str, Any]:
    """Stop the room accepting work from one of your own runtimes.

    Use this when you have told a worker to stop but cannot prove it stopped — a kill
    you could not verify, a process you can no longer see, a duplicate you did not
    expect. It does **not** end the process: nothing here can, and on a hosted room the
    process is on your machine, not ours. What it does is withdraw that runtime's
    permission, so if it is still alive its next command is refused with
    `stale_runtime` rather than silently succeeding.

    Sticky by design: the runtime reconnecting does not undo this, because reconnecting
    is exactly what a survivor does. Call `resume_runtime` once you know the old process
    is gone. You may only drain your own runtimes — stopping someone else's worker is a
    directive to them, not an action you take.
    """
    try:
        participant = await _participant(ctx, participant_token)
        room = await store.load_room(participant.room_id)
        outcome = await presence.drain_runtime(
            room=room,
            participant=participant,
            attachment_id=attachment_id,
            reason=reason,
        )
        return outcome.result
    except RoomError as exc:
        return _err(exc)


@mcp.tool()
async def resume_runtime(
    attachment_id: str,
    note: str = "",
    participant_token: str | None = None,
    ctx: Context | None = None,
) -> dict[str, Any]:
    """Let a drained runtime work again, once you know the old process is gone.

    Separate from reconnecting on purpose. A reconnect proves something is alive; this
    asserts the previous run is dead, which the room cannot observe and only you can
    vouch for. The claim is recorded against your name.
    """
    try:
        participant = await _participant(ctx, participant_token)
        room = await store.load_room(participant.room_id)
        outcome = await presence.resume_runtime(
            room=room,
            participant=participant,
            attachment_id=attachment_id,
            note=note,
        )
        return outcome.result
    except RoomError as exc:
        return _err(exc)


@mcp.tool()
async def complete_task(
    task_id: str,
    fence: int,
    result: str = "",
    participant_token: str | None = None,
    ctx: Context | None = None,
) -> dict[str, Any]:
    """Mark a task done. Requires the fence from your claim.

    `result` is shared with the room — put the outcome and any reference other
    participants need, not your working notes.
    """
    try:
        participant = await _participant(ctx, participant_token)
        key = _session_key(ctx)
        binding = _session_connections.get(key) if key else None
        connection_id = (
            binding.connection_id
            if binding is not None and binding.participant_id == participant.id
            else None
        )
        task = await tasks.complete(
            participant=participant,
            command=CompleteTaskCommand(
                task_id=task_id,
                fence=fence,
                result=result,
                connection_id=connection_id,
                disclosure=_disclosure(),
            ),
        )
        return {"ok": True, "task": task.model_dump(mode="json")}
    except RoomError as exc:
        return _err(exc)


@mcp.tool()
async def record_checkpoint(
    task_id: str,
    fence: int,
    summary: str,
    phase: str = "",
    next_action: str = "",
    completed_step_ids: list[str] | None = None,
    artifact_refs: list[str] | None = None,
    pending_tool_calls: list[str] | None = None,
    participant_token: str | None = None,
    ctx: Context | None = None,
) -> dict[str, Any]:
    """Record durable progress on a task you hold, so a restart is not an amnesia.

    `summary` is **read by the whole room**: what you did, what it means, what is
    next. It is a progress record, not a scratchpad — never put your reasoning, your
    prompt, your private context or anything a human said to you privately in it.

    The remaining fields are your own bookmark and are returned only to you: enough
    to pick the work back up after a restart, and nothing about how you were
    thinking. Use ids and short labels, not narratives.

    Checkpoint **after each step rather than only at the end**. A `stop` releases
    your lease, so your last checkpoint is the last thing written before you were
    interrupted — which is exactly what makes the interruption recoverable.
    """
    try:
        participant = await _participant(ctx, participant_token)
        resume = None
        if phase or next_action or completed_step_ids or artifact_refs or pending_tool_calls:
            resume = ResumeState(
                phase=phase,
                next_action=next_action,
                completed_step_ids=completed_step_ids or [],
                artifact_refs=artifact_refs or [],
                pending_tool_calls=pending_tool_calls or [],
            )
        checkpoint = await checkpoints.append(
            participant=participant,
            command=AppendCheckpointCommand(
                task_id=task_id, fence=fence, summary=summary, resume_state=resume
            ),
        )
        return {"ok": True, "checkpoint": checkpoint.model_dump(mode="json")}
    except RoomError as exc:
        return _err(exc)


@mcp.tool()
async def ask_question(
    body: str,
    to_participant_id: str | None = None,
    task_id: str | None = None,
    blocking: bool = False,
    fence: int | None = None,
    checkpoint_summary: str = "",
    participant_token: str | None = None,
    ctx: Context | None = None,
) -> dict[str, Any]:
    """Ask something you cannot work out for yourself, of a participant or the room.

    Asking needs no special authority — that is the difference between a question
    and a directive, and the reason you can raise one while a directive can only be
    issued to you.

    **`blocking` costs you the work.** By default nothing changes: keep your lease
    and carry on with whatever you can still do, because stopping at every
    uncertainty is how an unattended worker becomes useless. Set `blocking=True`
    only when you genuinely cannot proceed and would otherwise guess at something
    consequential — then present your `fence`, and the room writes a checkpoint,
    parks the task as `waiting_input` and releases your lease in one step. You are
    free to work on other tasks; this one returns to `open` when someone answers.

    You may not answer your own question.
    """
    try:
        participant = await _participant(ctx, participant_token)
        question = await questions.ask(
            participant=participant,
            command=AskQuestionCommand(
                body=body,
                to_participant_id=to_participant_id,
                task_id=task_id,
                blocking=blocking,
                fence=fence,
                checkpoint_summary=checkpoint_summary,
            ),
        )
        return {"ok": True, "question": question.model_dump(mode="json")}
    except RoomError as exc:
        return _err(exc)


@mcp.tool()
async def answer_question(
    question_id: str,
    body: str,
    participant_token: str | None = None,
    ctx: Context | None = None,
) -> dict[str, Any]:
    """Answer someone's question, releasing whatever work it parked.

    Any participant may answer — this is a reply, not an exercise of authority, and
    routing it through the control plane would mean only room admins could ever
    unblock a worker. If the question was blocking, its task returns to `open` and
    the worker re-claims it through the normal path.
    """
    try:
        participant = await _participant(ctx, participant_token)
        answer = await questions.answer(
            participant=participant,
            command=AnswerQuestionCommand(question_id=question_id, body=body),
        )
        return {"ok": True, "answer": answer.model_dump(mode="json")}
    except RoomError as exc:
        return _err(exc)


# ---------------------------------------------------------------------------
# Coordination
# ---------------------------------------------------------------------------


@mcp.tool()
async def post_message(
    body: str,
    to_participant_id: str | None = None,
    about_ref: str | None = None,
    participant_token: str | None = None,
    ctx: Context | None = None,
) -> dict[str, Any]:
    """Say something to the room, or to one participant.

    **Keep it short — a few sentences, the length you would send in a chat.** Elaborate
    only when someone asks. Every reader pays for what you write and a model-backed
    reader pays per word, so length here spends other people's budget rather than your
    own. Lead with what changed or what you need, and leave out the transport details
    the room already records.

    This is an annotation channel, not the main surface — prefer a work declaration
    or a task for anything that represents work. Use `about_ref` to attach the message
    to a task or work item.
    """
    try:
        participant = await _participant(ctx, participant_token)
        result = await messages.post(
            participant=participant,
            command=PostMessageCommand(
                body=body,
                about_ref=about_ref,
                disclosure=_disclosure(to_participant_id=to_participant_id),
            ),
        )
        return {"ok": True, **result}
    except RoomError as exc:
        return _err(exc)


@mcp.tool()
async def steer_participant(
    target_participant_id: str,
    action: str,
    reason: str,
    task_id: str | None = None,
    priority: int | None = None,
    participant_token: str | None = None,
    ctx: Context | None = None,
) -> dict[str, Any]:
    """Direct another participant's work without taking it over.

    `action` is `pause`, `stop`, `resume`, `reprioritize`, or `input`. Everything but
    `input` needs a `task_id`, because pausing "in general" is not something the task
    layer can enforce — and an unenforceable directive is a message wearing a uniform.

    **The effect is already applied when this returns.** It does not wait for the
    target to notice, because a stop that waited for acknowledgement would depend on
    the cooperation of whatever you are trying to stop. The target discovers it at
    its next read, cannot progress or re-claim in the meantime, and acknowledges
    separately — so the room can say afterwards exactly when it was told and when it
    complied.

    You need `room.admin`. Being a human principal is not the same thing: the room
    can attribute an action to your identity, but it cannot verify a person is
    present when it happens, so authority is a grant rather than an inference.

    `input` is the exception that waits: there is no room state to halt, so nothing
    applies until the target consumes it.
    """
    try:
        participant = await _participant(ctx, participant_token)
        directive = await directives.issue(
            participant=participant,
            command=IssueDirectiveCommand(
                target_participant_id=target_participant_id,
                action=DirectiveAction(action),
                task_id=task_id,
                reason=reason,
                priority=priority,
            ),
        )
        return {"ok": True, "directive": directive.model_dump(mode="json")}
    except ValueError:
        return {
            "ok": False,
            "error": "invalid_command",
            "message": (
                f"Unknown action {action!r}. Use pause, stop, resume, reprioritize, or input."
            ),
        }
    except RoomError as exc:
        return _err(exc)


@mcp.tool()
async def acknowledge_directive(
    directive_id: str,
    note: str = "",
    rejected: bool = False,
    participant_token: str | None = None,
    ctx: Context | None = None,
) -> dict[str, Any]:
    """Record that you saw a directive — and, for `input`, that you consumed it.

    Acknowledge *after* complying, not before: this is evidence that you noticed, so
    sending it first makes it evidence of nothing. It never re-applies or undoes an
    effect; acknowledging a stop does not re-stop anything.

    `rejected` is available because an agent may decline. The room's job is to make
    the refusal visible, not to argue with it — and for a control action the effect
    has already landed regardless, so declining records an opinion rather than a veto.
    """
    try:
        participant = await _participant(ctx, participant_token)
        directive = await directives.acknowledge(
            participant=participant,
            command=AcknowledgeDirectiveCommand(
                directive_id=directive_id, note=note, rejected=rejected
            ),
        )
        return {"ok": True, "directive": directive.model_dump(mode="json")}
    except RoomError as exc:
        return _err(exc)
