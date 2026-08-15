export type SurfaceHealth =
  | "stopped"
  | "connecting"
  | "live"
  | "reconnecting"
  | "stale"
  | "error";

export interface ConnectionProfile {
  baseUrl: string;
  roomId: string;
}

export interface RuntimeView {
  is_attachment?: boolean;
  liveness?: string;
  declared?: {
    role?: string;
  };
}

export interface ParticipantView {
  id: string;
  state?: string;
  presence?: {
    liveness?: string;
    runtimes?: RuntimeView[];
  } | null;
}

export interface RoomSnapshot {
  type: "snapshot";
  protocol?: string;
  snapshot_seq: number;
  room?: { id?: string; name?: string; status?: string };
  you?: { participant_id?: string };
  participants?: ParticipantView[];
  open_questions?: unknown[];
  conflicts?: Array<{ status?: string }>;
}

export interface EventEnvelope {
  protocol?: string;
  room_id?: string;
  seq: number;
  id?: string;
  type: string;
  ts?: string;
  actor?: {
    participant_id?: string | null;
    display_name?: string;
  };
  payload: Record<string, unknown>;
}

export interface ConnectResponse {
  connection_id: string;
  negotiated: string[];
  delivery_mode: string;
  heartbeat_interval_s: number;
  current_seq: number;
}

export interface SseFrame {
  event: string;
  id?: number;
  data?: unknown;
}

export interface SurfaceState {
  health: SurfaceHealth;
  cursor: number;
  lastRestContactAt?: number;
  lastStreamContactAt?: number;
  heartbeatIntervalSeconds: number;
  liveWorkers: number;
  newActionable: number;
  participantId?: string;
  roomName?: string;
  error?: string;
}

export interface ActivityRecord {
  seq?: number;
  actionable?: boolean;
  line: string;
}
