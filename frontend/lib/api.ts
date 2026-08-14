import type { Agent, Message, Room, RoomSnapshot, ServerConfig } from "./types";

export const API_BASE =
  process.env.NEXT_PUBLIC_API_BASE?.replace(/\/$/, "") ?? "http://localhost:8000";

export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
    readonly code: string,
  ) {
    super(message);
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let response: Response;
  try {
    response = await fetch(`${API_BASE}${path}`, {
      ...init,
      headers: { "Content-Type": "application/json", ...(init?.headers ?? {}) },
    });
  } catch {
    throw new ApiError(
      `Cannot reach the backend at ${API_BASE}. Is it running?`,
      0,
      "network_error",
    );
  }

  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw new ApiError(
      body.message ?? body.detail ?? `Request failed (${response.status})`,
      response.status,
      body.error ?? "http_error",
    );
  }
  return (await response.json()) as T;
}

export const api = {
  config: () => request<ServerConfig>("/api/config"),

  createRoom: (title: string, objective: string, ttlSeconds?: number) =>
    request<Room>("/api/rooms", {
      method: "POST",
      body: JSON.stringify({ title, objective, ttl_seconds: ttlSeconds ?? null }),
    }),

  getRoom: (code: string) => request<RoomSnapshot>(`/api/rooms/${code}`),

  spawnGptAgent: (
    code: string,
    body: {
      owner_name: string;
      agent_name: string;
      public_objective: string;
      private_instructions: string;
    },
  ) => request<Agent>(`/api/rooms/${code}/gpt-agent`, { method: "POST", body: JSON.stringify(body) }),

  postHumanMessage: (code: string, content: string, senderLabel: string) =>
    request<Message>(`/api/rooms/${code}/messages`, {
      method: "POST",
      body: JSON.stringify({ content, sender_label: senderLabel }),
    }),

  setAutonomy: (code: string, enabled: boolean) =>
    request<Room>(`/api/rooms/${code}/autonomy`, {
      method: "POST",
      body: JSON.stringify({ enabled }),
    }),

  resetTurns: (code: string) => request<Room>(`/api/rooms/${code}/reset-turns`, { method: "POST" }),

  expireRoom: (code: string) => request<Room>(`/api/rooms/${code}/expire`, { method: "POST" }),

  eventsUrl: (code: string) => `${API_BASE}/api/rooms/${code}/events`,
};
