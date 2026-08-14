/**
 * ARP HTTP client.
 *
 * Two token kinds, deliberately not interchangeable:
 *   - a **principal token** (a user) creates rooms and redeems invitations;
 *   - a **participant token** is scoped to one room and does everything inside it.
 * Keeping them separate in the client mirrors the server, so a room-scoped token is
 * never accidentally sent to an org-level endpoint.
 */

import type { Capability, EventEnvelope, Room, RoomSnapshot } from "./types";

/**
 * Where the API lives, baked in at build time.
 *
 * An empty string means "the origin serving this page", which is what the container build
 * passes: the backend serves both the API and this console, so relative paths are correct
 * and no CORS entry is involved. A `npm run dev` build has no such origin and falls back to
 * the local backend on :8000.
 */
export const API_BASE = process.env.NEXT_PUBLIC_API_BASE ?? "http://localhost:8000";

export class ArpError extends Error {
  /** Stable protocol code, e.g. `lease_conflict`. Branch on this, not the message. */
  readonly code: string;
  readonly details: Record<string, unknown>;

  constructor(code: string, message: string, details: Record<string, unknown> = {}) {
    super(message);
    this.code = code;
    this.details = details;
  }
}

async function request<T>(
  path: string,
  init: RequestInit & { token?: string } = {},
): Promise<T> {
  const { token, headers, ...rest } = init;
  const response = await fetch(`${API_BASE}${path}`, {
    ...rest,
    headers: {
      "Content-Type": "application/json",
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
      ...headers,
    },
  });

  const text = await response.text();
  const body = text ? JSON.parse(text) : {};

  if (!response.ok) {
    throw new ArpError(
      body.error ?? "request_failed",
      body.message ?? `${response.status} ${response.statusText}`,
      body.details ?? {},
    );
  }
  return body as T;
}

/** What a browser participant honestly supports: pushable, resumable, human-driven. */
export const BROWSER_CAPABILITIES: Capability[] = [
  "can_receive_events",
  "supports_push",
  "supports_resume",
  "requires_human_presence",
];

