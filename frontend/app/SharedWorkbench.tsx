const activity = [
  { time: "12:04:02", mark: ">", tone: "human", actor: "you", text: "set direction: simplify onboarding, preserve security", meta: "priority" },
  { time: "12:04:04", mark: "◆", tone: "supervisor", actor: "claude", text: "split goal into research, UI, backend, and verification", meta: "plan" },
  { time: "12:04:06", mark: "+", tone: "claim", actor: "research", text: "claimed first-glance comprehension audit", meta: "lease 8m" },
  { time: "12:04:08", mark: "+", tone: "claim", actor: "codex", text: "claimed interface implementation", meta: "lease 12m" },
  { time: "12:04:10", mark: "~", tone: "sync", actor: "content", text: "shared reduced-copy checkpoint", meta: "checkpoint" },
  { time: "12:04:12", mark: "↻", tone: "sync", actor: "cottage", text: "renewed 2 active task leases", meta: "seq 184" },
  { time: "12:04:15", mark: "+", tone: "file", actor: "backend", text: "published provider-neutral OAuth shell", meta: "3 files" },
  { time: "12:04:17", mark: "!", tone: "warning", actor: "cottage", text: "prevented overlapping CSS edit", meta: "redirected" },
  { time: "12:04:19", mark: "✓", tone: "done", actor: "research", text: "completed mobile readability audit", meta: "done" },
  { time: "12:04:21", mark: "+", tone: "claim", actor: "testing", text: "claimed responsive release checks", meta: "lease 6m" },
  { time: "12:04:24", mark: "~", tone: "file", actor: "codex", text: "updated graph and terminal animation", meta: "2 files" },
  { time: "12:04:27", mark: "✓", tone: "done", actor: "testing", text: "desktop + 390px visual checks passed", meta: "green" },
  { time: "12:04:29", mark: "◆", tone: "supervisor", actor: "claude", text: "assembled worker outputs for review", meta: "milestone" },
  { time: "12:04:32", mark: ">", tone: "human", actor: "you", text: "approved direction; continue to deployment", meta: "approved" },
];

function ActivityRows({ duplicate = false }: { duplicate?: boolean }) {
  return (
    <div className="cli-log-set" aria-hidden={duplicate || undefined}>
      {activity.map((event, index) => (
        <div className="cli-line" key={`${duplicate ? "copy" : "event"}-${index}`}>
          <time>{event.time}</time>
          <span className={`cli-mark ${event.tone}`}>{event.mark}</span>
          <p><b>{event.actor}</b> {event.text}</p>
          <em>{event.meta}</em>
        </div>
      ))}
    </div>
  );
}

export default function SharedWorkbench() {
  return (
    <figure className="team-workbench" aria-labelledby="workbench-title" aria-describedby="workbench-summary">
      <div className="workbench-chrome">
        <div className="window-dots" aria-hidden="true"><i /><i /><i /></div>
        <div id="workbench-title"><span className="status-beacon" /> Cottage · Launch room</div>
        <span>Live</span>
      </div>

      <div className="workbench-body">
        <aside className="agent-rail" aria-label="AI team status">
          <p>AI team</p>
          <div className="rail-agent active"><span>C</span><div><strong>Claude</strong><small>Supervisor · coordinating</small></div><i /></div>
          <div className="rail-agent active"><span>X</span><div><strong>Codex</strong><small>Supervisor · building</small></div><i /></div>
          <div className="rail-agent done"><span>R</span><div><strong>Research</strong><small>Worker · complete</small></div><b>✓</b></div>
          <div className="rail-agent active"><span>B</span><div><strong>Backend</strong><small>Worker · checkpoint</small></div><i /></div>
          <div className="rail-agent active"><span>T</span><div><strong>Testing</strong><small>Worker · verifying</small></div><i /></div>
          <div className="rail-room-state"><span>5</span><div><strong>Agents connected</strong><small>One ordered stream</small></div></div>
        </aside>

        <div className="workbench-main">
          <div className="workbench-tabs"><strong>Terminal</strong><span>room-42</span><span>ARP events</span><i>follow: on ●</i></div>
          <div className="workbench-thread cli-thread">
            <div className="activity-terminal cli-terminal" aria-label="Continuously scrolling coordinated work events">
              <div className="terminal-heading"><span>alan@cottage:~$ cottage watch room-42 --follow</span><i>LIVE</i></div>
              <div className="cli-command"><span>YOU</span><p>“Simplify onboarding. Keep the security model intact.”</p></div>
              <div className="cli-plan"><span>CLAUDE / SUPERVISOR</span><p>Plan accepted · 4 tasks opened · workers notified</p></div>
              <div className="cli-log-window">
                <div className="cli-log-track">
                  <ActivityRows />
                  <ActivityRows duplicate />
                </div>
              </div>
              <div className="cli-status"><span>● 5 online</span><span>2 working</span><span>0 conflicts</span><b>FOLLOW</b></div>
            </div>
          </div>

          <div className="steering-prompt cli-prompt">
            <code>alan@cottage:~$</code>
            <p>steer the bigger direction…</p>
            <kbd>↵</kbd>
          </div>
        </div>
      </div>

      <figcaption id="workbench-summary">You steer. Cottage streams the room. Every agent stays in sync.</figcaption>
    </figure>
  );
}
