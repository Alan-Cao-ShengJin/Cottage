import {
  ConnectResponse,
  ConnectionProfile,
  EventEnvelope,
  RoomSnapshot,
  SseFrame,
  SurfaceHealth,
} from "./types";

const CAPABILITIES = ["can_receive_events", "supports_push", "supports_resume"];
const REQUIRED_CAPABILITIES = new Set(CAPABILITIES);
const PROTOCOL_MAJOR = 1;

export class SseParser {
  private buffer = "";
  private event = "message";
  private id: number | undefined;
  private data: string[] = [];

  push(chunk: string): SseFrame[] {
    this.buffer += chunk;
    const frames: SseFrame[] = [];
    let newline = this.buffer.indexOf("\n");
    while (newline >= 0) {
      let line = this.buffer.slice(0, newline);
      this.buffer = this.buffer.slice(newline + 1);
      if (line.endsWith("\r")) line = line.slice(0, -1);
      const frame = this.line(line);
      if (frame) frames.push(frame);
      newline = this.buffer.indexOf("\n");
    }
    return frames;
  }

  finish(): SseFrame[] {
    const frames: SseFrame[] = [];
    if (this.buffer) {
      const frame = this.line(this.buffer.replace(/\r$/, ""));
      if (frame) frames.push(frame);
      this.buffer = "";
    }
    const final = this.dispatch();
    if (final) frames.push(final);
    return frames;
  }

  private line(line: string): SseFrame | undefined {
    if (line === "") return this.dispatch();
    if (line.startsWith(":")) return { event: "keepalive" };
    const colon = line.indexOf(":");
    const field = colon < 0 ? line : line.slice(0, colon);
    let value = colon < 0 ? "" : line.slice(colon + 1);
    if (value.startsWith(" ")) value = value.slice(1);
    if (field === "event") this.event = value;
    if (field === "id" && /^\d+$/.test(value)) this.id = Number(value);
    if (field === "data") this.data.push(value);
    return undefined;
  }

  private dispatch(): SseFrame | undefined {
    if (this.data.length === 0) {
      this.event = "message";
      this.id = undefined;
      return undefined;
    }
    const raw = this.data.join("\n");
    const frame: SseFrame = { event: this.event };
    if (this.id !== undefined) frame.id = this.id;
    try {
      frame.data = JSON.parse(raw) as unknown;
    } catch {
      frame.data = raw;
    }
    this.event = "message";
    this.id = undefined;
    this.data = [];
    return frame;
  }
}

export function reconnectDelay(attempt: number): number {
  return Math.min(30_000, 1_000 * 2 ** Math.min(Math.max(attempt, 0), 5));
}

interface ClientCallbacks {
  onHealth(health: SurfaceHealth, error?: string): void;
  onRestContact(): void;
  onStreamContact(): void;
  onConnected(connection: ConnectResponse): void;
  onSnapshot(snapshot: RoomSnapshot): Promise<void>;
  onEvent(event: EventEnvelope): Promise<void>;
  onResumeGap(detail: unknown): Promise<void>;
  onPoisonFrame(reason: string): Promise<void>;
  persistCursor(cursor: number): Promise<void>;
}

export interface ArpClientRuntime {
  fetch(input: string, init?: RequestInit): Promise<Response>;
  wait(milliseconds: number, signal: AbortSignal): Promise<void>;
}

const DEFAULT_RUNTIME: ArpClientRuntime = {
  fetch: (input, init) => fetch(input, init),
  wait: delay,
};

class HttpStatusError extends Error {
  constructor(readonly status: number, surface: string) {
    super(`${surface} returned HTTP ${status}.`);
  }
}

class ProtocolFrameError extends Error {}

export class ArpClient {
  private abort?: AbortController;
  private loop?: Promise<void>;
  private connectionId?: string;
  private running = false;
  private cursor = 0;
  private refreshTimer?: ReturnType<typeof setTimeout>;
  private awaitingGapSnapshot = false;

  constructor(
    private readonly profile: ConnectionProfile,
    private readonly token: string,
    cursor: number,
    private readonly callbacks: ClientCallbacks,
    private readonly runtime: ArpClientRuntime = DEFAULT_RUNTIME,
  ) {
    this.cursor = cursor;
  }

  start(): void {
    if (this.running) return;
    this.running = true;
    this.loop = this.run();
  }

