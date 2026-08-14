"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { api, type Session } from "./api";
import type {
  Conflict,
  EventEnvelope,
  Participant,
  RoomSnapshot,
  Task,
  WorkDeclaration,
} from "./types";

export type StreamState = "connecting" | "live" | "reconnecting" | "closed";

/**
 * Subscribes to a room's event stream and folds events into local state.
 *
 * The important behavior is the cursor. The stream opens with a snapshot carrying
 * `snapshot_seq`; every event after it advances a `cursor` ref. On reconnect we resume
 * from that cursor, so nothing is missed and nothing is replayed. A `resume_gap` frame
 * means history we needed was truncated — the only correct response is to drop local
 * state and re-snapshot, never to carry on from a partial view.
 *
 * Note that gaps in `seq` are expected and are *not* loss: the server filters events
 * this participant is not authorized to see, so a recipient legitimately sees holes.
 */
export function useRoom(session: Session | null) {
  const [snapshot, setSnapshot] = useState<RoomSnapshot | null>(null);
  const [state, setState] = useState<StreamState>("connecting");
  const [error, setError] = useState<string | null>(null);
  const [activity, setActivity] = useState<EventEnvelope[]>([]);

  const cursor = useRef(0);
  const source = useRef<EventSource | null>(null);
  const connectionId = useRef<string | null>(null);

  const applyEvent = useCallback((event: EventEnvelope) => {
    cursor.current = Math.max(cursor.current, event.seq);
    setActivity((prev) => [event, ...prev].slice(0, 300));

    setSnapshot((prev) => {
      if (!prev) return prev;
      const payload = event.payload as Record<string, never>;

      switch (event.type) {
        case "work.declared":
        case "work.updated":
        case "work.ended":
        case "work.stale":
        case "task.created":
        case "task.updated":
        case "task.claimed":
        case "task.claim_renewed":
        case "task.claim_released":
        case "task.claim_expired":
        case "task.completed":
        case "task.cancelled":
        case "conflict.detected":
        case "conflict.resolved":
        case "participant.joined":
        case "participant.left":
        case "presence.changed":
          // These change derived, cross-referenced state (lease countdowns, presence
          // grades, staleness) that is cheaper and safer to re-read than to
          // reconstruct client-side. Re-snapshot rather than risk divergence.
          void payload;
          return prev;
        case "message.posted":
          return {
            ...prev,
            messages: prev.messages.some((m) => m.id === payload["message_id"])
              ? prev.messages
              : [
                  ...prev.messages,
                  {
                    id: String(payload["message_id"]),
                    seq: event.seq,
                    participant_id: event.actor.participant_id,
                    body: String(payload["body"] ?? ""),
                    about_ref: (payload["about_ref"] as string | null) ?? null,
                    privacy_class: event.privacy_class,
                    to_participant_id:
                      (payload["to_participant_id"] as string | null) ?? null,
                    created_at: event.ts,
                  },
                ],
          };
        default:
          return prev;
      }
    });
  }, []);

  const refresh = useCallback(async () => {
    if (!session) return;
    try {
      const fresh = await api.snapshot(session.participantToken, session.roomId);
      cursor.current = fresh.snapshot_seq;
      setSnapshot(fresh);
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }, [session]);

  useEffect(() => {
    if (!session) return;
    let cancelled = false;
    let heartbeat: ReturnType<typeof setInterval> | null = null;

    const start = async () => {
      try {
        const negotiated = await api.connect(
          session.participantToken,
          session.roomId,
          cursor.current,
        );
        if (cancelled) return;
        connectionId.current = negotiated.connection_id;

        // Keep presence honest while the tab is open. The stream itself also
        // heartbeats server-side, but an explicit beat covers a stalled stream.
        heartbeat = setInterval(
          () =>
            api
              .heartbeat(session.participantToken, session.roomId, negotiated.connection_id)
              .catch(() => undefined),
          Math.max(5, negotiated.heartbeat_interval_s - 5) * 1000,
        );

        const es = new EventSource(
          api.streamUrl(
            session.participantToken,
            session.roomId,
            cursor.current,
            negotiated.connection_id,
          ),
        );
        source.current = es;

        es.addEventListener("snapshot", (raw) => {
          const frame = JSON.parse((raw as MessageEvent).data) as RoomSnapshot;
          cursor.current = frame.snapshot_seq;
          setSnapshot(frame);
          setState("live");
          setError(null);
        });

        es.addEventListener("resume_gap", () => {
          // History we needed is gone. Discard local state and re-snapshot; carrying
          // on from a partial view would mean coordinating on stale state.
          cursor.current = 0;
          setSnapshot(null);
          setActivity([]);
          void refresh();
        });

        const eventTypes = [
          "room.closed",
          "participant.joined",
          "participant.left",
          "presence.changed",
          "message.posted",
          "work.declared",
          "work.updated",
          "work.ended",
          "work.stale",
          "task.created",
          "task.updated",
          "task.claimed",
          "task.claim_renewed",
          "task.claim_released",
          "task.claim_expired",
          "task.completed",
          "task.cancelled",
          "conflict.detected",
          "conflict.resolved",
        ];
        const needsRefresh = new Set(
          eventTypes.filter((t) => t !== "message.posted"),
        );

        for (const type of eventTypes) {
          es.addEventListener(type, (raw) => {
            setState("live");
            const event = JSON.parse((raw as MessageEvent).data) as EventEnvelope;
            applyEvent(event);
            if (needsRefresh.has(type)) void refresh();
          });
        }

        es.onopen = () => setState("live");
        // EventSource reconnects by itself; surface the gap without tearing down, and
        // let it resume from our cursor when it comes back.
        es.onerror = () => setState("reconnecting");
      } catch (err) {
        if (!cancelled) {
          setError(err instanceof Error ? err.message : String(err));
          setState("closed");
        }
      }
    };

    void refresh().then(start);

    return () => {
      cancelled = true;
      if (heartbeat) clearInterval(heartbeat);
      source.current?.close();
      source.current = null;
    };
  }, [session, applyEvent, refresh]);

  return { snapshot, state, error, activity, refresh };
}

/** Ticking clock, so lease countdowns and work ages update without a re-fetch. */
export function useNow(intervalMs = 1000): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), intervalMs);
    return () => clearInterval(id);
  }, [intervalMs]);
  return now;
}

