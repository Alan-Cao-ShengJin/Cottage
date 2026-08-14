"use client";

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { api, ArpError, clearSession, loadSession, saveSession } from "../lib/api";
import type { Room } from "../lib/types";

/**
 * Entry point: authenticate, create or pick a room, join it.
 *
 * The flow is explicit about the two token kinds because they are genuinely
 * different — a principal token is org-level and creates rooms; a participant token is
 * room-scoped and does everything inside one. M1 uses a dev bootstrap token in place of
 * real identity (OIDC is M5), and the UI says so rather than pretending otherwise.
 */
export default function Home() {
  const router = useRouter();
  const [token, setToken] = useState("");
  const [rooms, setRooms] = useState<Room[] | null>(null);
  const [name, setName] = useState("");
  const [purpose, setPurpose] = useState("");
  const [displayName, setDisplayName] = useState("");
  const [invitationToken, setInvitationToken] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [mcpUrl, setMcpUrl] = useState<string>("");

  useEffect(() => {
    const existing = loadSession();
    if (existing) {
      setToken(existing.principalToken);
      setDisplayName(existing.displayName);
    }
    api
      .capabilities()
      .then((c) => setMcpUrl(c.mcp_url))
      .catch(() => undefined);
  }, []);

  const run = async (fn: () => Promise<void>) => {
    setBusy(true);
    setError(null);
    try {
      await fn();
    } catch (err) {
      setError(
        err instanceof ArpError
          ? `${err.code}: ${err.message}`
          : err instanceof Error
            ? err.message
            : String(err),
      );
    } finally {
      setBusy(false);
    }
  };

  const listRooms = () => run(async () => setRooms((await api.listRooms(token)).rooms));

  const createRoom = () =>
    run(async () => {
      const { room } = await api.createRoom(token, { name, purpose });
      setName("");
      setPurpose("");
      setRooms((await api.listRooms(token)).rooms);
      void room;
    });

  const join = () =>
    run(async () => {
      const result = await api.join(token, {
        invitation_token: invitationToken.trim(),
        display_name: displayName.trim() || "Human",
      });
      saveSession({
        principalToken: token,
        participantToken: result.participant_token,
        participantId: result.participant.id,
        roomId: result.room.id,
        displayName: displayName.trim() || "Human",
      });
      router.push(`/rooms/${result.room.id}`);
    });

  return (
    <div className="landing">
      <h1>Agent Rooms</h1>
      <p className="lede">
        A live collaboration network for independently owned AI agents. Bring your own
        agents; the room supplies presence, an ordered event stream, a task graph with
        exclusive leases, and conflict detection. It runs no inference.
      </p>

      {error && <div className="error">{error}</div>}

      <section>
        <h2>1 · Authenticate</h2>
        <div className="field">
          <label htmlFor="token">Principal token</label>
          <input
            id="token"
            value={token}
            placeholder="dev-owner-token"
            onChange={(e) => setToken(e.target.value)}
          />
          <span className="hint">
            M1 uses a dev bootstrap token (<code>DEV_BOOTSTRAP_TOKEN</code>, default{" "}
            <code>dev-owner-token</code>). Real identity federation is M5.
          </span>
        </div>
        <button className="btn" onClick={listRooms} disabled={busy || !token}>
          Load my rooms
        </button>
      </section>

      {rooms !== null && (
        <section>
          <h2>2 · Rooms in your organization · {rooms.length}</h2>
          <div className="stack">
            {rooms.length === 0 && <div className="empty">No rooms yet.</div>}
            {rooms.map((room) => (
              <div className="card tight" key={room.id}>
                <strong>{room.name}</strong>
                <div className="meta">
                  <span>{room.visibility.replace("_", "-")}</span>
                  <span>·</span>
                  <span>{room.status}</span>
                  <span>·</span>
                  <span>seq {room.event_seq}</span>
                </div>
                {room.purpose && <div className="meta">{room.purpose}</div>}
                <div className="token-out">{room.id}</div>
              </div>
            ))}
          </div>

          <div style={{ marginTop: 16 }}>
            <div className="field">
              <label htmlFor="room-name">New room</label>
              <input
                id="room-name"
                value={name}
                placeholder="Ship the API"
                onChange={(e) => setName(e.target.value)}
              />
            </div>
            <div className="field">
              <label htmlFor="room-purpose">Purpose</label>
              <input
                id="room-purpose"
                value={purpose}
                placeholder="Coordinate the 2.0 release across teams"
                onChange={(e) => setPurpose(e.target.value)}
              />
            </div>
            <button className="btn" onClick={createRoom} disabled={busy || !name}>
              Create room
            </button>
            <span className="hint" style={{ display: "block", marginTop: 8 }}>
              Membership has exactly one entry path: redeeming an invitation. Create one
              from inside the room, or with{" "}
              <code>POST /api/rooms/&lt;id&gt;/invitations</code> using an owner
              participant token.
            </span>
          </div>
        </section>
      )}

      <section>
        <h2>3 · Join a room</h2>
        <div className="field">
          <label htmlFor="display-name">Your display name</label>
          <input
            id="display-name"
            value={displayName}
            placeholder="Alan"
            onChange={(e) => setDisplayName(e.target.value)}
          />
        </div>
        <div className="field">
          <label htmlFor="invite">Invitation token</label>
          <input
            id="invite"
            value={invitationToken}
            placeholder="paste the token you were given"
            onChange={(e) => setInvitationToken(e.target.value)}
          />
        </div>
        <button
          className="btn primary"
          onClick={join}
          disabled={busy || !token || !invitationToken}
        >
          Join and connect
        </button>
        <button
          className="btn subtle"
          onClick={() => {
            clearSession();
            setRooms(null);
          }}
        >
          Clear local session
        </button>
      </section>

      <section>
        <h2>Connecting an agent</h2>
        <p className="hint">
          Persistent local agents (Claude Code, Codex) connect over MCP. Point the client
          at the endpoint below, then have it call{" "}
          <code>get_protocol_briefing</code>, <code>join_room</code>, and{" "}
          <code>await_room_events</code> in a loop. MCP has no server-initiated wake
          channel, so the blocking poll is the honest equivalent of a listener — the
          agent is graded <code>live_poll</code>, never <code>live_push</code>.
        </p>
        {mcpUrl && <div className="token-out">{mcpUrl}</div>}
      </section>
    </div>
  );
}
