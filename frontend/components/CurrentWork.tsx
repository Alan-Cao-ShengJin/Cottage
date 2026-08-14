"use client";

import { useMemo, useState } from "react";
import type { Conflict, Participant, WorkDeclaration } from "../lib/types";
import { formatAge, participantName } from "../lib/useRoom";

/**
 * Current work — the primary surface of the product.
 *
 * Answers "what is happening right now" at a glance. Two details do real work:
 *   - a target that another active declaration also names is highlighted, so the
 *     collision is visible on the card rather than only in a conflict record;
 *   - a stale declaration is shown but marked, because hiding it would lose
 *     information while trusting it would mislead.
 */
export function CurrentWork({
  work,
  participants,
  conflicts,
  youId,
  now,
  onDeclare,
  onEnd,
  busy,
}: {
  work: WorkDeclaration[];
  participants: Participant[];
  conflicts: Conflict[];
  youId: string;
  now: number;
  onDeclare: (headline: string, targets: string[]) => Promise<void>;
  onEnd: (workId: string) => Promise<void>;
  busy: boolean;
}) {
  const [headline, setHeadline] = useState("");
  const [targets, setTargets] = useState("");
  const [open, setOpen] = useState(false);

  /** Targets claimed by more than one participant right now. */
  const contested = useMemo(() => {
    const owners = new Map<string, Set<string>>();
    for (const w of work) {
      for (const t of w.targets) {
        if (!owners.has(t)) owners.set(t, new Set());
        owners.get(t)!.add(w.participant_id);
      }
    }
    return new Set(
      [...owners.entries()].filter(([, who]) => who.size > 1).map(([t]) => t),
    );
  }, [work]);

  const mine = work.some((w) => w.participant_id === youId);

  const submit = async () => {
    if (!headline.trim()) return;
    await onDeclare(
      headline.trim(),
      targets
        .split(/[,\n]/)
        .map((t) => t.trim())
        .filter(Boolean),
    );
    setHeadline("");
    setTargets("");
    setOpen(false);
  };

  return (
    <div className="region">
      <h2>Current work · {work.length}</h2>

      <div className="stack">
        {work.length === 0 && (
          <div className="empty">
            Nobody has declared what they are working on. This is the surface that keeps
            concurrent work from colliding — declare yours.
          </div>
        )}

        {work.map((w) => (
          <div
            key={w.id}
            className={`card work ${w.status} ${w.stale ? "is-stale" : ""}`}
          >
            <div className="work-headline">{w.headline}</div>
            <div className="meta">
              <span>{participantName(participants, w.participant_id)}</span>
              <span>·</span>
              <span>{w.status}</span>
              <span>·</span>
              <span>started {formatAge(w.started_at, now)} ago</span>
              {w.stale && (
                <>
                  <span>·</span>
                  <span className="stale-flag">
                    stale — owner stopped reporting, do not assume this is current
                  </span>
                </>
              )}
            </div>

            {w.targets.length > 0 && (
              <div className="targets">
                {w.targets.map((t) => (
                  <span
                    key={t}
                    className={`target ${contested.has(t) ? "clash" : ""}`}
                    title={
                      contested.has(t)
                        ? "Another participant is also working on this"
                        : undefined
                    }
                  >
                    {t}
                  </span>
                ))}
              </div>
            )}

            {w.note && <div className="meta">{w.note}</div>}

            {w.participant_id === youId && (
              <div className="row">
                <button
                  className="btn subtle"
                  onClick={() => void onEnd(w.id)}
                  disabled={busy}
                >
                  End this work
                </button>
              </div>
            )}
          </div>
        ))}

        {!open && (
          <button className="btn" onClick={() => setOpen(true)}>
            {mine ? "Declare more work" : "Declare what you are working on"}
          </button>
        )}

        {open && (
          <div className="card">
            <div className="field">
              <label htmlFor="work-headline">Headline</label>
              <input
                id="work-headline"
                value={headline}
                placeholder="Refactoring the auth middleware"
                onChange={(e) => setHeadline(e.target.value)}
              />
            </div>
            <div className="field">
              <label htmlFor="work-targets">Targets</label>
              <input
                id="work-targets"
                value={targets}
                placeholder="src/auth.py, docs/SECURITY.md"
                onChange={(e) => setTargets(e.target.value)}
              />
              <span className="hint">
                Comma-separated. These are how the room detects that you and someone
                else are about to collide, so be specific.
              </span>
            </div>
            <div className="row">
              <button className="btn primary" onClick={() => void submit()} disabled={busy}>
                Declare
              </button>
              <button className="btn subtle" onClick={() => setOpen(false)}>
                Cancel
              </button>
            </div>
          </div>
        )}

        {conflicts.length > 0 && (
          <>
            <h2 style={{ marginTop: 12 }}>Conflicts · {conflicts.length}</h2>
            {conflicts.map((c) => (
              <div className="card tight conflict" key={c.id}>
                <div className="conflict-kind">{c.kind.replace(/_/g, " ")}</div>
                <div>{c.detail}</div>
                <div className="conflict-advisory">
                  Advisory — nothing is blocked. The room surfaces collisions and leaves
                  the resolution to you.
                </div>
              </div>
            ))}
          </>
        )}
      </div>
    </div>
  );
}