  async whenStopped(): Promise<void> {
    await this.loop?.catch(() => undefined);
  }

  async stop(notifyServer = true): Promise<void> {
    this.running = false;
    this.abort?.abort();
    if (this.refreshTimer) clearTimeout(this.refreshTimer);
    if (notifyServer) await this.closeConnection();
    await this.loop?.catch(() => undefined);
    this.callbacks.onHealth("stopped");
  }

  private async run(): Promise<void> {
    let attempt = 0;
    while (this.running) {
      let healthy = false;
      this.abort = new AbortController();
      this.callbacks.onHealth(attempt === 0 ? "connecting" : "reconnecting");
      try {
        const snapshot = await this.requestSnapshot(this.abort.signal);
        await this.callbacks.onSnapshot(snapshot);
        if (this.cursor === 0) await this.advanceCursor(snapshot.snapshot_seq);

        const connectedCandidate = await this.request<unknown>("/connect", {
          method: "POST",
          signal: this.abort.signal,
          body: JSON.stringify({
            host_class: "interactive_client",
            capabilities: CAPABILITIES,
            since_seq: this.cursor,
            transport: "sse",
            runtime_role: "control_surface",
            executor_kind: "none",
          }),
        });
        if (!isConnectResponse(connectedCandidate, this.cursor)) {
          await this.callbacks.onPoisonFrame("Invalid connect response.");
          throw new ProtocolFrameError("Cottage returned an invalid connect response.");
        }
        const connected = connectedCandidate;
        this.connectionId = connected.connection_id;
        this.callbacks.onConnected(connected);
        this.awaitingGapSnapshot = false;
        await this.stream(this.abort.signal, () => {
          healthy = true;
        });
        if (this.running) throw new Error("The room stream ended.");
      } catch (error) {
        if (!this.running) break;
        const message = safeError(error);
        if (isTerminalHttpError(error)) {
          this.running = false;
          this.callbacks.onHealth("error", message);
          await this.closeConnection();
          break;
        }
        this.callbacks.onHealth("reconnecting", message);
        await this.closeConnection();
        if (!this.running) break;
        if (healthy) attempt = 0;
        await this.runtime.wait(reconnectDelay(attempt++), this.abort.signal).catch(() => undefined);
      }
    }
  }

  private async stream(signal: AbortSignal, markHealthy: () => void): Promise<void> {
    const query = new URLSearchParams({ since_seq: String(this.cursor) });
    if (this.connectionId) query.set("connection_id", this.connectionId);
    const response = await this.runtime.fetch(`${this.roomUrl()}/stream?${query.toString()}`, {
      headers: { Authorization: `Bearer ${this.token}`, Accept: "text/event-stream" },
      signal,
    });
    if (!response.ok || !response.body) {
      throw new HttpStatusError(response.status, "Room stream");
    }
    if (!hasContentType(response, "text/event-stream")) {
      throw new ProtocolFrameError("Cottage stream did not return text/event-stream.");
    }
    this.callbacks.onStreamContact();
    this.callbacks.onHealth("live");

    const parser = new SseParser();
    const decoder = new TextDecoder();
    const reader = response.body.getReader();
    while (this.running) {
      const { done, value } = await reader.read();
      if (done) break;
      const frames = parser.push(decoder.decode(value, { stream: true }));
      for (const frame of frames) {
        await this.handleFrame(frame);
        markHealthy();
        if (!this.running) break;
      }
    }
    if (!this.running) return;
    for (const frame of [...parser.push(decoder.decode()), ...parser.finish()]) {
      await this.handleFrame(frame);
      markHealthy();
      if (!this.running) break;
    }
  }

