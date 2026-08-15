import * as vscode from "vscode";

import { activityStorageKeys, clearStoredActivity } from "./config";
import {
  appendUniqueActivity,
  countUnopenedActivity,
  normalizeActivityHistory,
} from "./state";
import { ActivityRecord, ConnectionProfile, EventEnvelope } from "./types";

const MAX_HISTORY = 200;

export class ActivityFeed implements vscode.Disposable {
  private readonly output = vscode.window.createOutputChannel("Cottage Activity", { log: true });
  private history: ActivityRecord[] = [];
  private historyKey?: string;
  private openedKey?: string;

  constructor(private readonly context: vscode.ExtensionContext) {
    // History is loaded only after an exact origin + room profile is selected.
  }

  async useProfile(profile: ConnectionProfile): Promise<number> {
    const keys = activityStorageKeys(profile);
    this.historyKey = keys.history;
    this.openedKey = keys.opened;
    this.history = normalizeActivityHistory(this.context.globalState.get<unknown>(this.historyKey));
    this.output.clear();
    for (const record of this.history) this.output.appendLine(record.line);
    return countUnopenedActivity(
      this.history,
      this.context.globalState.get<number>(this.openedKey, 0),
    );
  }

  async clearProfile(profile: ConnectionProfile): Promise<void> {
    const keys = activityStorageKeys(profile);
    const cleanup = await clearStoredActivity(this.context, profile);
    if (this.historyKey === keys.history) {
      this.history = [];
      this.historyKey = undefined;
      this.openedKey = undefined;
      this.output.clear();
    }
    if (!cleanup.complete) {
      throw new Error(`Could not remove ${cleanup.residue.join(", ")}.`);
    }
  }

  async event(event: EventEnvelope, actionable: boolean): Promise<boolean> {
    const actor = event.actor?.participant_id || "room";
    const detail = summarize(event.payload);
    return this.append({
      seq: event.seq,
      ...(actionable ? { actionable: true } : {}),
      line: `${stamp(event.ts)} ${event.seq} ${event.type} — ${actor}${detail}`,
    });
  }

  async system(message: string): Promise<void> {
    await this.append({ line: `${stamp()} Cottage — ${message}` });
  }

  show(): void {
    this.output.show(true);
  }

  async markOpened(): Promise<void> {
    const lastSeq = this.history.reduce(
      (highest, record) => Math.max(highest, record.seq ?? 0),
      0,
    );
    if (this.openedKey) await this.context.globalState.update(this.openedKey, lastSeq);
  }

  dispose(): void {
    this.output.dispose();
  }

  private async append(record: ActivityRecord): Promise<boolean> {
    const merged = appendUniqueActivity(this.history, record, MAX_HISTORY);
    if (!merged.appended) return false;
    if (this.historyKey) await this.context.globalState.update(this.historyKey, merged.history);
    this.history = merged.history;
    this.output.appendLine(record.line);
    return true;
  }
}

function stamp(raw?: string): string {
  const date = raw ? new Date(raw) : new Date();
  return Number.isNaN(date.valueOf()) ? new Date().toISOString() : date.toISOString();
}

function summarize(payload: Record<string, unknown>): string {
  const metadata: Record<string, unknown> = {};
  for (const [key, value] of Object.entries(payload)) {
    const useful =
      key.endsWith("_id") ||
      key.endsWith("_ref") ||
      ["status", "liveness", "fence", "priority", "targets", "expires_at"].includes(key);
    if (useful && (typeof value !== "object" || Array.isArray(value))) metadata[key] = value;
  }
  const rendered = JSON.stringify(metadata);
  return rendered === "{}" ? "" : `: ${rendered.slice(0, 400)}`;
}
