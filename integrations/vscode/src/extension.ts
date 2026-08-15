import * as vscode from "vscode";

import { ArpClient } from "./client";
import {
  ConnectionSaveError,
  forgetConnection,
  loadConnection,
  normalizeBaseUrl,
  saveConnection,
  saveCursor,
} from "./config";
import { ActivityFeed } from "./feed";
import {
  applyEvent,
  applySnapshot,
  createState,
  drainAndSwitch,
  effectiveHealth,
  gatherThenSerialize,
  isActionable,
  LifecycleGeneration,
  markActivityOpened,
  profilesRequiringActivityClear,
  SerializedOperations,
} from "./state";
import { ConnectionProfile, EventEnvelope, SurfaceState } from "./types";

let activeClient: ArpClient | undefined;
let activeProfile: ConnectionProfile | undefined;
const connectionChanges = new SerializedOperations();
const lifecycle = new LifecycleGeneration();

export async function activate(context: vscode.ExtensionContext): Promise<void> {
  const feed = new ActivityFeed(context);
  const status = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Left, 100);
  status.command = "cottage.openActivity";
  status.show();
  let state: SurfaceState = createState();

  const render = (): void => renderStatus(status, state);
  const pulse = setInterval(render, 1_000);
  context.subscriptions.push(feed, status, { dispose: () => clearInterval(pulse) });

  const stopActive = async (): Promise<void> => {
    await activeClient?.stop();
    activeClient = undefined;
    activeProfile = undefined;
  };

  const bindClient = async (
    profile: ConnectionProfile,
    token: string,
    cursor: number,
  ): Promise<void> => {
    const unopenedActionable = await feed.useProfile(profile);
    state = createState(cursor);
    state.newActionable = unopenedActionable;
    activeProfile = profile;
    activeClient = new ArpClient(profile, token, cursor, {
      onHealth: (health, error) => {
        state.health = health;
        state.error = error;
        render();
      },
      onRestContact: () => {
        state.lastRestContactAt = Date.now();
        render();
      },
      onStreamContact: () => {
        state.lastStreamContactAt = Date.now();
        render();
      },
      onConnected: (connection) => {
        state.heartbeatIntervalSeconds = connection.heartbeat_interval_s;
      },
      onSnapshot: async (snapshot) => {
        applySnapshot(state, snapshot);
        render();
      },
      onEvent: async (event: EventEnvelope) => {
        const newlyRecorded = await feed.event(event, isActionable(event, state.participantId));
        const actionable = applyEvent(state, event, newlyRecorded);
        if (actionable && newlyRecorded) notify(event);
        render();
      },
      onResumeGap: async () => {
        state.health = "reconnecting";
        await feed.system("Retained history gap reported; waiting for the server snapshot.");
        render();
      },
      onPoisonFrame: async (reason) => {
        state.health = "reconnecting";
        await feed.system(`${reason} Recovering from a fresh room snapshot.`);
        render();
      },
      persistCursor: async (next) => {
        await saveCursor(context, profile, next);
        state.cursor = Math.max(state.cursor, next);
        render();
      },
    });
    activeClient.start();
  };

  context.subscriptions.push(
    vscode.commands.registerCommand("cottage.connect", () =>
      gatherThenSerialize(
        connectionChanges,
        async () => {
          const existing = await loadConnection(context);
          const rawBase = await vscode.window.showInputBox({
            title: "Connect Cottage",
            prompt: "Cottage server root URL",
            value: existing?.profile.baseUrl ?? "https://agent-rooms.fly.dev",
            ignoreFocusOut: true,
          });
          if (!rawBase) return undefined;
          let baseUrl: string;
          try {
            baseUrl = normalizeBaseUrl(rawBase);
          } catch (error) {
            await vscode.window.showErrorMessage(
              error instanceof Error ? error.message : "Invalid URL.",
            );
            return undefined;
          }
          const roomId = await vscode.window.showInputBox({
            title: "Connect Cottage",
            prompt: "Room ID",
            value: existing?.profile.roomId ?? "",
            ignoreFocusOut: true,
            validateInput: (value) => (value.trim() ? undefined : "Room ID is required."),
          });
          if (!roomId) return undefined;
          const exactExisting =
            existing?.profile.baseUrl === baseUrl && existing.profile.roomId === roomId.trim();
          const enteredToken = await vscode.window.showInputBox({
            title: "Connect Cottage",
            prompt: exactExisting
              ? "Participant token (leave blank to reuse this room's saved token)"
              : "Participant token",
            password: true,
            ignoreFocusOut: true,
          });
          if (enteredToken === undefined) return undefined;
          return {
            profile: { baseUrl, roomId: roomId.trim() },
            enteredToken: enteredToken.trim() || undefined,
          };
        },
        async ({ profile, enteredToken }) => {
          // Prompts deliberately ran outside the lifecycle queue. Reload now: a
          // disconnect or another completed mutation may have changed the saved seat.
          const existing = await loadConnection(context);
          const exactExisting =
            existing?.profile.baseUrl === profile.baseUrl &&
            existing.profile.roomId === profile.roomId;
          const token = enteredToken || (exactExisting ? existing?.token : undefined);
          if (!token) {
            void vscode.window.showErrorMessage("A room-scoped participant token is required.");
            return;
          }
          const sameCredential = exactExisting && existing?.token === token;
          try {
            await drainAndSwitch({
              stopAndDrain: stopActive,
              clear: async () => {
                for (const staleProfile of profilesRequiringActivityClear(
                  existing?.profile,
                  profile,
                  sameCredential,
                )) {
                  await feed.clearProfile(staleProfile);
                }
              },
              save: () => saveConnection(context, profile, token),
              bind: async (saved) => {
                await bindClient(profile, token, saved.cursor);
                if (saved.residue.length > 0) {
                  const detail = saved.residue.join(", ");
                  await feed
                    .system(`Connected; local orphan remains: ${detail}.`)
                    .catch(() => undefined);
                  void vscode.window.showWarningMessage(
                    `Cottage connected, but could not remove: ${detail}.`,
                  );
                }
              },
            });
          } catch (error) {
            state = createState();
            state.health = "error";
            const residue = error instanceof ConnectionSaveError ? error.residue : [];
            const baseMessage =
              error instanceof Error ? error.message : "Connection change failed.";
            state.error =
              residue.length > 0 ? `${baseMessage} Residue: ${residue.join(", ")}.` : baseMessage;
            render();
            void vscode.window.showErrorMessage(`Cottage did not switch rooms: ${state.error}`);
          }
        },
        lifecycle,
      ),
    ),
    vscode.commands.registerCommand("cottage.disconnect", () => {
      lifecycle.invalidate();
      return connectionChanges.run(async () => {
        const profile = activeProfile ?? (await loadConnection(context))?.profile;
        await stopActive();
        const cleanup = await Promise.allSettled([
          profile ? feed.clearProfile(profile) : Promise.resolve(),
          forgetConnection(context),
        ]);
        const feedFailed = cleanup[0].status === "rejected";
        const credentialCleanup = cleanup[1];
        const residue =
          credentialCleanup.status === "fulfilled"
            ? credentialCleanup.value.residue
            : ["credential cleanup status"];
        const cleanupFailed = feedFailed || residue.length > 0;
        state = createState();
        await feed.system(
          cleanupFailed
            ? `Disconnected; local residue remains: ${[
                ...(feedFailed ? ["activity history"] : []),
                ...residue,
              ].join(", ")}.`
            : "Disconnected and removed the saved credential.",
        );
        render();
        if (cleanupFailed) {
          void vscode.window.showErrorMessage(
            `Cottage disconnected, but could not remove: ${[
              ...(feedFailed ? ["activity history"] : []),
              ...residue,
            ].join(", ")}.`,
          );
        }
      });
    }),
    vscode.commands.registerCommand("cottage.openActivity", async () => {
      feed.show();
      // Capture the feed boundary synchronously before yielding. Events arriving
      // while the marker write is pending remain new both in memory and on restart.
      const persistOpenedBoundary = feed.markOpened();
      markActivityOpened(state);
      render();
      await persistOpenedBoundary;
    }),
    vscode.commands.registerCommand("cottage.openRoom", async () => {
      const profile = activeProfile ?? (await loadConnection(context))?.profile;
      if (!profile) {
        await vscode.window.showInformationMessage("Connect to a Cottage room first.");
        return;
      }
      const roomUrl = `${profile.baseUrl}/room/?room=${encodeURIComponent(profile.roomId)}`;
      await vscode.env.openExternal(vscode.Uri.parse(roomUrl));
    }),
  );

  render();
  const saved = await loadConnection(context);
  if (saved) {
    await connectionChanges.run(() => bindClient(saved.profile, saved.token, saved.cursor));
  }
}