  private async handleFrame(frame: SseFrame): Promise<void> {
    this.callbacks.onStreamContact();
    if (frame.event === "keepalive") return;
    if (frame.event === "resume_gap") {
      if (
        frame.id !== undefined ||
        this.awaitingGapSnapshot ||
        !isControlFrame(frame.data, "resume_gap")
      ) {
        await this.recoverPoison("Invalid or repeated resume_gap without the required snapshot.");
      }
      this.awaitingGapSnapshot = true;
      await this.callbacks.onResumeGap(frame.data);
      return;
    }
    if (frame.event === "snapshot") {
      if (!this.awaitingGapSnapshot && this.cursor !== 0) {
        await this.recoverPoison("Unexpected snapshot without a preceding resume_gap.");
      }
      const candidate = frame.data;
      if (
        !isSnapshot(candidate, this.profile.roomId) ||
        frame.id === undefined ||
        frame.id !== candidate.snapshot_seq ||
        candidate.snapshot_seq < this.cursor
      ) {
        await this.recoverPoison("Invalid snapshot frame or cursor.");
      }
      const snapshot = candidate as RoomSnapshot;
      this.awaitingGapSnapshot = false;
      await this.callbacks.onSnapshot(snapshot);
    } else {
      if (this.awaitingGapSnapshot) {
        await this.recoverPoison("resume_gap was not followed by a snapshot.");
      }
      const candidate = frame.data;
      if (
        !isEvent(candidate, this.profile.roomId) ||
        frame.id === undefined ||
        frame.id !== candidate.seq ||
        frame.event !== candidate.type
      ) {
        await this.recoverPoison("Invalid event frame or mismatched SSE/event cursor.");
      }
      const event = candidate as EventEnvelope;
      if (event.seq <= this.cursor) return;
      await this.callbacks.onEvent(event);
      if (frame.id !== undefined) await this.advanceCursor(frame.id);
      if (
        event.type === "presence.changed" ||
        event.type === "presence.attachment_registered" ||
        event.type === "participant.joined" ||
        event.type === "participant.left"
      ) {
        this.scheduleSnapshotRefresh();
      }
      if (event.type === "room.closed") {
        this.running = false;
        this.abort?.abort();
        await this.closeConnection();
        this.callbacks.onHealth("stopped");
      }
      return;
    }
    if (frame.id !== undefined) await this.advanceCursor(frame.id);
  }

  private async recoverPoison(reason: string): Promise<never> {
    await this.callbacks.onPoisonFrame(reason);
    try {
      const snapshot = await this.requestSnapshot(this.abort?.signal);
      if (snapshot.snapshot_seq < this.cursor) {
        throw new ProtocolFrameError("Replacement snapshot is older than the processed cursor.");
      }
      await this.callbacks.onSnapshot(snapshot);
      await this.advanceCursor(snapshot.snapshot_seq);
    } finally {
      this.abort?.abort();
    }
    throw new Error("A malformed room frame was replaced by a fresh snapshot.");
  }

  private scheduleSnapshotRefresh(): void {
    if (this.refreshTimer) clearTimeout(this.refreshTimer);
    this.refreshTimer = setTimeout(() => {
      this.refreshTimer = undefined;
      if (!this.running) return;
      void this.requestSnapshot(this.abort?.signal)
        .then((snapshot) => {
          if (snapshot.snapshot_seq < this.cursor) return;
          return this.callbacks.onSnapshot(snapshot);
        })
        .catch(async (error) => {
          if (error instanceof ProtocolFrameError) {
            await this.callbacks.onPoisonFrame(error.message);
          }
        })
        .catch(() => undefined);
    }, 250);
  }

  private async advanceCursor(cursor: number): Promise<void> {
    if (cursor <= this.cursor) return;
    await this.callbacks.persistCursor(cursor);
    this.cursor = cursor;
  }

  private async closeConnection(): Promise<void> {
    const connectionId = this.connectionId;
    this.connectionId = undefined;
    if (!connectionId) return;
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 3_000);
    try {
      const query = new URLSearchParams({ connection_id: connectionId });
      await this.request(`/disconnect?${query.toString()}`, {
        method: "POST",
        signal: controller.signal,
      });
    } catch {
      // A stale transport is reaped by Cottage. Never put credentials or request data in logs.
    } finally {
      clearTimeout(timeout);
    }
  }

  private async request<T>(path: string, init: RequestInit = {}): Promise<T> {
    const response = await this.runtime.fetch(`${this.roomUrl()}${path}`, {
      ...init,
      headers: {
        Authorization: `Bearer ${this.token}`,
        "Content-Type": "application/json",
        ...(init.headers ?? {}),
      },
    });
    if (!response.ok) throw new HttpStatusError(response.status, "Cottage");
    if (!hasJsonContentType(response)) {
      throw new ProtocolFrameError("Cottage REST response did not return JSON.");
    }
    const value = (await response.json()) as T;
    this.callbacks.onRestContact();
    return value;
  }

  private async requestSnapshot(signal?: AbortSignal): Promise<RoomSnapshot> {
    const candidate = await this.request<unknown>("/snapshot", { signal });
    if (!isSnapshot(candidate, this.profile.roomId)) {
      throw new ProtocolFrameError("Cottage returned an invalid room snapshot.");
    }
    return candidate;
  }

  private roomUrl(): string {
    return `${this.profile.baseUrl}/api/rooms/${encodeURIComponent(this.profile.roomId)}`;
  }
}

