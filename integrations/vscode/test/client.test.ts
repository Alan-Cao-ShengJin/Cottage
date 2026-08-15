import assert from "node:assert/strict";
import test from "node:test";

import { ArpClient, ArpClientRuntime, reconnectDelay, SseParser } from "../src/client";
import {
  activityStorageKeys,
  clearStoredActivity,
  ConnectionSaveError,
  cursorKey,
  forgetConnection,
  loadConnection,
  saveConnection,
  saveCursor,
  tokenKey,
} from "../src/config";
import { EventEnvelope, RoomSnapshot, SurfaceHealth } from "../src/types";

test("SSE parser keeps frames intact across arbitrary chunks", () => {
  const parser = new SseParser();
  assert.deepEqual(parser.push("id: 41\nevent: task.pro"), []);
  assert.deepEqual(parser.push('posed\ndata: {"seq":41,"type":"task.proposed","payload":{}}\n\n'), [
    {
      event: "task.proposed",
      id: 41,
      data: { seq: 41, type: "task.proposed", payload: {} },
    },
  ]);
});

test("SSE comments become contact-only keepalives", () => {
  const parser = new SseParser();
  assert.deepEqual(parser.push(": keepalive\n\n"), [{ event: "keepalive" }]);
});

test("SSE parser accepts CRLF and multi-line data", () => {
  const parser = new SseParser();
  assert.deepEqual(parser.push("event: note\r\ndata: first\r\ndata: second\r\n\r\n"), [
    { event: "note", data: "first\nsecond" },
  ]);
});

test("reconnect backoff is deterministic and bounded", () => {
  assert.deepEqual([0, 1, 2, 3, 4, 5, 6].map(reconnectDelay), [
    1_000, 2_000, 4_000, 8_000, 16_000, 30_000, 30_000,
  ]);
});

const PROFILE_A = { baseUrl: "https://a.example", roomId: "room-a" };
const PROFILE_B = { baseUrl: "https://b.example", roomId: "room-b" };

function fakeContext() {
  const state = new Map<string, unknown>();
  const secrets = new Map<string, string>();
  const failUpdates = new Set<string>();
  const failSecretStores = new Set<string>();
  const failSecretDeletes = new Set<string>();
  const context = {
    globalState: {
      get<T>(key: string, fallback?: T): T | undefined {
        return (state.has(key) ? state.get(key) : fallback) as T | undefined;
      },
      async update(key: string, value: unknown): Promise<void> {
        if (failUpdates.delete(key)) {
          throw new Error("state write failed");
        }
        if (value === undefined) state.delete(key);
        else state.set(key, value);
      },
    },
    secrets: {
      async get(key: string): Promise<string | undefined> {
        return secrets.get(key);
      },
      async store(key: string, value: string): Promise<void> {
        if (failSecretStores.delete(key)) {
          throw new Error("secret write failed");
        }
        secrets.set(key, value);
      },
      async delete(key: string): Promise<void> {
        if (failSecretDeletes.delete(key)) {
          throw new Error("secret delete failed");
        }
        secrets.delete(key);
      },
    },
  };
  return {
    context,
    state,
    secrets,
    failNextUpdate: (key: string) => {
      failUpdates.add(key);
    },
    failNextSecretStore: (key: string) => {
      failSecretStores.add(key);
    },
    failNextSecretDelete: (key: string) => {
      failSecretDeletes.add(key);
    },
  };
}

test("profile switch never pairs a new origin with the old credential on partial failure", async () => {
  const fake = fakeContext();
  await saveConnection(fake.context as never, PROFILE_A, "token-a");

  fake.failNextSecretStore(tokenKey(PROFILE_B));
  await assert.rejects(saveConnection(fake.context as never, PROFILE_B, "token-b"));
  assert.deepEqual(await loadConnection(fake.context as never), {
    profile: PROFILE_A,
    token: "token-a",
    cursor: 0,
  });

  fake.failNextUpdate("cottage.connectionProfile");
  await assert.rejects(saveConnection(fake.context as never, PROFILE_B, "token-b"));
  assert.equal(fake.secrets.has(tokenKey(PROFILE_B)), false, "unpublished credential is cleaned up");
  assert.equal((await loadConnection(fake.context as never))?.token, "token-a");
});