export async function deactivate(): Promise<void> {
  lifecycle.close();
  await connectionChanges.run(async () => {
    await activeClient?.stop();
    activeClient = undefined;
    activeProfile = undefined;
  });
}

function renderStatus(item: vscode.StatusBarItem, state: SurfaceState): void {
  const health = effectiveHealth(state);
  const streamAge =
    state.lastStreamContactAt === undefined
      ? "—"
      : shortAge(Date.now() - state.lastStreamContactAt);
  const restAge =
    state.lastRestContactAt === undefined ? "—" : shortAge(Date.now() - state.lastRestContactAt);
  const icon =
    health === "live"
      ? "$(pulse)"
      : health === "connecting" || health === "reconnecting"
        ? "$(sync~spin)"
        : health === "stale" || health === "error"
          ? "$(warning)"
          : "$(circle-slash)";
  item.text = `${icon} Cottage · seq ${state.cursor} · stream ${streamAge} · workers ${state.liveWorkers} · new ${state.newActionable}`;
  item.tooltip = [
    `Cottage room: ${state.roomName ?? "not connected"}`,
    `Health: ${health}`,
    `Last stream contact: ${streamAge}`,
    `Last REST contact: ${restAge}`,
    `Room cursor: ${state.cursor}`,
    `Live companion workers: ${state.liveWorkers}`,
    `New actionable events not yet opened in Activity: ${state.newActionable}`,
    state.error ? `Last error: ${state.error}` : "",
    "Click to open the deterministic Activity feed. No model is running.",
  ]
    .filter(Boolean)
    .join("\n");
}

function shortAge(milliseconds: number): string {
  const seconds = Math.max(0, Math.floor(milliseconds / 1_000));
  if (seconds < 60) return `${seconds}s`;
  return `${Math.floor(seconds / 60)}m`;
}

function notify(event: EventEnvelope): void {
  const summary = `${event.type} at room seq ${event.seq}`;
  if (event.type === "room.closed" || event.type === "task.claim_expired") {
    void vscode.window.showErrorMessage(`Cottage: ${summary}`);
  } else {
    void vscode.window.showWarningMessage(`Cottage needs attention: ${summary}`);
  }
}
