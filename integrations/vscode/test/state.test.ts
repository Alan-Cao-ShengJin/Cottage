import assert from "node:assert/strict";
import test from "node:test";

import {
  appendUniqueActivity,
  applyEvent,
  applySnapshot,
  createState,
  countUnopenedActivity,
  effectiveHealth,
  drainAndSwitch,
  gatherThenSerialize,
  LifecycleGeneration,
  markActivityOpened,
  normalizeActivityHistory,
  profilesRequiringActivityClear,
  SerializedOperations,
} from "../src/state";

test("snapshot counts live companion runtimes, not seats or control surfaces", () => {
  const state = createState(9);
  applySnapshot(state, {
    type: "snapshot",
    snapshot_seq: 12,
    participants: [
      {
        id: "p1",
        presence: {
          liveness: "live_push",
          runtimes: [
            {
              is_attachment: true,
              liveness: "live_push",
              declared: { role: "control_surface" },
            },
            { is_attachment: true, liveness: "live_poll", declared: { role: "companion" } },
            { is_attachment: true, liveness: "live_push", declared: { role: "companion" } },
          ],
        },
      },
      {
        id: "p2",
        presence: {
          liveness: "stale",
          runtimes: [
            { is_attachment: true, liveness: "stale", declared: { role: "companion" } },
            { is_attachment: false, liveness: "live_poll", declared: { role: "companion" } },
          ],
        },
      },
    ],
  });
  assert.equal(state.liveWorkers, 2);
  assert.equal(state.cursor, 9, "a state refresh must not skip unconsumed stream events");
});

test("only new actionable events for this participant accumulate since Activity opened", () => {
  const state = createState(20);
  state.participantId = "mine";
  assert.equal(
    applyEvent(state, {
      seq: 25,
      type: "question.asked",
      payload: { to_participant_id: "other" },
    }),
    false,
  );
  assert.equal(
    applyEvent(state, {
      seq: 29,
      type: "directive.issued",
      payload: { target_participant_id: "mine" },
    }),
    true,
  );
  assert.equal(state.cursor, 29, "privacy-filtered sequence jumps are legal");
  assert.equal(state.newActionable, 1);
  assert.equal(
    applyEvent(state, {
      seq: 29,
      type: "directive.issued",
      payload: { target_participant_id: "mine" },
    }),
    false,
    "a replayed sequence must not notify twice",
  );
  assert.equal(state.newActionable, 1);
  markActivityOpened(state);
  assert.equal(state.newActionable, 0);
});

test("durably recorded replay advances state without inflating the new count", () => {
  const state = createState(4);
  assert.equal(
    applyEvent(state, { seq: 5, type: "question.asked", payload: {} }, false),
    true,
  );
  assert.equal(state.cursor, 5);
  assert.equal(state.newActionable, 0);
});

test("a silent live stream becomes visibly stale", () => {
  const state = createState();
  state.health = "live";
  state.heartbeatIntervalSeconds = 20;
  state.lastStreamContactAt = 1_000;
  assert.equal(effectiveHealth(state, 50_000), "live");
  assert.equal(effectiveHealth(state, 52_000), "stale");
});

test("activity history is durable and idempotent by room sequence", () => {
  const migrated = normalizeActivityHistory(["legacy", { seq: 8, line: "eight" }, null]);
  assert.deepEqual(migrated, [{ line: "legacy" }, { seq: 8, line: "eight" }]);
  const duplicate = appendUniqueActivity(migrated, { seq: 8, line: "duplicate" }, 200);
  assert.equal(duplicate.appended, false);
  assert.deepEqual(duplicate.history, migrated);
  const appended = appendUniqueActivity(migrated, { seq: 9, line: "nine" }, 2);
  assert.equal(appended.appended, true);
  assert.deepEqual(appended.history, [{ seq: 8, line: "eight" }, { seq: 9, line: "nine" }]);
});

test("new actionable count survives restart until Activity is opened through its sequence", () => {
  const history = [
    { seq: 10, actionable: true, line: "question" },
    { seq: 11, line: "presence" },
    { seq: 12, actionable: true, line: "directive" },
  ];
  assert.equal(countUnopenedActivity(history, 0), 2);
  assert.equal(countUnopenedActivity(history, 10), 1);
  assert.equal(countUnopenedActivity(history, 12), 0);
});

