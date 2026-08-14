"use client";

import { useState } from "react";
import type { EventEnvelope, Message, Participant } from "../lib/types";
import { participantName } from "../lib/useRoom";

/**
 * Activity + coordination.
 *
 * Reads as an audit feed, not a conversation: `seq`, type, actor, and a one-line
 * summary. Messages appear inline as one event type among many, which is the honest
 * rendering — they annotate coordination rather than being its record.
 *
 * Gaps in `seq` are expected. The server filters events this participant is not
 * authorized to see, so a hole is a privacy boundary doing its job, not lost data.
 */

function summarize(event: EventEnvelope, participants: Participant[]): string {
  const p = event.payload as Record<string, never>;
  const who = (id: unknown) => participantName(participants, id as string | null);

  switch (event.type) {
    case "participant.joined":
      return `${p["display_name"] ?? "someone"} joined as ${p["role"]}`;
    case "participant.left":
      return `${who(p["participant_id"])} left (${p["reason"]})`;
    case "presence.changed":
      return `${who(p["participant_id"])} is now ${p["liveness"]}`;
    case "message.posted":
      return String(p["body"] ?? "");
    case "work.declared":
      return `declared: ${p["headline"]}`;
    case "work.updated":
      return `updated work: ${p["headline"]} (${p["status"]})`;
    case "work.ended":
      return `ended work (${p["reason"]})`;
    case "work.stale":
      return `work went stale (${p["reason"]})`;
    case "task.created":
      return `created task: ${p["title"]}`;
    case "task.updated":
      return `updated task: ${p["title"]}`;
    case "task.claimed":
      return `claimed a task · fence ${p["fence"]} · expires ${String(p["expires_at"]).slice(11, 19)}Z`;
    case "task.claim_renewed":
      return `renewed lease · fence ${p["fence"]}`;
    case "task.claim_released":
      return `released a lease${p["reason"] ? ` (${p["reason"]})` : ""}`;
    case "task.claim_expired":
      return `lease expired · task returned to the pool`;
    case "task.completed":
      return `completed a task${p["result"] ? `: ${p["result"]}` : ""}`;
    case "task.cancelled":
      return `cancelled a task${p["reason"] ? ` (${p["reason"]})` : ""}`;
    case "conflict.detected":
      return `conflict: ${p["detail"]}`;
    case "conflict.resolved":
      return `conflict resolved`;
    case "invitation.created":
      return `invited ${p["target_kind"] === "link" ? "via link" : p["target_value"]} as ${p["role"]}`;
    case "invitation.redeemed":
      return `invitation redeemed`;
    case "room.created":
      return `room created: ${p["name"]}`;
    case "room.closed":
      return `room closed (${p["reason"]})`;
    default:
      return event.type;
  }
}

export function Activity({
  activity,
  messages,
  participants,
  now,
  onPost,
  busy,
}: {
  activity: EventEnvelope[];
  messages: Message[];
  participants: Participant[];
  now: number;
  onPost: (body: string) => Promise<void>;
  busy: boolean;
}) {
  const [draft, setDraft] = useState("");
  const [tab, setTab] = useState<"activity" | "messages">("activity");

  const submit = async () => {
    if (!draft.trim()) return;
    await onPost(draft.trim());
    setDraft("");
  };

  return (
    <div className="region region-activity">
      <h2>
        <button
          className="btn subtle"
          style={{ padding: 0, fontSize: 11, letterSpacing: "0.08em" }}
          onClick={() => setTab(tab === "activity" ? "messages" : "activity")}
        >
          {tab === "activity" ? `Activity · ${activity.length}` : `Messages · ${messages.length}`}
          {" ⇄"}
        </button>
      </h2>

      {tab === "activity" ? (
        <div className="feed">
          {activity.length === 0 && (
            <div className="feed-row">
              <span className="feed-seq">—</span>
              <span className="feed-detail">
                Nothing yet. Every state change in this room appears here, in order.
              </span>
            </div>
          )}
          {activity.map((event) => (
            <div className="feed-row" key={event.id}>
              <span className="feed-seq">{event.seq}</span>
              <span>
                <span className="feed-type">{event.type}</span>{" "}
                <span className="feed-detail">
                  {event.actor.display_name} — {summarize(event, participants)}
                </span>
              </span>
            </div>
          ))}
        </div>
      ) : (
        <div className="stack">
          {messages.length === 0 && (
            <div className="empty">
              No messages. This is an annotation channel — prefer a work declaration or a
              task for anything that represents work.
            </div>
          )}
          {messages
            .slice()
            .sort((a, b) => a.seq - b.seq)
            .map((m) => (
              <div className="message" key={m.id}>
                <div className="message-meta">
                  {participantName(participants, m.participant_id)}
                  {m.to_participant_id && (
                    <> → {participantName(participants, m.to_participant_id)} (direct)</>
                  )}
                  {" · seq "}
                  {m.seq}
                </div>
                <div>{m.body}</div>
              </div>
            ))}
        </div>
      )}

      <div className="card" style={{ marginTop: 8 }}>
        <div className="field">
          <label htmlFor="msg">Say something to the room</label>
          <input
            id="msg"
            value={draft}
            placeholder="Blocked on the schema change — who owns it?"
            onChange={(e) => setDraft(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && void submit()}
          />
        </div>
        <button className="btn" onClick={() => void submit()} disabled={busy}>
          Post
        </button>
      </div>
      <span className="hint">
        Credentials, prompts, and private context are rejected by the server, not
        scrubbed. Share conclusions and references.
      </span>
      <span className="hint" style={{ display: "block", marginTop: 4 }}>
        Gaps in <code>seq</code> are normal — events you are not authorized to see are
        filtered server-side.
      </span>
      <span aria-hidden style={{ display: "none" }}>
        {now}
      </span>
    </div>
  );
}
