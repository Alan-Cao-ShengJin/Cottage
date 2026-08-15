"use client";

import type { Participant } from "../lib/types";
import { formatAge } from "../lib/useRoom";

/**
 * The presence rail.
 *
 * Shows negotiated capabilities and the derived runtime policy, not a product badge.
 * That is the point: a participant coordinating with another needs to know whether it
 * can actually be reached and whether it can hold a lease — and if it cannot, why.
 * Showing "ChatGPT" would tell you nothing you can act on; showing `long_poll` +
 * `attended` + "may not claim: requires human presence" tells you everything.
 */

const LIVENESS_LABEL: Record<string, string> = {
  live_push: "live · pushable",
  live_poll: "live · polling",
  attended: "attended · human-driven",
  idle: "idle",
  stale: "stale · heartbeat lapsed",
  disconnected: "disconnected",
};

/** Flags worth surfacing, in the order a reader cares about them. */
const SHOWN_CAPABILITIES = [
  "supports_push",
  "supports_poll",
  "can_execute_background",
  "can_initiate_followup",
  "requires_human_presence",
  "supports_tools",
] as const;

export function PresenceRail({
  participants,
  youId,
}: {
  participants: Participant[];
  youId: string;
}) {
  const active = participants.filter((p) => p.state === "joined");

  return (
    <div className="region">
      <h2>Participants · {active.length}</h2>
      <div className="stack">
        {active.length === 0 && <div className="empty">Nobody has joined yet.</div>}
        {active.map((p) => {
          const presence = p.presence;
          const liveness = presence?.liveness ?? "disconnected";
          const caps = new Set(presence?.negotiated_capabilities ?? []);
          const runtime = presence?.runtime ?? null;

          return (
            <div className="card tight participant" key={p.id}>
              <div className="participant-head">
                <span className={`dot ${liveness}`} aria-hidden />
                <span className="participant-name">
                  {p.identity.display_name}
                  {p.id === youId && " (you)"}
                </span>
                <span className="participant-org">{p.identity.org_name}</span>
              </div>

              {/* Attribution is the room's integrity guarantee, so where it is weaker the
                  board has to say so rather than render two different things the same. */}
              {p.identity.name_is_self_asserted && (
                <div className="self-asserted" title="This participant joined with an invitation link and chose its own name. Nobody vouched for it.">
                  guest · name self-asserted
                </div>
              )}

              <div className="liveness">
                {LIVENESS_LABEL[liveness] ?? liveness}
                {presence?.last_seen_at && liveness !== "disconnected" && (
                  <> · seen {formatAge(presence.last_seen_at, Date.now())} ago</>
                )}
              </div>

              {p.trust !== "member" && (
                <div className="chips">
                  <span className={`chip ${p.trust === "untrusted" ? "warn" : ""}`}>
                    {p.trust}
                  </span>
                </div>
              )}

              <div className="chips">
                {SHOWN_CAPABILITIES.filter((c) => caps.has(c)).map((c) => (
                  <span
                    key={c}
                    className={`chip ${c === "requires_human_presence" ? "warn" : "on"}`}
                  >
                    {c.replace(/_/g, " ")}
                  </span>
                ))}
                {caps.size === 0 && <span className="chip">no live connection</span>}
              </div>

              {runtime && !runtime.may_claim && runtime.claim_denied_reason && (
                <div className="claim-note">
                  Cannot hold work: {runtime.claim_denied_reason}
                </div>
              )}
              {runtime?.may_claim && (
                <div className="claim-note">
                  Can hold work · max lease {Math.round(runtime.max_lease_seconds / 60)}m
                  {!runtime.lease_renewable_unattended && " · needs a human to renew"}
                </div>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}
