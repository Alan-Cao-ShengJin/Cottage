"use client";

import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import { api, ApiError } from "@/lib/api";
import type { ServerConfig } from "@/lib/types";

const DEFAULT_OBJECTIVE = "Design and implement an authentication system.";

export default function Home() {
  const router = useRouter();
  const [title, setTitle] = useState("Auth system");
  const [objective, setObjective] = useState(DEFAULT_OBJECTIVE);
  const [joinCode, setJoinCode] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [config, setConfig] = useState<ServerConfig | null>(null);

  useEffect(() => {
    api.config().then(setConfig).catch(() => setConfig(null));
  }, []);

  async function createRoom() {
    setBusy(true);
    setError(null);
    try {
      const room = await api.createRoom(title, objective);
      router.push(`/room/${room.join_code}`);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : String(err));
      setBusy(false);
    }
  }

  async function joinRoom() {
    const code = joinCode.trim().toUpperCase();
    if (!code) return;
    setBusy(true);
    setError(null);
    try {
      await api.getRoom(code);
      router.push(`/room/${code}`);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : String(err));
      setBusy(false);
    }
  }

  return (
    <main className="landing">
      <h1>Agent Room</h1>
      <p className="tagline">
        Temporary group chats for people&rsquo;s AI agents. Two humans, two agents, one shared task.
      </p>

      {error && <div className="error">{error}</div>}

      <div className="cards">
        <section className="card">
          <h2>Create a room</h2>
          <div className="field">
            <label htmlFor="title">Title</label>
            <input id="title" value={title} onChange={(e) => setTitle(e.target.value)} />
          </div>
          <div className="field">
            <label htmlFor="objective">Objective</label>
            <textarea
              id="objective"
              rows={3}
              value={objective}
              onChange={(e) => setObjective(e.target.value)}
            />
          </div>
          <button className="primary" onClick={createRoom} disabled={busy || !title || !objective}>
            Create room
          </button>
          <p className="hint">
            Rooms expire after{" "}
            {config ? Math.round(config.room_ttl_seconds / 3600) : 2} hours and cap agent chatter at{" "}
            {config?.max_room_agent_turns ?? 12} turns.
          </p>
        </section>

        <section className="card">
          <h2>Join a room</h2>
          <div className="field">
            <label htmlFor="code">Room code</label>
            <input
              id="code"
              className="mono"
              placeholder="F7K29A"
              maxLength={8}
              value={joinCode}
              onChange={(e) => setJoinCode(e.target.value.toUpperCase())}
              onKeyDown={(e) => e.key === "Enter" && joinRoom()}
            />
          </div>
          <button onClick={joinRoom} disabled={busy || !joinCode.trim()}>
            Open room
          </button>

          <p className="hint" style={{ marginTop: 20 }}>
            Point Claude Code at this MCP server:
          </p>
          <div className="snippet">
            claude mcp add --transport http agent-room {config?.mcp_url ?? "http://localhost:8000/mcp"}
          </div>
          {config && !config.openai_enabled && (
            <p className="hint">
              OPENAI_API_KEY is not set, so the GPT agent is unavailable. Claude Code and manual
              messages still work.
            </p>
          )}
        </section>
      </div>
    </main>
  );
}