export function secondsUntil(iso: string | null | undefined, now: number): number {
  if (!iso) return 0;
  return Math.round((new Date(iso).getTime() - now) / 1000);
}

export function formatDuration(seconds: number): string {
  const abs = Math.abs(seconds);
  const h = Math.floor(abs / 3600);
  const m = Math.floor((abs % 3600) / 60);
  const s = abs % 60;
  const sign = seconds < 0 ? "-" : "";
  return h > 0
    ? `${sign}${h}h ${String(m).padStart(2, "0")}m`
    : `${sign}${m}m ${String(s).padStart(2, "0")}s`;
}

export function formatAge(iso: string, now: number): string {
  return formatDuration(Math.round((now - new Date(iso).getTime()) / 1000));
}

export function participantName(
  participants: Participant[],
  id: string | null | undefined,
): string {
  if (!id) return "the room";
  return (
    participants.find((p) => p.id === id)?.identity.display_name ?? "someone who left"
  );
}

export function openWork(work: WorkDeclaration[]): WorkDeclaration[] {
  return work.filter((w) => !w.ended_at);
}

export function boardColumns(tasks: Task[]): { label: string; tasks: Task[] }[] {
  const by = (statuses: Task["status"][]) =>
    tasks.filter((t) => statuses.includes(t.status));
  return [
    { label: "Proposed", tasks: by(["proposed"]) },
    { label: "Open", tasks: by(["open"]) },
    { label: "In flight", tasks: by(["claimed", "in_progress"]) },
    { label: "Blocked", tasks: by(["blocked"]) },
    { label: "Done", tasks: by(["done"]) },
  ];
}

export function openConflicts(conflicts: Conflict[]): Conflict[] {
  return conflicts.filter((c) => c.status === "open");
}