test("forget reports keychain residue while unpublishing the active profile", async () => {
  const fake = fakeContext();
  await saveConnection(fake.context as never, PROFILE_A, "token-a");
  fake.failNextSecretDelete(tokenKey(PROFILE_A));
  const cleanup = await forgetConnection(fake.context as never);
  assert.deepEqual(cleanup, { complete: false, residue: ["participant credential"] });
  assert.equal(await loadConnection(fake.context as never), undefined);
  assert.equal(fake.secrets.get(tokenKey(PROFILE_A)), "token-a", "residue is reported, not hidden");
});

test("unproven continuity clears stale target history and opened marker before a new token", async () => {
  const fake = fakeContext();
  await saveConnection(fake.context as never, PROFILE_A, "old-token");
  const keys = activityStorageKeys(PROFILE_A);
  await fake.context.globalState.update(keys.history, [{ seq: 7, line: "old room data" }]);
  await fake.context.globalState.update(keys.opened, 7);
  fake.failNextUpdate(keys.history);
  fake.failNextUpdate(keys.opened);
  const failedClear = await clearStoredActivity(fake.context as never, PROFILE_A);
  assert.deepEqual(failedClear.residue, ["activity history", "activity opened marker"]);
  await forgetConnection(fake.context as never);
  assert.equal(await loadConnection(fake.context as never), undefined);

  const cleared = await clearStoredActivity(fake.context as never, PROFILE_A);
  assert.equal(cleared.complete, true);
  assert.equal(fake.state.has(keys.history), false);
  assert.equal(fake.state.has(keys.opened), false);
  const saved = await saveConnection(fake.context as never, PROFILE_A, "new-token");
  assert.deepEqual(saved, { cursor: 0, residue: [] });
});

test("failed staged-secret rollback reports exact residue and keeps old profile active", async () => {
  const fake = fakeContext();
  await saveConnection(fake.context as never, PROFILE_A, "token-a");
  fake.failNextUpdate("cottage.connectionProfile");
  fake.failNextSecretDelete(tokenKey(PROFILE_B));
  let failure: unknown;
  try {
    await saveConnection(fake.context as never, PROFILE_B, "token-b");
  } catch (error) {
    failure = error;
  }
  assert.ok(failure instanceof ConnectionSaveError);
  assert.deepEqual(failure.residue, ["staged participant credential"]);
  assert.equal((await loadConnection(fake.context as never))?.profile.roomId, PROFILE_A.roomId);
  assert.equal(fake.secrets.get(tokenKey(PROFILE_B)), "token-b");
});

test("published new connection reports old secret and cursor cleanup residue", async () => {
  const fake = fakeContext();
  await saveConnection(fake.context as never, PROFILE_A, "token-a");
  await saveCursor(fake.context as never, PROFILE_A, 12);
  fake.failNextSecretDelete(tokenKey(PROFILE_A));
  fake.failNextUpdate(cursorKey(PROFILE_A));
  const saved = await saveConnection(fake.context as never, PROFILE_B, "token-b");
  assert.deepEqual(saved, {
    cursor: 0,
    residue: ["old participant credential", "old room cursor"],
  });
  assert.equal((await loadConnection(fake.context as never))?.profile.roomId, PROFILE_B.roomId);
  assert.equal(fake.secrets.get(tokenKey(PROFILE_A)), "token-a");
  assert.equal(fake.state.get(cursorKey(PROFILE_A)), 12);
});

interface CallbackLog {
  health: Array<{ health: SurfaceHealth; error?: string }>;
  snapshots: number[];
  events: number[];
  gaps: number;
  poison: string[];
  cursors: number[];
  restContacts: number;
  streamContacts: number;
}

