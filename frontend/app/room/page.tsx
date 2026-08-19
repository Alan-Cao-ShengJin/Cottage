"use client";

import { Suspense, useCallback, useEffect, useMemo, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import { Activity } from "../../components/Activity";
import { CurrentWork } from "../../components/CurrentWork";
import { PresenceRail } from "../../components/Presence";
import { TaskBoard } from "../../components/TaskBoard";
import {
  api,
  ArpError,
  clearSession,
  loadSession,
  saveSession,
  type Session,
} from "../../lib/api";
import { openConflicts, openWork, useNow, useRoom } from "../../lib/useRoom";

/**
 * The room screen: presence rail, current work, task board, activity feed.
 *
 * Not a chat window. The layout puts "who is here and what are they doing" first,
 * because that is the question the product exists to answer.
 *
 * The room id arrives as `?room=`, not as a path segment, because the console is a static
 * export and a build cannot pre-render an id that does not exist yet (`next.config.js`).
 */
function RoomView() {
  const params = useSearchParams();
  const roomId = params.get("room");
  const router = useRouter();
  const [session, setSession] = useState<Session | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [invite, setInvite] = useState<string | null>(null);
  const now = useNow();

  // An invitation arriving in the URL is the whole point of a shareable link, and it is
  // also a credential — so it is lifted into state once and removed from the address bar
  // before anything else happens. Left there it would sit in history, in a bookmark, and in
  // the `Referer` of every subsequent request.
  const [pendingInvite, setPendingInvite] = useState("");
  const [displayName, setDisplayName] = useState("");
  const [checkedSession, setCheckedSession] = useState(false);

  useEffect(() => {
    const existing = loadSession();
    if (existing && existing.roomId === roomId) {
      setSession(existing);
      setCheckedSession(true);
      return;
    }
    const fromUrl = params.get("invite");
    if (fromUrl) {
      setPendingInvite(fromUrl);
      const stripped = new URLSearchParams(params.toString());
      stripped.delete("invite");
      router.replace(`/room/?${stripped.toString()}`);
    }
    // Deliberately *not* a redirect to `/`. A person following an invitation link had no
    // session by definition, and bouncing them to a marketing page was indistinguishable
    // from the room being broken — which is exactly how it was reported.
    setCheckedSession(true);
  }, [roomId, router, params]);

  const joinWithInvitation = useCallback(
    async (token: string, name: string) => {
      setBusy(true);
      setError(null);
      try {
        const joined = await api.join({
          invitation_token: token.trim(),
          display_name: name.trim(),
        });
        const next: Session = {
          participantToken: joined.participant_token,
          participantId: joined.participant.id,
          // The invitation names the room, and it is the authority on which room this is.
          // A `?room=` that disagrees is a stale or mistyped link, not a second opinion.
          roomId: joined.room.id,
          displayName: name.trim(),
        };
        saveSession(next);
        setPendingInvite("");
        if (joined.room.id !== roomId) {
          router.replace(`/room/?room=${encodeURIComponent(joined.room.id)}`);
        }
        setSession(next);
      } catch (err) {
        setError(
          err instanceof ArpError
            ? `${err.code}: ${err.message}`
            : err instanceof Error
              ? err.message
              : String(err),
        );
      } finally {
        setBusy(false);
      }
    },
    [roomId, router],
  );

  const { snapshot, state, error: streamError, activity, liveActivity, refresh } =
    useRoom(session);

  const act = useCallback(
    async (fn: () => Promise<unknown>) => {
      setBusy(true);
      setError(null);
      try {
        await fn();
        await refresh();
      } catch (err) {
        // Protocol errors are information, so show the code: `lease_conflict` and
        // `stale_fence` mean different things and the user needs to know which.
        setError(
          err instanceof ArpError
            ? `${err.code}: ${err.message}`
            : err instanceof Error
              ? err.message
              : String(err),
        );
      } finally {
        setBusy(false);
      }
    },
    [refresh],
  );

  const you = useMemo(
    () => snapshot?.participants.find((p) => p.id === snapshot.you.participant_id) ?? null,
    [snapshot],
  );
  const runtime = you?.presence?.runtime ?? null;

  if (!session) {
    if (!checkedSession) return null;
    return (
      <div className="shell">
        <div className="topbar">
          <span className="brand">
            Cottage
            <small>join a room</small>
          </span>
          <span className="spacer" />
          <a className="btn" href="/">
            Home
          </a>
        </div>
        <div className="landing">
          <div className="card">
            <h2>Open a room you were invited to</h2>
            <p className="hint">
              Paste the invitation you were given. It is the only credential you need — the
              room it names is the room you join. Agents use the same invitation with{" "}
              <code>join_room</code>; this is the same door for a person.
            </p>
            {error && <div className="error">{error}</div>}
            <div className="field">
              <label htmlFor="join-name">Your name in the room</label>
              <input
                id="join-name"
                value={displayName}
                placeholder="Alan"
                autoComplete="name"
                onChange={(e) => setDisplayName(e.target.value)}
              />
            </div>
            <div className="field">
              <label htmlFor="join-invite">Invitation</label>
              <input
                id="join-invite"
                value={pendingInvite}
                placeholder="paste the invitation token"
                autoComplete="off"
                spellCheck={false}
                onChange={(e) => setPendingInvite(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Enter" && pendingInvite.trim() && displayName.trim()) {
                    void joinWithInvitation(pendingInvite, displayName);
                  }
                }}
              />
            </div>
            <button
              className="btn"
              disabled={busy || !pendingInvite.trim() || !displayName.trim()}
              onClick={() => void joinWithInvitation(pendingInvite, displayName)}
            >
              {busy ? "Joining…" : "Join room"}
            </button>
            <span className="hint" style={{ display: "block", marginTop: 8 }}>
              If this returns <code>unauthenticated</code>, this instance requires an
              account before joining. <a href="/account/login">Sign in</a> and open the link
              again — the invitation is still valid.
            </span>
          </div>
        </div>
      </div>
    );
  }

  if (!snapshot) {
    return (
      <div className="shell">
        <div className="topbar">
          <span className="brand">
            Agent Rooms
            <small>connecting…</small>
          </span>
        </div>
        <div className="landing">
          {streamError ? <div className="error">{streamError}</div> : <p>Loading room…</p>}
        </div>
      </div>
    );
  }

  const { room } = snapshot;

  return (
    <div className="shell">
      <div className="topbar">
        <span className="brand">
          {room.name}
          <small>
            {room.visibility.replace("_", "-")} · {room.status} · seq {room.event_seq}
          </small>
        </span>

        <span className="spacer" />

        <span className="status">
          <span
            className={`dot ${
              state === "live" ? "live_push" : state === "reconnecting" ? "stale" : "idle"
            }`}
          />
          {state === "live" ? "live" : state}
        </span>

        <button
          className="btn"
          disabled={busy}
          onClick={() =>
            void act(async () => {
              const created = await api.createInvitation(
                session.participantToken,
                room.id,
                { max_redemptions: 20 },
              );
              setInvite(created.token);
            })
          }
        >
          Create invitation
        </button>

        <button
          className="btn subtle"
          disabled={busy}
          onClick={() =>
            void act(async () => {
              await api.leave(session.participantToken, room.id);
              clearSession();
              router.push("/");
            })
          }
        >
          Leave
        </button>
      </div>

      {invite && (
        <div style={{ padding: "8px 20px 0" }}>
          <div className="card tight">
            <span className="hint">
              Invitation token — shown once. Give it to a human or an agent; it is the only
              way into this room.
            </span>
            <div className="token-out">{invite}</div>
            <button className="btn subtle" onClick={() => setInvite(null)}>
              Dismiss
            </button>
          </div>
        </div>
      )}

      {(error || streamError) && (
        <div style={{ padding: "8px 20px 0" }}>
          <div className="error">{error ?? streamError}</div>
        </div>
      )}

      <div className="board">
        <PresenceRail
          participants={snapshot.participants}
          youId={snapshot.you.participant_id}
          liveActivity={liveActivity}
        />

        <div className="region" style={{ display: "grid", gap: 20 }}>
          <CurrentWork
            work={openWork(snapshot.work)}
            participants={snapshot.participants}
            conflicts={openConflicts(snapshot.conflicts)}
            youId={snapshot.you.participant_id}
            now={now}
            busy={busy}
            onDeclare={(headline, targets) =>
              act(() =>
                api.declareWork(session.participantToken, room.id, { headline, targets }),
              )
            }
            onEnd={(workId) =>
              act(() => api.endWork(session.participantToken, room.id, workId))
            }
          />

          <TaskBoard
            tasks={snapshot.tasks}
            participants={snapshot.participants}
            youId={snapshot.you.participant_id}
            mayClaim={runtime?.may_claim ?? false}
            claimDeniedReason={runtime?.claim_denied_reason ?? null}
            now={now}
            busy={busy}
            onCreate={(title, targets) =>
              act(() => api.createTask(session.participantToken, room.id, { title, targets }))
            }
            onClaim={(taskId) =>
              act(() => api.claimTask(session.participantToken, room.id, taskId))
            }
            onRenew={(taskId, fence) =>
              act(() => api.renewClaim(session.participantToken, room.id, taskId, fence))
            }
            onRelease={(taskId, fence) =>
              act(() => api.releaseClaim(session.participantToken, room.id, taskId, fence))
            }
            onComplete={(taskId, fence) =>
              act(() =>
                api.completeTask(
                  session.participantToken,
                  room.id,
                  taskId,
                  fence,
                  "completed from the browser",
                ),
              )
            }
          />
        </div>

        <Activity
          activity={activity}
          messages={snapshot.messages}
          participants={snapshot.participants}
          now={now}
          busy={busy}
          onPost={(body) =>
            act(() => api.postMessage(session.participantToken, room.id, { body }))
          }
        />
      </div>
    </div>
  );
}

/**
 * `useSearchParams` reads something that only exists in the browser, so a prerendered page
 * must declare a boundary for it. Without this the static export fails to build.
 */
export default function RoomPage() {
  return (
    <Suspense fallback={null}>
      <RoomView />
    </Suspense>
  );
}
