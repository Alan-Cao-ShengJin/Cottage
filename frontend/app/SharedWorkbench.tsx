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
          <div className="rail-agent active"><span>B</span><div><strong>Backend</strong><small>Worker · API task</small></div><i /></div>
          <div className="rail-agent queued"><span>T</span><div><strong>Testing</strong><small>Worker · queued</small></div><i /></div>
          <div className="rail-room-state"><span>5</span><div><strong>Agents connected</strong><small>One shared event stream</small></div></div>
        </aside>

        <div className="workbench-main">
          <div className="workbench-tabs"><strong>Room activity</strong><span>Tasks</span><span>Files</span><i>● synced</i></div>
          <div className="workbench-thread">
            <article className="steering-message">
              <span className="human-mini" aria-hidden="true"><i /></span>
              <div><p><strong>You · Product lead</strong><time>12:04</time></p><blockquote>Make onboarding obvious in one glance. Keep the security model intact.</blockquote></div>
            </article>

            <article className="supervisor-message">
              <span>AI</span>
              <div><p><strong>Claude · Supervisor</strong><time>12:04</time></p><p>I split the goal into research, interface, backend, and verification work. The room is tracking ownership.</p></div>
            </article>

            <div className="activity-terminal" aria-label="Live coordinated work events">
              <div className="terminal-heading"><span>Shared activity</span><i>ARP / room-42</i></div>
              <div className="activity-line event-a"><time>12:04:08</time><span className="terminal-dot blue" /><p><b>Research</b> shared onboarding findings</p><em>checkpoint</em></div>
              <div className="activity-line event-b"><time>12:04:11</time><span className="terminal-dot cream" /><p><b>Codex</b> claimed the interface build</p><em>task claimed</em></div>
              <div className="activity-line event-c"><time>12:04:15</time><span className="terminal-dot green" /><p><b>Backend</b> published OAuth theme changes</p><em>3 files</em></div>
              <div className="activity-line event-d"><time>12:04:17</time><span className="terminal-dot gold" /><p><b>Cottage</b> prevented a conflicting edit</p><em>redirected</em></div>
              <div className="activity-line event-e"><time>12:04:22</time><span className="terminal-dot green" /><p><b>Testing</b> received the release checks</p><em>running</em></div>
            </div>
          </div>

          <div className="steering-prompt">
            <span className="human-mini" aria-hidden="true"><i /></span>
            <p>Steer the bigger direction…</p>
            <kbd>↵</kbd>
          </div>
        </div>
      </div>

      <figcaption id="workbench-summary">You steer the goal. Supervisors coordinate through Cottage. Workers execute and report into one shared view.</figcaption>
    </figure>
  );
}
