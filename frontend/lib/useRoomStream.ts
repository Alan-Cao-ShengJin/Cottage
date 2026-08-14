"use client";

import { useEffect, useRef, useState } from "react";
import { api } from "./api";
import type { Agent, Message, Room, RoomSnapshot, SharedMemory, Task } from "./types";

export type ConnectionState = "connecting" | "live" | "offline";

/**
 * Subscribes to a room's SSE stream. The first frame is a full snapshot, then
 * every change arrives incrementally, so the UI never polls.
 */
export function useRoomStream(code: string) {
  const [snapshot, setSnapshot] = useState<RoomSnapshot | null>(null);
  const [connection, setConnection] = useState<ConnectionState>("connecting");
  const [error, setError] = useState<string | null>(null);
  const sourceRef = useRef<EventSource | null>(null);

  useEffect(() => {
    let cancelled = false;

    // Fetch once so a room that fails to stream still renders something.
    api
      .getRoom(code)
      .then((data) => !cancelled && setSnapshot(data))
      .catch((err) => !cancelled && setError(err.message));

    const source = new EventSource(api.eventsUrl(code));
    sourceRef.current = source;

    const on = <T,>(event: string, handler: (data: T) => void) =>
      source.addEventListener(event, (e) => {
        setConnection("live");
        handler(JSON.parse((e as MessageEvent).data) as T);
      });

    on<RoomSnapshot>("snapshot", (data) => setSnapshot(data));

    on<{ message: Message }>("message", ({ message }) =>
      setSnapshot((prev) =>
        !prev || prev.messages.some((m) => m.id === message.id)
          ? prev
          : { ...prev, messages: [...prev.messages, message] },
      ),
    );

    on<{ room: Room }>("room_updated", (data) =>
      setSnapshot((prev) => (prev ? { ...prev, room: data.room } : prev)),
    );

    on<{ memory: SharedMemory }>("memory_updated", (data) =>
      setSnapshot((prev) => (prev ? { ...prev, memory: data.memory } : prev)),
    );

    const upsertAgent = ({ agent }: { agent: Agent }) =>
      setSnapshot((prev) =>
        prev
          ? {
              ...prev,
              agents: prev.agents.some((a) => a.id === agent.id)
                ? prev.agents.map((a) => (a.id === agent.id ? agent : a))
                : [...prev.agents, agent],
            }
          : prev,
      );
    on<{ agent: Agent }>("agent_joined", upsertAgent);
    on<{ agent: Agent }>("agent_left", upsertAgent);

    on<{ task: Task }>("task_updated", ({ task }) =>
      setSnapshot((prev) =>
        prev
          ? {
              ...prev,
              tasks: prev.tasks.some((t) => t.id === task.id)
                ? prev.tasks.map((t) => (t.id === task.id ? task : t))
                : [...prev.tasks, task],
            }
          : prev,
      ),
    );

    on("room_expired", () =>
      setSnapshot((prev) => (prev ? { ...prev, room: { ...prev.room, status: "expired" } } : prev)),
    );

    source.onopen = () => setConnection("live");
    // EventSource reconnects on its own; surface the gap without tearing down.
    source.onerror = () => setConnection("offline");

    return () => {
      cancelled = true;
      source.close();
      sourceRef.current = null;
    };
  }, [code]);

  return { snapshot, setSnapshot, connection, error };
}

export function useCountdown(expiresAt: string | undefined) {
  const [remaining, setRemaining] = useState(0);

  useEffect(() => {
    if (!expiresAt) return;
    const tick = () =>
      setRemaining(Math.max(0, Math.floor((new Date(expiresAt).getTime() - Date.now()) / 1000)));
    tick();
    const id = setInterval(tick, 1000);
    return () => clearInterval(id);
  }, [expiresAt]);

  return remaining;
}

export function formatDuration(totalSeconds: number): string {
  const h = Math.floor(totalSeconds / 3600);
  const m = Math.floor((totalSeconds % 3600) / 60);
  const s = totalSeconds % 60;
  return [h, m, s].map((n) => String(n).padStart(2, "0")).join(":");
}