function callbacks(log: CallbackLog) {
  return {
    onHealth(health: SurfaceHealth, error?: string): void {
      log.health.push({ health, error });
    },
    onRestContact(): void {
      log.restContacts += 1;
    },
    onStreamContact(): void {
      log.streamContacts += 1;
    },
    onConnected(): void {},
    async onSnapshot(snapshot: RoomSnapshot): Promise<void> {
      log.snapshots.push(snapshot.snapshot_seq);
    },
    async onEvent(event: EventEnvelope): Promise<void> {
      log.events.push(event.seq);
    },
    async onResumeGap(): Promise<void> {
      log.gaps += 1;
    },
    async onPoisonFrame(reason: string): Promise<void> {
      log.poison.push(reason);
    },
    async persistCursor(cursor: number): Promise<void> {
      log.cursors.push(cursor);
    },
  };
}

function callbackLog(): CallbackLog {
  return {
    health: [],
    snapshots: [],
    events: [],
    gaps: 0,
    poison: [],
    cursors: [],
    restContacts: 0,
    streamContacts: 0,
  };
}

function json(value: unknown, status = 200): Response {
  return new Response(JSON.stringify(value), {
    status,
    headers: { "content-type": "application/json" },
  });
}

function sse(frames: string, status = 200): Response {
  return new Response(frames, { status, headers: { "content-type": "text/event-stream" } });
}

function snapshot(seq: number): RoomSnapshot {
  return {
    type: "snapshot",
    protocol: "arp/1",
    snapshot_seq: seq,
    room: { id: PROFILE_A.roomId },
    participants: [],
  };
}

function connected() {
  return {
    connection_id: "connection-1",
    negotiated: ["can_receive_events", "supports_push", "supports_resume"],
    delivery_mode: "push",
    heartbeat_interval_s: 20,
    current_seq: 2,
  };
}

type FetchStep = Response | ((input: string, init?: RequestInit) => Promise<Response>);

function scriptedRuntime(
  responses: FetchStep[],
  onWait?: () => void,
): ArpClientRuntime {
  return {
    async fetch(input: string, init?: RequestInit): Promise<Response> {
      const next = responses.shift();
      assert.ok(next, "unexpected fetch");
      return typeof next === "function" ? next(input, init) : next;
    },
    async wait(): Promise<void> {
      onWait?.();
    },
  };
}

test("terminal HTTP statuses stop visibly without retrying", async () => {
  for (const status of [401, 403, 404, 410]) {
    const log = callbackLog();
    let waits = 0;
    const runtime = scriptedRuntime(
      [json(snapshot(2)), json(connected()), sse("denied", status), json({ ok: true })],
      () => {
        waits += 1;
      },
    );
    const client = new ArpClient(PROFILE_A, "token", 0, callbacks(log), runtime);
    client.start();
    await client.whenStopped();
    assert.equal(log.health.at(-1)?.health, "error");
    assert.match(log.health.at(-1)?.error ?? "", new RegExp(String(status)));
    assert.equal(waits, 0);
  }
});

test("resume gap accepts only its matching snapshot and room close drains", async () => {
  const log = callbackLog();
  const stream = [
    "event: resume_gap\ndata: {\"error\":\"resume_gap\"}\n\n",
    `id: 5\nevent: snapshot\ndata: {"type":"snapshot","protocol":"arp/1","snapshot_seq":5,"room":{"id":"${PROFILE_A.roomId}"},"participants":[]}\n\n`,
    `id: 6\nevent: room.closed\ndata: {"protocol":"arp/1","room_id":"${PROFILE_A.roomId}","seq":6,"type":"room.closed","payload":{}}\n\n`,
  ].join("");
  const runtime = scriptedRuntime([
    json(snapshot(2)),
    json(connected()),
    sse(stream),
    json({ ok: true }),
  ]);
  const client = new ArpClient(PROFILE_A, "token", 0, callbacks(log), runtime);
  client.start();
  await client.whenStopped();
  assert.equal(log.gaps, 1);
  assert.deepEqual(log.snapshots, [2, 5]);
  assert.deepEqual(log.events, [6]);
  assert.deepEqual(log.cursors, [2, 5, 6]);
  assert.equal(log.health.at(-1)?.health, "stopped");
});

