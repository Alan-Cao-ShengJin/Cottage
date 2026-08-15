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

import logging
from collections import OrderedDict
from typing import Any
from urllib.parse import urlparse

from mcp.server.fastmcp import Context, FastMCP
from mcp.server.streamable_http import MCP_SESSION_ID_HEADER
from mcp.server.transport_security import TransportSecuritySettings

from ...config import settings
from ...core import (
    eventlog,
    messages,
    presence,
    projections,
    rooms,
    store,
    tasks,
    work,
)
from ...core.bus import bus
from ...core.errors import RoomError, Unauthenticated
from ...domain.capabilities import Capability, HostClass
from ...domain.commands import (
    ClaimTaskCommand,
    CompleteTaskCommand,
    ConnectCommand,
    CreateRoomCommand,
    CreateTaskCommand,
    DeclareWorkCommand,
    EndWorkCommand,
    JoinRoomCommand,
    LeaveRoomCommand,
    PostMessageCommand,
    ReleaseClaimCommand,
    RenewClaimCommand,
    UpdateWorkCommand,
)
from ...domain.disclosure import Audience, Disclosure
from ...domain.room import Participant, PrivacyClass, RoomVisibility
from ...domain.work import WorkStatus
from . import compact
from .auth import principal_for_tool

log = logging.getLogger(__name__)

INSTRUCTIONS = (
    "Agent Rooms: a live coordination network for independently owned agents. "
    "This is not a chat server — the point is shared work awareness. "
    "Call get_protocol_briefing first, then join_room, then declare_current_work. "
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


def _remember_session(ctx: Context | None, participant_token: str) -> None:
    key = _session_key(ctx)
    if key is None:
        return
    _session_tokens[key] = participant_token
    _session_tokens.move_to_end(key)
    while len(_session_tokens) > _SESSION_TOKEN_LIMIT:
        _session_tokens.popitem(last=False)


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
    return await store.load_participant_by_token(resolved)


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

* **Creating:** `create_room(principal_token, name)` → you are the owner, already joined,
  and you get a `join_token`. Hand that one token to everyone else. Nothing else needed.
* **Joining:** `join_room(invitation_token, display_name)`. One call. That token is the
  only way in — there is no open door.

## Declare how you run, honestly
`join_room` requires an `execution_mode`, and there is no safe default:

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
7. `update_current_work` as your status changes; `end_current_work` when done.
8. `leave_room` when finished. This releases your claims immediately rather than
   making everyone wait for expiry.

## Errors are information
`lease_conflict` means someone else is on it — pick different work or say something.
`stale_fence` means you no longer hold the claim. `capability_unsupported` means you
did not declare a capability the action requires. None of these are crashes.
"""


# ---------------------------------------------------------------------------
# Connect
# ---------------------------------------------------------------------------


@mcp.tool()
async def create_room(
    principal_token: str,
    name: str,
    purpose: str = "",
    display_name: str = "Room creator",
    cross_org: bool = False,
    ctx: Context | None = None,
) -> dict[str, Any]:
    """Create a room, join it as owner, and get a token to share with everyone else.

    `principal_token` is your organization credential (creating a room is an org-level
    act, so a room-scoped token cannot do it). You get back:

      * `join_token` — the one thing you share. Anyone you give it to calls
        `join_room(invitation_token=<join_token>, display_name="...")`.
      * `participant_token` — yours. You are already in the room; this session is bound
        to it, so subsequent tools work without passing anything.

    Both tokens are shown once and stored only as hashes.
    """
    try:
        principal = await rooms.authenticate_principal(principal_token)
        if principal.user is None:
            return {
                "ok": False,
                "error": "forbidden",
                "message": (
                    "Creating a room needs a user principal token, not an agent identity "
                    "token. Ask a human in your organization for one."
                ),
            }

        created = await rooms.create_room(
            user=principal.user,
            command=CreateRoomCommand(
                name=name,
                purpose=purpose,
                visibility=RoomVisibility.CROSS_ORG if cross_org else RoomVisibility.INTERNAL,
            ),
            creator_display_name=display_name,
        )
        # Bind this session so later tools need no token, and open a polling connection
        # so the creator is present rather than a room with nobody in it.
        _remember_session(ctx, created.participant_token)
        declared = _default_agent_capabilities()
        negotiated = await presence.connect(
            participant=created.participant,
            command=ConnectCommand(capabilities=declared),
            transport="long_poll",
        )
        return {
            "ok": True,
            "room_id": created.room.id,
            "room_name": created.room.name,
            "join_token": created.join_token,
            "participant_token": created.participant_token,
            "participant_id": created.participant.id,
            "connection_id": negotiated.connection.id,
            "cursor": await eventlog.current_seq(created.room.id),
            "share_this": (
                f"Give join_token to each participant. They call "
                f'join_room(invitation_token="{created.join_token}", display_name="...").'
            ),
            "next_step": (
                "Call declare_current_work, then await_room_events(since_seq=cursor) in a loop."
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
    display_name: str,
    execution_mode: str,
    description: str = "",
    since_seq: int = 0,
    ctx: Context | None = None,
) -> dict[str, Any]:
    """Redeem a join token and enter the room. One call — this is the only way in.

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
            command=ConnectCommand(capabilities=declared, since_seq=since_seq),
            transport="long_poll",
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
            "connection_id": negotiated.connection.id,
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
        key = _session_key(ctx)
        if key:
            _session_tokens.pop(key, None)
        return {"ok": True}
    except RoomError as exc:
        return _err(exc)


# ---------------------------------------------------------------------------
# See current work / await events
# ---------------------------------------------------------------------------


@mcp.tool()
async def get_room_state(
    detail: str = "compact",
    max_messages: int = compact.DEFAULT_MAX_MESSAGES,
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
    """
    try:
        participant = await _participant(ctx, participant_token)
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
        await _touch(participant, cursor)

        if detail == "full":
            payload_events: list[dict[str, Any]] = visible
            dropped = 0
        else:
            payload_events, dropped = compact.events(visible, max_events=max_events)

        result: dict[str, Any] = {
            "ok": True,
            "events": payload_events,
            "cursor": cursor,
            "timed_out": not visible,
            "hint": (
                "Call await_room_events again with since_seq=cursor."
                if not visible
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


async def _touch(participant: Participant, seq: int) -> None:
    from ...db import database as db

    rows = await db.fetch_all(
        "SELECT id FROM connections WHERE participant_id = ? AND closed_at IS NULL LIMIT 1",
        (participant.id,),
    )
    if rows:
        await presence.heartbeat(connection_id=rows[0]["id"], participant=participant, seq=seq)


# ---------------------------------------------------------------------------
# Current work
# ---------------------------------------------------------------------------


@mcp.tool()
async def declare_current_work(
    headline: str,
    targets: list[str] | None = None,
    note: str = "",
    task_id: str | None = None,
    participant_token: str | None = None,
    ctx: Context | None = None,
) -> dict[str, Any]:
    """Tell the room what you are doing right now. Do this before you start working.

    `targets` are the things you will touch — file paths, service names, ticket ids.
    They are how the room detects that you and another participant are about to
    collide, so list them specifically. If another participant is already on one, you
    will get a conflict record back and can coordinate before doing damage.
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
        task = await tasks.complete(
            participant=participant,
            command=CompleteTaskCommand(
                task_id=task_id, fence=fence, result=result, disclosure=_disclosure()
            ),
        )
        return {"ok": True, "task": task.model_dump(mode="json")}
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
