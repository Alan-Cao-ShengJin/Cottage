import {
  ActivityRecord,
  ConnectionProfile,
  EventEnvelope,
  RoomSnapshot,
  SurfaceHealth,
  SurfaceState,
} from "./types";

const LIVE = new Set(["live_push", "live_poll", "attended"]);

export const ACTIONABLE_EVENTS = new Set([
  "directive.issued",
  "question.asked",
  "conflict.detected",
  "artifact.divergence_detected",
  "task.proposed",
  "task.awaiting_input",
  "task.blocked",
  "task.claim_expired",
  "room.closed",
]);

export function createState(cursor = 0): SurfaceState {
  return {
    health: "stopped",
    cursor,
    heartbeatIntervalSeconds: 20,
    liveWorkers: 0,
    newActionable: 0,
  };
}

export function applySnapshot(state: SurfaceState, snapshot: RoomSnapshot): void {
  state.roomName = snapshot.room?.name;
  state.participantId = snapshot.you?.participant_id;
  state.liveWorkers = (snapshot.participants ?? []).reduce((count, participant) => {
    const liveCompanions = (participant.presence?.runtimes ?? []).filter(
      (runtime) =>
        runtime.is_attachment === true &&
        runtime.declared?.role === "companion" &&
        LIVE.has(runtime.liveness ?? "disconnected"),
    ).length;
    return count + liveCompanions;
  }, 0);
}

function isForAnotherParticipant(event: EventEnvelope, participantId?: string): boolean {
  if (!participantId) return false;
  const target =
    event.payload.target_participant_id ??
    event.payload.to_participant_id ??
    event.payload.propose_to_participant_id;
  return typeof target === "string" && target !== participantId;
}

export function isActionable(event: EventEnvelope, participantId?: string): boolean {
  return ACTIONABLE_EVENTS.has(event.type) && !isForAnotherParticipant(event, participantId);
}

export function applyEvent(
  state: SurfaceState,
  event: EventEnvelope,
  countAsNew = true,
): boolean {
  if (event.seq <= state.cursor) return false;
  state.cursor = Math.max(state.cursor, event.seq);
  const actionable = isActionable(event, state.participantId);
  if (actionable && countAsNew) state.newActionable += 1;
  return actionable;
}

export function markActivityOpened(state: SurfaceState): void {
  state.newActionable = 0;
}

export function effectiveHealth(state: SurfaceState, now = Date.now()): SurfaceHealth {
  if (state.health !== "live" || state.lastStreamContactAt === undefined) return state.health;
  const staleAfterMs = Math.max(15, state.heartbeatIntervalSeconds * 2.5) * 1000;
  return now - state.lastStreamContactAt > staleAfterMs ? "stale" : "live";
}

export class SerializedOperations {
  private tail: Promise<void> = Promise.resolve();

  run<T>(operation: () => Promise<T>): Promise<T> {
    const result = this.tail.then(operation, operation);
    this.tail = result.then(
      () => undefined,
      () => undefined,
    );
    return result;
  }
}

export class LifecycleGeneration {
  private generation = 0;
  private closed = false;

  capture(): number | undefined {
    return this.closed ? undefined : this.generation;
  }

  invalidate(): void {
    this.generation += 1;
  }

  close(): void {
    this.closed = true;
    this.generation += 1;
  }

  accepts(generation: number): boolean {
    return !this.closed && generation === this.generation;
  }
}

export async function gatherThenSerialize<T, R>(
  queue: SerializedOperations,
  gather: () => Promise<T | undefined>,
  mutate: (input: T) => Promise<R>,
  lifecycle?: LifecycleGeneration,
): Promise<R | undefined> {
  const generation = lifecycle?.capture();
  if (lifecycle && generation === undefined) return undefined;
  const input = await gather();
  if (input === undefined) return undefined;
  return queue.run(() => {
    if (lifecycle && !lifecycle.accepts(generation as number)) return Promise.resolve(undefined);
    return mutate(input);
  });
}

export function profilesRequiringActivityClear(
  existing: ConnectionProfile | undefined,
  target: ConnectionProfile,
  sameCredential: boolean,
): ConnectionProfile[] {
  if (sameCredential) return [];
  if (
    !existing ||
    (existing.baseUrl === target.baseUrl && existing.roomId === target.roomId)
  ) {
    return [target];
  }
  return [existing, target];
}

export async function drainAndSwitch<T>(steps: {
  stopAndDrain(): Promise<void>;
  clear(): Promise<void>;
  save(): Promise<T>;
  bind(saved: T): Promise<void>;
}): Promise<void> {
  await steps.stopAndDrain();
  await steps.clear();
  const saved = await steps.save();
  await steps.bind(saved);
}

export function normalizeActivityHistory(value: unknown): ActivityRecord[] {
  if (!Array.isArray(value)) return [];
  const records: ActivityRecord[] = [];
  for (const item of value) {
    if (typeof item === "string") {
      records.push({ line: item });
      continue;
    }
    if (!item || typeof item !== "object") continue;
    const candidate = item as Partial<ActivityRecord>;
    if (typeof candidate.line !== "string") continue;
    if (
      candidate.seq !== undefined &&
      (typeof candidate.seq !== "number" || !Number.isSafeInteger(candidate.seq))
    ) {
      continue;
    }
    records.push({
      ...(candidate.seq === undefined ? {} : { seq: candidate.seq }),
      ...(candidate.actionable === true ? { actionable: true } : {}),
      line: candidate.line,
    });
  }
  return records;
}

export function countUnopenedActivity(
  history: readonly ActivityRecord[],
  lastOpenedSeq: number,
): number {
  return history.filter(
    (record) => record.actionable === true && record.seq !== undefined && record.seq > lastOpenedSeq,
  ).length;
}

export function appendUniqueActivity(
  history: readonly ActivityRecord[],
  record: ActivityRecord,
  limit: number,
): { history: ActivityRecord[]; appended: boolean } {
  if (record.seq !== undefined && history.some((existing) => existing.seq === record.seq)) {
    return { history: [...history], appended: false };
  }
  const next = [...history, record];
  return { history: next.slice(Math.max(0, next.length - limit)), appended: true };
}