test("poison event cursor is never persisted and recovery advances only to fresh snapshot", async () => {
  const log = callbackLog();
  const stream = [
    "event: resume_gap\ndata: {\"error\":\"resume_gap\"}\n\n",
    `id: 999\nevent: task.created\ndata: {"protocol":"arp/1","room_id":"${PROFILE_A.roomId}","seq":999,"type":"task.created","payload":{}}\n\n`,
  ].join("");
  const runtime = scriptedRuntime([
    json(snapshot(2)),
    json(connected()),
    sse(stream),
    json(snapshot(7)),
    json({ ok: true }),
    json({ error: "gone" }, 410),
  ]);
  const client = new ArpClient(PROFILE_A, "token", 0, callbacks(log), runtime);
  client.start();
  await client.whenStopped();
  assert.equal(log.poison.length, 1);
  assert.deepEqual(log.events, []);
  assert.deepEqual(log.cursors, [2, 7]);
  assert.equal(log.cursors.includes(999), false);
});

test("invalid REST snapshots are rejected without corrupting the cursor", async () => {
  const log = callbackLog();
  const runtime = scriptedRuntime([
    json({ type: "snapshot", snapshot_seq: "bad" }),
    json({ error: "gone" }, 410),
  ]);
  const client = new ArpClient(PROFILE_A, "token", 0, callbacks(log), runtime);
  client.start();
  await client.whenStopped();
  assert.deepEqual(log.snapshots, []);
  assert.deepEqual(log.cursors, []);
  assert.equal(log.health.at(-1)?.health, "error");
});

test("stop during disconnect skips an already-aborted reconnect delay", async () => {
  const log = callbackLog();
  let releaseDisconnect!: (response: Response) => void;
  let markDisconnectStarted!: () => void;
  const disconnectStarted = new Promise<void>((resolve) => {
    markDisconnectStarted = resolve;
  });
  const disconnectResponse = new Promise<Response>((resolve) => {
    releaseDisconnect = resolve;
  });
  let waits = 0;
  const runtime = scriptedRuntime(
    [
      json(snapshot(2)),
      json(connected()),
      sse("temporary", 500),
      () => {
        markDisconnectStarted();
        return disconnectResponse;
      },
    ],
    () => {
      waits += 1;
    },
  );
  const client = new ArpClient(PROFILE_A, "token", 0, callbacks(log), runtime);
  client.start();
  await disconnectStarted;
  const stopped = client.stop(false);
  releaseDisconnect(json({ ok: true }));
  await stopped;
  assert.equal(waits, 0);
  assert.equal(log.health.at(-1)?.health, "stopped");
});

test("hung SSE establishment never reports live and aborts promptly", async () => {
  const log = callbackLog();
  let streamStarted!: () => void;
  const started = new Promise<void>((resolve) => {
    streamStarted = resolve;
  });
  const runtime = scriptedRuntime([
    json(snapshot(2)),
    json(connected()),
    (_input, init) =>
      new Promise<Response>((_resolve, reject) => {
        streamStarted();
        init?.signal?.addEventListener(
          "abort",
          () => reject(new DOMException("Aborted", "AbortError")),
          { once: true },
        );
      }),
  ]);
  const client = new ArpClient(PROFILE_A, "token", 0, callbacks(log), runtime);
  client.start();
  await started;
  assert.equal(log.health.some(({ health }) => health === "live"), false);
  assert.equal(log.restContacts, 2);
  assert.equal(log.streamContacts, 0);
  await client.stop(false);
  assert.equal(log.health.at(-1)?.health, "stopped");
});

test("poll or incomplete negotiation never becomes live", async () => {
  for (const response of [
    { ...connected(), delivery_mode: "long_poll" },
    { ...connected(), negotiated: ["can_receive_events", "supports_push"] },
  ]) {
    const log = callbackLog();
    const runtime = scriptedRuntime([
      json(snapshot(2)),
      json(response),
      json({ error: "gone" }, 410),
    ]);
    const client = new ArpClient(PROFILE_A, "token", 0, callbacks(log), runtime);
    client.start();
    await client.whenStopped();
    assert.equal(log.health.some(({ health }) => health === "live"), false);
    assert.equal(log.streamContacts, 0);
    assert.equal(log.poison.includes("Invalid connect response."), true);
  }
});