test("connection transitions drain an event before injected clear failure", async () => {
  const queue = new SerializedOperations();
  const order: string[] = [];
  let finishEvent!: () => void;
  const eventFinished = new Promise<void>((resolve) => {
    finishEvent = resolve;
  });
  const first = queue.run(() =>
    drainAndSwitch({
      stopAndDrain: async () => {
        order.push("stop");
        await eventFinished;
        order.push("event persisted");
      },
      clear: async () => {
        order.push("clear failed");
        throw new Error("clear failed");
      },
      save: async () => {
        order.push("save forbidden");
        return 0;
      },
      bind: async () => {
        order.push("bind forbidden");
      },
    }),
  );
  const second = queue.run(async () => {
    order.push("save next");
  });
  await Promise.resolve();
  assert.deepEqual(order, ["stop"]);
  finishEvent();
  await assert.rejects(first);
  await second;
  assert.deepEqual(order, ["stop", "event persisted", "clear failed", "save next"]);
});

test("injected save failure happens after clear and never binds a client", async () => {
  const order: string[] = [];
  await assert.rejects(
    drainAndSwitch({
      stopAndDrain: async () => {
        order.push("stopped");
      },
      clear: async () => {
        order.push("cleared");
      },
      save: async () => {
        order.push("save failed");
        throw new Error("save failed");
      },
      bind: async () => {
        order.push("bind forbidden");
      },
    }),
  );
  assert.deepEqual(order, ["stopped", "cleared", "save failed"]);
});

test("unproven credential continuity always clears the target profile", () => {
  const target = { baseUrl: "https://cottage.example", roomId: "room" };
  assert.deepEqual(profilesRequiringActivityClear(undefined, target, false), [target]);
  assert.deepEqual(profilesRequiringActivityClear(target, target, false), [target]);
  assert.deepEqual(profilesRequiringActivityClear(target, target, true), []);
});

test("interactive cancellation does not hold the lifecycle mutation queue", async () => {
  const queue = new SerializedOperations();
  let finishPrompt!: (value: string | undefined) => void;
  const prompt = new Promise<string | undefined>((resolve) => {
    finishPrompt = resolve;
  });
  let mutated = false;
  const connect = gatherThenSerialize(
    queue,
    () => prompt,
    async () => {
      mutated = true;
    },
  );
  let disconnected = false;
  await queue.run(async () => {
    disconnected = true;
  });
  assert.equal(disconnected, true, "disconnect runs while the prompt is still pending");
  finishPrompt(undefined);
  await connect;
  assert.equal(mutated, false, "cancelled input never enters the mutation queue");
});

test("valid connect input gathered before disconnect cannot mutate or bind", async () => {
  const queue = new SerializedOperations();
  const lifecycle = new LifecycleGeneration();
  let finishPrompt!: (value: string | undefined) => void;
  const prompt = new Promise<string | undefined>((resolve) => {
    finishPrompt = resolve;
  });
  let mutated = false;
  let bound = false;
  const connect = gatherThenSerialize(
    queue,
    () => prompt,
    async () => {
      mutated = true;
      bound = true;
    },
    lifecycle,
  );

  lifecycle.invalidate();
  let disconnected = false;
  await queue.run(async () => {
    disconnected = true;
  });
  finishPrompt("valid-token");
  await connect;

  assert.equal(disconnected, true);
  assert.equal(mutated, false, "stale prompt input cannot mutate connection state");
  assert.equal(bound, false, "stale prompt input cannot bind a client");
});

test("valid connect input gathered before deactivate is rejected and lifecycle stays closed", async () => {
  const queue = new SerializedOperations();
  const lifecycle = new LifecycleGeneration();
  let finishPrompt!: (value: string | undefined) => void;
  const prompt = new Promise<string | undefined>((resolve) => {
    finishPrompt = resolve;
  });
  let mutated = false;
  let bound = false;
  const connect = gatherThenSerialize(
    queue,
    () => prompt,
    async () => {
      mutated = true;
      bound = true;
    },
    lifecycle,
  );

  lifecycle.close();
  let deactivated = false;
  await queue.run(async () => {
    deactivated = true;
  });
  finishPrompt("valid-token");
  await connect;

  assert.equal(deactivated, true);
  assert.equal(mutated, false, "input from a prompt pending at deactivate cannot mutate");
  assert.equal(bound, false, "input from a prompt pending at deactivate cannot bind a client");

  let laterPromptShown = false;
  const laterConnect = await gatherThenSerialize(
    queue,
    async () => {
      laterPromptShown = true;
      return "another-valid-token";
    },
    async () => {
      mutated = true;
      bound = true;
    },
    lifecycle,
  );
  assert.equal(laterConnect, undefined);
  assert.equal(laterPromptShown, false, "closed lifecycle prevents later connect prompts");
  assert.equal(mutated, false);
  assert.equal(bound, false);
});
