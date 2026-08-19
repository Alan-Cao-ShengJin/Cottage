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
 * The latest thing a participant said it was doing (D-082).
 *
 * Folded from durable `activity.noted` events. There is no mutable activity table;
 * snapshots and realtime both derive the latest-per-runtime value from the log.
 */
export interface LiveActivity {
  ref: string;
  participantId: string;
  attachmentId: string | null;
  phase: string;
  summary: string;
  tool: string | null;
  at: string;
  seq: number;
}

function foldLiveActivity(
  events: EventEnvelope[],
  initial: Record<string, LiveActivity> = {},
): Record<string, LiveActivity> {
  return events.reduce<Record<string, LiveActivity>>((current, event) => {
    if (event.type !== "activity.noted") return current;
    const payload = event.payload;
    const participantId = String(
      payload["participant_id"] ?? event.actor.participant_id ?? "",
    );
    const attachmentId = payload["attachment_id"]
      ? String(payload["attachment_id"])
      : null;
    const ref = attachmentId || participantId;
    if (!ref || (current[ref]?.seq ?? -1) >= event.seq) return current;
    return {
      ...current,
      [ref]: {
        ref,
        participantId,
        attachmentId,
        phase: String(payload["phase"] ?? ""),
        summary: String(payload["summary"] ?? ""),
        tool: (payload["tool"] as string | null) ?? null,
        at: event.ts,
        seq: event.seq,
      },
    };
  }, initial);
}

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
  const [liveActivity, setLiveActivity] = useState<Record<string, LiveActivity>>({});

  const cursor = useRef(0);
  const source = useRef<WebSocket | null>(null);
  const connectionId = useRef<string | null>(null);

  const applyEvent = useCallback((event: EventEnvelope) => {
    if (event.seq <= cursor.current) return;
    cursor.current = Math.max(cursor.current, event.seq);
    setActivity((prev) => [event, ...prev].slice(0, 300));

    if (event.type === "activity.noted") {
      // Keyed by durable attachment when the sender supplied a live connection, so
      // sibling runtimes cannot overwrite each other's narration. Legacy notes fall
      // back to participant grain.
      setLiveActivity((prev) => foldLiveActivity([event], prev));
      // Deliberately no `refresh()`: a note changes no server state, so re-fetching
      // the snapshot per note would turn a cheap narration channel into a fetch
      // storm exactly when an agent is busiest.
      return;
    }

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
                    speaking_for:
                      (payload["speaking_for"] as "agent" | "human" | undefined) ?? "agent",
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
      setSnapshot(fresh);
      setLiveActivity((previous) =>
        foldLiveActivity(fresh.latest_activity ?? [], previous),
      );
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }, [session]);

  useEffect(() => {
    if (!session) return;
    let cancelled = false;
    let heartbeat: ReturnType<typeof setInterval> | null = null;
    let retry: ReturnType<typeof setTimeout> | null = null;
    let attempts = 0;

    const needsRefresh = new Set([
      "room.closed",
      "participant.joined",
      "participant.left",
      "presence.changed",
      "runtime.state_changed",
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
    ]);

    const start = async () => {
      try {
        setState(attempts ? "reconnecting" : "connecting");
        const negotiated = await api.connect(
          session.participantToken,
          session.roomId,
          cursor.current,
        );
        if (cancelled) return;
        connectionId.current = negotiated.connection_id;

        if (heartbeat) clearInterval(heartbeat);
        heartbeat = setInterval(
          () =>
            api
              .heartbeat(session.participantToken, session.roomId, negotiated.connection_id)
              .catch(() => undefined),
          Math.max(5, negotiated.heartbeat_interval_s - 5) * 1000,
        );

        const issued = await api.streamTicket(
          session.participantToken,
          session.roomId,
        );
        if (cancelled) return;
        const ws = new WebSocket(
          api.websocketUrl(
            issued.ticket,
            session.roomId,
            cursor.current,
            negotiated.connection_id,
          ),
        );
        source.current = ws;

        ws.onopen = () => {
          attempts = 0;
          setState("live");
          setError(null);
        };
        ws.onmessage = (raw) => {
          const frame = JSON.parse(raw.data as string) as {
            frame: "snapshot" | "resume_gap" | "keepalive" | "event";
            data?: RoomSnapshot;
            event?: EventEnvelope;
          };
          if (frame.frame === "snapshot" && frame.data) {
            cursor.current = frame.data.snapshot_seq;
            setSnapshot(frame.data);
            setLiveActivity(foldLiveActivity(frame.data.latest_activity ?? []));
            setState("live");
            setError(null);
            return;
          }
          if (frame.frame === "resume_gap") {
            cursor.current = 0;
            setSnapshot(null);
            setActivity([]);
            setLiveActivity({});
            return;
          }
          if (frame.frame === "event" && frame.event) {
            applyEvent(frame.event);
            if (needsRefresh.has(frame.event.type)) void refresh();
          }
        };
        ws.onerror = () => setState("reconnecting");
        ws.onclose = () => {
          if (cancelled) return;
          setState("reconnecting");
          void api
            .disconnect(
              session.participantToken,
              session.roomId,
              negotiated.connection_id,
            )
            .catch(() => undefined);
          attempts += 1;
          retry = setTimeout(() => void start(), Math.min(10_000, 500 * 2 ** attempts));
        };
      } catch (err) {
        if (!cancelled) {
          setError(err instanceof Error ? err.message : String(err));
          setState("reconnecting");
          attempts += 1;
          retry = setTimeout(() => void start(), Math.min(10_000, 500 * 2 ** attempts));
        }
      }
    };

    void start();

    return () => {
      cancelled = true;
      if (heartbeat) clearInterval(heartbeat);
      if (retry) clearTimeout(retry);
      source.current?.close();
      source.current = null;
      const currentConnection = connectionId.current;
      if (currentConnection) {
        void api
          .disconnect(session.participantToken, session.roomId, currentConnection)
          .catch(() => undefined);
      }
    };
  }, [session, applyEvent, refresh]);

  return { snapshot, state, error, activity, liveActivity, refresh };
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