function isSnapshot(value: unknown, roomId: string): value is RoomSnapshot {
  if (!value || typeof value !== "object") return false;
  const candidate = value as Partial<RoomSnapshot>;
  return (
    candidate.type === "snapshot" &&
    hasProtocolMajor(candidate.protocol) &&
    candidate.room?.id === roomId &&
    typeof candidate.snapshot_seq === "number" &&
    Number.isSafeInteger(candidate.snapshot_seq) &&
    candidate.snapshot_seq >= 0
  );
}

function isEvent(value: unknown, roomId: string): value is EventEnvelope {
  if (!value || typeof value !== "object") return false;
  const candidate = value as Partial<EventEnvelope>;
  return (
    typeof candidate.seq === "number" &&
    Number.isSafeInteger(candidate.seq) &&
    candidate.seq >= 0 &&
    hasProtocolMajor(candidate.protocol) &&
    candidate.room_id === roomId &&
    typeof candidate.type === "string" &&
    candidate.payload !== null &&
    typeof candidate.payload === "object" &&
    !Array.isArray(candidate.payload)
  );
}

function isConnectResponse(value: unknown, cursor: number): value is ConnectResponse {
  if (!value || typeof value !== "object") return false;
  const candidate = value as Partial<ConnectResponse>;
  return (
    typeof candidate.connection_id === "string" &&
    candidate.connection_id.length > 0 &&
    Array.isArray(candidate.negotiated) &&
    candidate.negotiated.every((capability) => typeof capability === "string") &&
    [...REQUIRED_CAPABILITIES].every((capability) => candidate.negotiated?.includes(capability)) &&
    candidate.delivery_mode === "push" &&
    typeof candidate.heartbeat_interval_s === "number" &&
    Number.isFinite(candidate.heartbeat_interval_s) &&
    candidate.heartbeat_interval_s > 0 &&
    typeof candidate.current_seq === "number" &&
    Number.isSafeInteger(candidate.current_seq) &&
    candidate.current_seq >= cursor
  );
}

function hasProtocolMajor(protocol: unknown): boolean {
  if (typeof protocol !== "string") return false;
  const match = /^arp\/(\d+)(?:\.|$)/.exec(protocol);
  return match !== null && Number(match[1]) === PROTOCOL_MAJOR;
}

function isControlFrame(value: unknown, type: string): boolean {
  if (!value || typeof value !== "object") return false;
  const candidate = value as { type?: unknown; error?: unknown };
  return (candidate.type ?? candidate.error) === type;
}

function hasContentType(response: Response, expected: string): boolean {
  return response.headers.get("content-type")?.split(";", 1)[0].trim().toLowerCase() === expected;
}

function hasJsonContentType(response: Response): boolean {
  const contentType = response.headers.get("content-type")?.split(";", 1)[0].trim().toLowerCase();
  return contentType === "application/json" || contentType?.endsWith("+json") === true;
}

function isTerminalHttpError(error: unknown): error is HttpStatusError {
  return error instanceof HttpStatusError && [401, 403, 404, 410].includes(error.status);
}

function safeError(error: unknown): string {
  if (error instanceof Error && error.name === "AbortError") return "Connection cancelled.";
  if (error instanceof Error) return error.message.slice(0, 200);
  return "Unknown connection error.";
}

function delay(milliseconds: number, signal: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    if (signal.aborted) {
      reject(new Error("Connection cancelled."));
      return;
    }
    const timer = setTimeout(done, milliseconds);
    signal.addEventListener("abort", aborted, { once: true });
    function done(): void {
      signal.removeEventListener("abort", aborted);
      resolve();
    }
    function aborted(): void {
      clearTimeout(timer);
      reject(new Error("Connection cancelled."));
    }
  });
}
