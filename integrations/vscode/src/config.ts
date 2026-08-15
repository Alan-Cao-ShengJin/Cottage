import type * as vscode from "vscode";

import { ConnectionProfile } from "./types";

const PROFILE_KEY = "cottage.connectionProfile";
const TOKEN_PREFIX = "cottage.participantToken.";
const CURSOR_PREFIX = "cottage.roomCursor.";
const HISTORY_PREFIX = "cottage.activityHistory.";
const OPENED_PREFIX = "cottage.activityOpenedThrough.";

export function connectionKey(profile: ConnectionProfile): string {
  return encodeURIComponent(`${profile.baseUrl}\n${profile.roomId}`);
}

export function cursorKey(profile: ConnectionProfile): string {
  return `${CURSOR_PREFIX}${connectionKey(profile)}`;
}

export function tokenKey(profile: ConnectionProfile): string {
  return `${TOKEN_PREFIX}${connectionKey(profile)}`;
}

export interface CleanupResult {
  complete: boolean;
  residue: string[];
}

export interface ConnectionSaveResult {
  cursor: number;
  residue: string[];
}

export class ConnectionSaveError extends Error {
  constructor(
    message: string,
    readonly residue: string[],
    cause: unknown,
  ) {
    super(message, { cause });
    this.name = "ConnectionSaveError";
  }
}

export function activityStorageKeys(profile: ConnectionProfile): {
  history: string;
  opened: string;
} {
  const key = connectionKey(profile);
  return { history: `${HISTORY_PREFIX}${key}`, opened: `${OPENED_PREFIX}${key}` };
}

async function cleanupResidue(
  operations: Array<{ label: string; run(): PromiseLike<void> }>,
): Promise<string[]> {
  const results = await Promise.allSettled(
    operations.map((operation) => Promise.resolve().then(() => operation.run())),
  );
  return results.flatMap((result, index) =>
    result.status === "rejected" ? [operations[index].label] : [],
  );
}

export async function clearStoredActivity(
  context: vscode.ExtensionContext,
  profile: ConnectionProfile,
): Promise<CleanupResult> {
  const keys = activityStorageKeys(profile);
  const residue = await cleanupResidue([
    {
      label: "activity history",
      run: () => context.globalState.update(keys.history, undefined),
    },
    {
      label: "activity opened marker",
      run: () => context.globalState.update(keys.opened, undefined),
    },
  ]);
  return { complete: residue.length === 0, residue };
}

export function normalizeBaseUrl(raw: string): string {
  const parsed = new URL(raw.trim());
  if (parsed.protocol !== "https:" && parsed.protocol !== "http:") {
    throw new Error("Cottage URL must use http or https.");
  }
  if (parsed.username || parsed.password) {
    throw new Error("Put credentials in the participant-token prompt, not in the URL.");
  }
  const loopback = parsed.hostname === "localhost" || parsed.hostname === "127.0.0.1" || parsed.hostname === "[::1]";
  if (parsed.protocol !== "https:" && !loopback) {
    throw new Error("Cottage requires HTTPS except for an explicit loopback URL.");
  }
  if (parsed.pathname.replace(/\/+$/, "") === "/mcp") {
    parsed.pathname = "/";
  }
  parsed.search = "";
  parsed.hash = "";
  return parsed.toString().replace(/\/$/, "");
}

export async function loadConnection(
  context: vscode.ExtensionContext,
): Promise<{ profile: ConnectionProfile; token: string; cursor: number } | undefined> {
  const profile = context.globalState.get<ConnectionProfile>(PROFILE_KEY);
  if (!profile) return undefined;
  const token = await context.secrets.get(tokenKey(profile));
  if (!token) return undefined;
  return { profile, token, cursor: context.globalState.get<number>(cursorKey(profile), 0) };
}

export async function saveConnection(
  context: vscode.ExtensionContext,
  profile: ConnectionProfile,
  token: string,
): Promise<ConnectionSaveResult> {
  const old = context.globalState.get<ConnectionProfile>(PROFILE_KEY);
  const oldToken = old ? await context.secrets.get(tokenKey(old)) : undefined;
  const sameProfile = old?.baseUrl === profile.baseUrl && old.roomId === profile.roomId;
  const sameConnection =
    sameProfile && oldToken === token;
  if (sameConnection) {
    return { cursor: context.globalState.get<number>(cursorKey(profile), 0), residue: [] };
  }

  if (sameProfile) {
    // Reset replay before rotating a same-room credential. A failed secret write then
    // leaves the old credential at cursor zero, which is a safe replay rather than a
    // new principal silently inheriting an old cursor.
    try {
      await context.globalState.update(cursorKey(profile), 0);
      await context.secrets.store(tokenKey(profile), token);
      return { cursor: 0, residue: [] };
    } catch (error) {
      throw new ConnectionSaveError("Could not rotate the room credential.", [], error);
    }
  }

  const nextTokenKey = tokenKey(profile);
  try {
    await context.secrets.store(nextTokenKey, token);
  } catch (error) {
    throw new ConnectionSaveError("Could not stage the room credential.", [], error);
  }
  try {
    await context.globalState.update(cursorKey(profile), 0);
    // The active pointer is the commit point and is deliberately published last.
    await context.globalState.update(PROFILE_KEY, profile);
  } catch (error) {
    const residue = await cleanupResidue([
      {
        label: "staged participant credential",
        run: () => context.secrets.delete(nextTokenKey),
      },
      {
        label: "staged room cursor",
        run: () => context.globalState.update(cursorKey(profile), undefined),
      },
    ]);
    throw new ConnectionSaveError("Could not publish the room connection.", residue, error);
  }

  let residue: string[] = [];
  if (old) {
    // Cleanup happens after publication. Failure can leave only an unreachable
    // orphan, never an old credential paired with the new server.
    residue = await cleanupResidue([
      {
        label: "old participant credential",
        run: () => context.secrets.delete(tokenKey(old)),
      },
      {
        label: "old room cursor",
        run: () => context.globalState.update(cursorKey(old), undefined),
      },
    ]);
  }
  return { cursor: 0, residue };
}

export async function saveCursor(
  context: vscode.ExtensionContext,
  profile: ConnectionProfile,
  cursor: number,
): Promise<void> {
  await context.globalState.update(cursorKey(profile), cursor);
}

export async function forgetConnection(context: vscode.ExtensionContext): Promise<CleanupResult> {
  const profile = context.globalState.get<ConnectionProfile>(PROFILE_KEY);
  const operations: Array<{ label: string; run(): PromiseLike<void> }> = [
    {
      label: "active profile",
      run: () => context.globalState.update(PROFILE_KEY, undefined),
    },
  ];
  if (profile) {
    operations.push(
      {
        label: "participant credential",
        run: () => context.secrets.delete(tokenKey(profile)),
      },
      {
        label: "room cursor",
        run: () => context.globalState.update(cursorKey(profile), undefined),
      },
    );
  }
  const residue = await cleanupResidue(operations);
  return { complete: residue.length === 0, residue };
}
