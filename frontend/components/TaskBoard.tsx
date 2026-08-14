"use client";

import { useState } from "react";
import type { Participant, Task } from "../lib/types";
import { boardColumns, formatDuration, participantName, secondsUntil } from "../lib/useRoom";

/**
 * The task board.
 *
 * Leases are rendered as a live countdown rather than a static "assigned to" label,
 * because a lease is a *temporary* grant and the time remaining is the operative fact:
 * it tells everyone else when the work becomes reclaimable. A lease inside its last
 * quarter turns red for its holder, which is the cue to renew.
 */
export function TaskBoard({
  tasks,
  participants,
  youId,
  mayClaim,
  claimDeniedReason,
  now,
  onCreate,
  onClaim,
  onRenew,
  onRelease,
  onComplete,
  busy,
}: {
  tasks: Task[];
  participants: Participant[];
  youId: string;
  mayClaim: boolean;
  claimDeniedReason: string | null;
  now: number;
  onCreate: (title: string, targets: string[]) => Promise<void>;
  onClaim: (taskId: string) => Promise<void>;
  onRenew: (taskId: string, fence: number) => Promise<void>;
  onRelease: (taskId: string, fence: number) => Promise<void>;
  onComplete: (taskId: string, fence: number) => Promise<void>;
  busy: boolean;
}) {
  const [title, setTitle] = useState("");
  const [targets, setTargets] = useState("");
  const columns = boardColumns(tasks);

  const submit = async () => {
    if (!title.trim()) return;
    await onCreate(
      title.trim(),
      targets
        .split(/[,\n]/)
        .map((t) => t.trim())
        .filter(Boolean),
    );
    setTitle("");
    setTargets("");
  };

  return (
    <div className="region">
      <h2>Task board</h2>

      <div className="card" style={{ marginBottom: 12 }}>
        <div className="field">
          <label htmlFor="task-title">Add work to the board</label>
          <input
            id="task-title"
            value={title}
            placeholder="Cut the release branch"
            onChange={(e) => setTitle(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && void submit()}
          />
        </div>
        <div className="field">
          <label htmlFor="task-targets">Targets</label>
          <input
            id="task-targets"
            value={targets}
            placeholder="git/main, CHANGELOG.md"
            onChange={(e) => setTargets(e.target.value)}
          />
        </div>
        <button className="btn primary" onClick={() => void submit()} disabled={busy}>
          Create task
        </button>
      </div>

      {!mayClaim && claimDeniedReason && (
        <div className="card tight" style={{ marginBottom: 12 }}>
          <span className="hint">You cannot claim work here: {claimDeniedReason}</span>
        </div>
      )}

      <div className="columns">
        {columns.map((column) => (
          <div className="column" key={column.label}>
            <h3>
              <span>{column.label}</span>
              <span>{column.tasks.length}</span>
            </h3>
            <div className="stack">
              {column.tasks.length === 0 && <div className="empty">—</div>}
              {column.tasks.map((task) => {
                const claim = task.claim;
                const isMine = claim?.participant_id === youId;
                const remaining = claim ? secondsUntil(claim.expires_at, now) : 0;
                const expiring =
                  claim !== null && remaining < claim.heartbeat_interval_s * 3;

                return (
                  <div
                    key={task.id}
                    className={`card tight task ${task.status === "done" ? "done" : ""}`}
                  >
                    <div className="task-title">{task.title}</div>

                    {task.targets.length > 0 && (
                      <div className="targets">
                        {task.targets.map((t) => (
                          <span className="target" key={t}>
                            {t}
                          </span>
                        ))}
                      </div>
                    )}

                    {claim && (
                      <div className="row" style={{ marginTop: 6 }}>
                        <span className={`lease ${expiring ? "expiring" : ""}`}>
                          {isMine ? "your lease" : participantName(participants, claim.participant_id)}
                          {" · "}
                          {remaining > 0 ? formatDuration(remaining) + " left" : "expired"}
                          {" · fence "}
                          {claim.fence}
                        </span>
                      </div>
                    )}

                    {task.result && (
                      <div className="meta" style={{ marginTop: 6 }}>
                        {task.result}
                      </div>
                    )}

                    <div className="row">
                      {task.status === "open" && (
                        <button
                          className="btn"
                          onClick={() => void onClaim(task.id)}
                          disabled={busy || !mayClaim}
                          title={mayClaim ? undefined : claimDeniedReason ?? undefined}
                        >
                          Claim
                        </button>
                      )}
                      {isMine && claim && (
                        <>
                          <button
                            className={`btn ${expiring ? "primary" : ""}`}
                            onClick={() => void onRenew(task.id, claim.fence)}
                            disabled={busy}
                          >
                            Renew
                          </button>
                          <button
                            className="btn"
                            onClick={() => void onComplete(task.id, claim.fence)}
                            disabled={busy}
                          >
                            Complete
                          </button>
                          <button
                            className="btn subtle"
                            onClick={() => void onRelease(task.id, claim.fence)}
                            disabled={busy}
                          >
                            Release
                          </button>
                        </>
                      )}
                    </div>
                  </div>
                );
              })}
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}