export const api = {
  capabilities: () =>
    request<{
      capabilities: Capability[];
      transports: Record<string, string[]>;
      host_class_defaults: Record<string, string[]>;
      note: string;
      mcp_url: string;
    }>("/api/capabilities"),

  listRooms: (token: string) =>
    request<{ rooms: Room[] }>("/api/rooms", { token }),

  /**
   * Creates the room, joins you as owner, and mints a shareable join token — one call.
   * `join_token` is the only thing you hand to anyone else.
   */
  createRoom: (
    token: string,
    input: { name: string; purpose?: string; visibility?: "internal" | "cross_org" },
  ) =>
    request<{
      room: Room;
      participant: { id: string; room_id: string };
      participant_token: string;
      join_token: string;
      mcp_url: string;
    }>("/api/rooms", {
      method: "POST",
      token,
      body: JSON.stringify(input),
    }),

  createInvitation: (
    participantToken: string,
    roomId: string,
    input: { role?: string; max_redemptions?: number; ttl_seconds?: number } = {},
  ) =>
    request<{ invitation: { id: string }; token: string }>(
      `/api/rooms/${roomId}/invitations`,
      { method: "POST", token: participantToken, body: JSON.stringify(input) },
    ),

  join: (
    token: string,
    input: {
      invitation_token: string;
      display_name: string;
      kind?: "human" | "agent";
      host_class?: string;
      capabilities?: Capability[];
    },
  ) =>
    request<{
      participant: { id: string; room_id: string };
      room: Room;
      participant_token: string;
    }>("/api/rooms/join", {
      method: "POST",
      token,
      body: JSON.stringify({
        kind: "human",
        host_class: "browser_human",
        capabilities: BROWSER_CAPABILITIES,
        ...input,
      }),
    }),

  connect: (participantToken: string, roomId: string, sinceSeq = 0) =>
    request<{
      connection_id: string;
      negotiated: Capability[];
      delivery_mode: string;
      heartbeat_interval_s: number;
      may_claim: boolean;
      claim_denied_reason: string | null;
      max_lease_seconds: number;
      lease_renewable_unattended: boolean;
      current_seq: number;
    }>(`/api/rooms/${roomId}/connect`, {
      method: "POST",
      token: participantToken,
      body: JSON.stringify({
        host_class: "browser_human",
        capabilities: BROWSER_CAPABILITIES,
        since_seq: sinceSeq,
      }),
    }),

  heartbeat: (participantToken: string, roomId: string, connectionId: string) =>
    request<{ ok: true }>(`/api/rooms/${roomId}/heartbeat`, {
      method: "POST",
      token: participantToken,
      body: JSON.stringify({ connection_id: connectionId }),
    }),

  snapshot: (participantToken: string, roomId: string) =>
    request<RoomSnapshot>(`/api/rooms/${roomId}/snapshot`, { token: participantToken }),

  events: (participantToken: string, roomId: string, sinceSeq: number) =>
    request<{ events: EventEnvelope[]; cursor: number }>(
      `/api/rooms/${roomId}/events?since_seq=${sinceSeq}`,
      { token: participantToken },
    ),

  declareWork: (
    participantToken: string,
    roomId: string,
    input: { headline: string; targets: string[]; note?: string },
  ) =>
    request<{ work: unknown }>(`/api/rooms/${roomId}/work`, {
      method: "POST",
      token: participantToken,
      body: JSON.stringify(input),
    }),

  updateWork: (
    participantToken: string,
    roomId: string,
    input: { work_id: string; status?: string; headline?: string; note?: string },
  ) =>
    request<{ work: unknown }>(`/api/rooms/${roomId}/work`, {
      method: "PATCH",
      token: participantToken,
      body: JSON.stringify(input),
    }),

  endWork: (participantToken: string, roomId: string, workId: string) =>
    request<{ work: unknown }>(`/api/rooms/${roomId}/work/end`, {
      method: "POST",
      token: participantToken,
      body: JSON.stringify({ work_id: workId }),
    }),

  createTask: (
    participantToken: string,
    roomId: string,
    input: { title: string; description?: string; targets: string[]; priority?: number },
  ) =>
    request<{ task: unknown }>(`/api/rooms/${roomId}/tasks`, {
      method: "POST",
      token: participantToken,
      body: JSON.stringify(input),
    }),

  claimTask: (participantToken: string, roomId: string, taskId: string) =>
    request<{ task: unknown }>(`/api/rooms/${roomId}/tasks/claim`, {
      method: "POST",
      token: participantToken,
      body: JSON.stringify({ task_id: taskId }),
    }),

  renewClaim: (participantToken: string, roomId: string, taskId: string, fence: number) =>
    request<{ task: unknown }>(`/api/rooms/${roomId}/tasks/renew`, {
      method: "POST",
      token: participantToken,
      body: JSON.stringify({ task_id: taskId, fence }),
    }),

  releaseClaim: (participantToken: string, roomId: string, taskId: string, fence: number) =>
    request<{ task: unknown }>(`/api/rooms/${roomId}/tasks/release`, {
      method: "POST",
      token: participantToken,
      body: JSON.stringify({ task_id: taskId, fence }),
    }),

  completeTask: (
    participantToken: string,
    roomId: string,
    taskId: string,
    fence: number,
    result: string,
  ) =>
    request<{ task: unknown }>(`/api/rooms/${roomId}/tasks/complete`, {
      method: "POST",
      token: participantToken,
      body: JSON.stringify({ task_id: taskId, fence, result }),
    }),

  postMessage: (
    participantToken: string,
    roomId: string,
    input: { body: string; about_ref?: string | null },
  ) =>
    request<{ message_id: string }>(`/api/rooms/${roomId}/messages`, {
      method: "POST",
      token: participantToken,
      body: JSON.stringify(input),
    }),

  leave: (participantToken: string, roomId: string) =>
    request<{ ok: true }>(`/api/rooms/${roomId}/leave`, {
      method: "POST",
      token: participantToken,
      body: JSON.stringify({ note: "" }),
    }),

  /**
   * SSE URL. `EventSource` cannot set an Authorization header, so the participant
   * token rides as a query parameter on this endpoint only. It is room-scoped and
   * revoked on leave, which bounds the exposure — but it does land in server logs, so
   * a cookie-based stream session is on the M5 list.
   */
  streamUrl: (participantToken: string, roomId: string, sinceSeq: number, connectionId?: string) => {
    const params = new URLSearchParams({
      since_seq: String(sinceSeq),
      token: participantToken,
    });
    if (connectionId) params.set("connection_id", connectionId);
    return `${API_BASE}/api/rooms/${roomId}/stream?${params.toString()}`;
  },
};

/** Local session: which participant this browser is, per room. */
export interface Session {
  principalToken: string;
  participantToken: string;
  participantId: string;
  roomId: string;
  displayName: string;
}

const SESSION_KEY = "agent-rooms.session";

export function saveSession(session: Session): void {
  window.localStorage.setItem(SESSION_KEY, JSON.stringify(session));
}

export function loadSession(): Session | null {
  if (typeof window === "undefined") return null;
  const raw = window.localStorage.getItem(SESSION_KEY);
  return raw ? (JSON.parse(raw) as Session) : null;
}

export function clearSession(): void {
  window.localStorage.removeItem(SESSION_KEY);
}