test("wrong JSON and SSE content types are rejected before live delivery", async () => {
  const wrongRest = callbackLog();
  const restClient = new ArpClient(
    PROFILE_A,
    "token",
    0,
    callbacks(wrongRest),
    scriptedRuntime([
      new Response(JSON.stringify(snapshot(2)), {
        status: 200,
        headers: { "content-type": "text/plain" },
      }),
      json({ error: "gone" }, 410),
    ]),
  );
  restClient.start();
  await restClient.whenStopped();
  assert.deepEqual(wrongRest.snapshots, []);
  assert.equal(wrongRest.restContacts, 0);

  const wrongStream = callbackLog();
  const streamClient = new ArpClient(
    PROFILE_A,
    "token",
    0,
    callbacks(wrongStream),
    scriptedRuntime([
      json(snapshot(2)),
      json(connected()),
      new Response("event: ignored\n\n", {
        status: 200,
        headers: { "content-type": "text/plain" },
      }),
      json({ ok: true }),
      json({ error: "gone" }, 410),
    ]),
  );
  streamClient.start();
  await streamClient.whenStopped();
  assert.equal(wrongStream.health.some(({ health }) => health === "live"), false);
  assert.equal(wrongStream.streamContacts, 0);
});

test("wrong room and protocol snapshots are rejected before callbacks", async () => {
  for (const invalid of [
    { ...snapshot(2), room: { id: "other-room" } },
    { ...snapshot(2), protocol: "arp/2" },
  ]) {
    const log = callbackLog();
    const client = new ArpClient(
      PROFILE_A,
      "token",
      0,
      callbacks(log),
      scriptedRuntime([json(invalid), json({ error: "gone" }, 410)]),
    );
    client.start();
    await client.whenStopped();
    assert.deepEqual(log.snapshots, []);
    assert.deepEqual(log.cursors, []);
  }
});

test("wrong-room and mismatched-type SSE events are replaced without callback or cursor trust", async () => {
  for (const eventData of [
    {
      protocol: "arp/1",
      room_id: "other-room",
      seq: 9,
      type: "task.created",
      payload: {},
    },
    {
      protocol: "arp/1",
      room_id: PROFILE_A.roomId,
      seq: 9,
      type: "task.updated",
      payload: {},
    },
  ]) {
    const log = callbackLog();
    const frame = `id: 9\nevent: task.created\ndata: ${JSON.stringify(eventData)}\n\n`;
    const client = new ArpClient(
      PROFILE_A,
      "token",
      0,
      callbacks(log),
      scriptedRuntime([
        json(snapshot(2)),
        json(connected()),
        sse(frame),
        json(snapshot(7)),
        json({ ok: true }),
        json({ error: "gone" }, 410),
      ]),
    );
    client.start();
    await client.whenStopped();
    assert.deepEqual(log.events, []);
    assert.deepEqual(log.cursors, [2, 7]);
    assert.equal(log.cursors.includes(9), false);
  }
});

test("failed durable event delivery cannot advance its cursor", async () => {
  const log = callbackLog();
  const callbackSet = callbacks(log);
  callbackSet.onEvent = async () => {
    throw new Error("feed save failed");
  };
  const event = {
    protocol: "arp/1",
    room_id: PROFILE_A.roomId,
    seq: 3,
    type: "task.created",
    payload: {},
  };
  const client = new ArpClient(
    PROFILE_A,
    "token",
    0,
    callbackSet,
    scriptedRuntime([
      json(snapshot(2)),
      json(connected()),
      sse(`id: 3\nevent: task.created\ndata: ${JSON.stringify(event)}\n\n`),
      json({ ok: true }),
      json({ error: "gone" }, 410),
    ]),
  );
  client.start();
  await client.whenStopped();
  assert.deepEqual(log.cursors, [2]);
});
