"use client";

import { use, useEffect, useMemo, useRef, useState } from "react";
import { api, ApiError } from "@/lib/api";
import type { Agent, Message, SharedMemoryData, ServerConfig, Task } from "@/lib/types";
import { formatDuration, useCountdown, useRoomStream } from "@/lib/useRoomStream";

export default function RoomPage({ params }: { params: Promise<{ code: string }> }) {
  const { code } = use(params);
  const { snapshot, connection, error } = useRoomStream(code);
  const [config, setConfig] = useState<ServerConfig | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [draft, setDraft] = useState("");
  const [busy, setBusy] = useState(false);
  const [copied, setCopied] = useState(false);
  const messagesRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    api.config().then(setConfig).catch(() => setConfig(null));
  }, []);

  const room = snapshot?.room;
  const remaining = useCountdown(room?.expires_at);
  const expired = !room || room.status !== "active" || remaining <= 0;

  useEffect(() => {
    messagesRef.current?.scrollTo({ top: messagesRef.current.scrollHeight, behavior: "smooth" });
  }, [snapshot?.messages.length]);

  const gptPresent = useMemo(
    () => snapshot?.agents.some((a) => a.provider === "openai" && a.status === "active") ?? false,
    [snapshot?.agents],
  );

  async function run(action: () => Promise<unknown>) {
    setBusy(true);
    setActionError(null);
    try {
      await action();
    } catch (err) {
      setActionError(err instanceof ApiError ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }

  const send = () => {
    const content = draft.trim();
    if (!content) return;
    setDraft("");
    void run(() => api.postHumanMessage(code, content, "Human"));
  };

  const addGpt = () =>
    run(() =>
      api.spawnGptAgent(code, {
        owner_name: "Alan",
        agent_name: "Alan-GPT",
        public_objective: "Design the authentication architecture.",
        private_instructions:
          "You are the architecture agent. Another agent is implementing the backend. " +
          "Coordinate directly with it: state your recommendations, ask what it is doing, " +
          "and challenge choices that conflict with the threat model.",
      }),
    );

  if (error && !snapshot) {
    return (
      <main className="landing">
        <h1>Room {code}</h1>
        <div className="error">{error}</div>
        <a href="/">Back</a>
      </main>
    );
  }

  if (!snapshot || !room) {
    return (
      <main className="landing">
        <p className="muted">Loading room {code}&hellip;</p>
      </main>
    );
  }

  return (
    <div className="room">
      <header className="room-header">
        <span
          className="code-badge"
          title="Click to copy"
          onClick={() => {
            void navigator.clipboard.writeText(room.join_code);
            setCopied(true);
            setTimeout(() => setCopied(false), 1400);
          }}
          style={{ cursor: "pointer" }}
        >
          {room.join_code}
        </span>
        {copied && <span className="pill live">copied</span>}

        <div className="room-objective">
          <strong>{room.title}</strong> &mdash; {room.objective}
        </div>

        <div className="header-right">
          <span className={`pill ${connection === "live" ? "live" : "warn"}`}>
            {connection === "live" ? "live" : connection}
          </span>
          <span className="pill">
            turns {room.agent_turns_used}/{room.max_agent_turns}
          </span>
          <span className={`pill ${room.autonomy_enabled ? "live" : "warn"}`}>
            {room.autonomy_enabled ? "autonomous" : "paused"}
          </span>
          <span className={`countdown ${expired ? "expired" : ""}`}>
            {expired ? "EXPIRED" : `expires ${formatDuration(remaining)}`}
          </span>
        </div>
      </header>

      <div className="columns">
        {/* ---------------- agents ---------------- */}
        <aside className="col">
          <h3>Agents</h3>
          {snapshot.agents.length === 0 && <p className="mem-empty">Nobody has joined yet.</p>}
          {snapshot.agents.map((agent) => (
            <AgentCard key={agent.id} agent={agent} />
          ))}

          <h4>Controls</h4>
          {actionError && <div className="error">{actionError}</div>}
          <div className="controls">
            <button
              onClick={addGpt}
              disabled={busy || expired || gptPresent || !config?.openai_enabled}
              title={
                !config?.openai_enabled
                  ? "Set OPENAI_API_KEY in .env to enable the GPT agent"
                  : gptPresent
                    ? "The GPT agent is already in this room"
                    : ""
              }
            >
              Add GPT agent
            </button>
            <button
              onClick={() => run(() => api.setAutonomy(code, !room.autonomy_enabled))}
              disabled={busy || expired}
            >
              {room.autonomy_enabled ? "Stop collaboration" : "Start collaboration"}
            </button>
            <button onClick={() => run(() => api.resetTurns(code))} disabled={busy || expired}>
              Reset turn budget
            </button>
            <button
              className="danger"
              onClick={() => run(() => api.expireRoom(code))}
              disabled={busy || expired}
            >
              Expire now
            </button>
          </div>

          <h4>Claude Code</h4>
          <p className="hint" style={{ marginTop: 0 }}>
            Connect once, then tell Claude to join:
          </p>
          <div className="snippet">
            claude mcp add --transport http agent-room{" "}
            {config?.mcp_url ?? "http://localhost:8000/mcp"}
          </div>
          <div className="snippet">
            Join room {room.join_code} as Tim-Claude. My objective is to implement the backend.
            Coordinate with the other agent when useful.
          </div>
        </aside>

        {/* ---------------- conversation ---------------- */}
        <section className="conversation">
          <div className="messages" ref={messagesRef}>
            {snapshot.messages.map((message) => (
              <MessageRow key={message.id} message={message} agents={snapshot.agents} />
            ))}
          </div>
          <div className="composer">
            <input
              placeholder={expired ? "Room expired" : "Say something to the room…"}
              value={draft}
              disabled={expired || busy}
              onChange={(e) => setDraft(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && send()}
            />
            <button className="primary" onClick={send} disabled={expired || busy || !draft.trim()}>
              Send
            </button>
          </div>
        </section>

        {/* ---------------- memory ---------------- */}
        <aside className="col">
          <h3>Shared memory</h3>
          <MemoryView data={snapshot.memory.data} />
          {snapshot.memory.updated_by && (
            <p className="hint">Last updated by {snapshot.memory.updated_by}</p>
          )}

          <h4>Tasks</h4>
          {snapshot.tasks.length === 0 ? (
            <p className="mem-empty">No tasks yet.</p>
          ) : (
            snapshot.tasks.map((task) => (
              <TaskRow key={task.id} task={task} agents={snapshot.agents} />
            ))
          )}
        </aside>
      </div>
    </div>
  );
}

function AgentCard({ agent }: { agent: Agent }) {
  const left = agent.status !== "active";
  return (
    <div className="agent">
      <div className="agent-name">
        <span className={`dot ${left ? "left" : ""}`} />
        {agent.agent_name}
      </div>
      <div className="agent-meta">
        {agent.owner_name} · {agent.provider}
        {agent.autonomous ? " · autonomous" : ""}
        {left ? " · left" : ""}
      </div>
      <div className="agent-objective">{agent.public_objective || <em className="muted">no stated objective</em>}</div>
    </div>
  );
}

function MessageRow({ message, agents }: { message: Message; agents: Agent[] }) {
  const recipient = agents.find((a) => a.id === message.recipient_agent_id);
  const time = new Date(message.created_at).toLocaleTimeString([], {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
  return (
    <div className={`msg ${message.message_type}`}>
      <div className="msg-head">
        <span className="msg-sender">{message.sender_label}</span>
        {recipient && <span className="direct-tag">to {recipient.agent_name}</span>}
        <span className="msg-time">{time}</span>
      </div>
      <div className="msg-body">{message.content}</div>
    </div>
  );
}

function MemoryView({ data }: { data: SharedMemoryData }) {
  const sections: [string, string[]][] = [
    ["Decisions", data.decisions],
    ["Facts", data.facts],
    ["Assumptions", data.assumptions],
    ["Open questions", data.open_questions],
    ["Disagreements", data.disagreements],
  ];
  const empty = sections.every(([, items]) => items.length === 0);
  if (empty) {
    return <p className="mem-empty">Nothing recorded yet. Agents write here as they decide things.</p>;
  }
  return (
    <>
      {sections
        .filter(([, items]) => items.length > 0)
        .map(([label, items]) => (
          <div key={label}>
            <h4>{label}</h4>
            <ul className="mem-list">
              {items.map((item) => (
                <li key={item}>{item}</li>
              ))}
            </ul>
          </div>
        ))}
    </>
  );
}

function TaskRow({ task, agents }: { task: Task; agents: Agent[] }) {
  const owner = agents.find((a) => a.id === task.assigned_agent_id);
  return (
    <div className="task">
      <div className={`task-status ${task.status}`}>
        {task.status}
        {owner ? ` · ${owner.agent_name}` : ""}
      </div>
      <div>{task.title}</div>
      {task.result && <div className="muted">{task.result}</div>}
    </div>
  );
}
