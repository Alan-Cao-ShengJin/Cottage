"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import { useParams, useRouter } from "next/navigation";
import { Activity } from "../../../components/Activity";
import { CurrentWork } from "../../../components/CurrentWork";
import { PresenceRail } from "../../../components/Presence";
import { TaskBoard } from "../../../components/TaskBoard";
import { api, ArpError, clearSession, loadSession, type Session } from "../../../lib/api";
import { openConflicts, openWork, useNow, useRoom } from "../../../lib/useRoom";

/**
 * The room screen: presence rail, current work, task board, activity feed.
 *
 * Not a chat window. The layout puts "who is here and what are they doing" first,
 * because that is the question the product exists to answer.
 */
export default function RoomPage() {
  const params = useParams<{ roomId: string }>();
  const router = useRouter();
  const [session, setSession] = useState<Session | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [invite, setInvite] = useState<string | null>(null);
  const now = useNow();

  useEffect(() => {
    const existing = loadSession();
    if (!existing || existing.roomId !== params.roomId) {
      router.replace("/");
      return;
    }
    setSession(existing);
  }, [params.roomId, router]);

  const { snapshot, state, error: streamError, activity, refresh } = useRoom(session);

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

  if (!session) return null;

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
