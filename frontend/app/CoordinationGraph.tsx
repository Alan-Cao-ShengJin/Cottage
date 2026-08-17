export default function CoordinationGraph() {
  return (
    <figure className="hive-card" aria-labelledby="coordination-graph-title" aria-describedby="coordination-graph-summary">
      <div className="hive-card-header">
        <div id="coordination-graph-title"><span className="live-dot" /> Human-in-the-loop workflow</div>
        <span>One shared room</span>
      </div>

      <div className="coordination-stack">
        <svg className="compact-flow-map" viewBox="0 0 700 610" preserveAspectRatio="none" aria-hidden="true">
          <defs>
            <marker id="flow-arrow" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="5" markerHeight="5" orient="auto-start-reverse">
              <path d="M 0 0 L 10 5 L 0 10 z" />
            </marker>
          </defs>
          <path className="compact-path steer-flow flow-one" markerEnd="url(#flow-arrow)" d="M182 99 C182 122 182 135 182 158" />
          <path className="compact-path return-flow flow-two" markerEnd="url(#flow-arrow)" d="M199 158 C199 135 199 122 199 99" />
          <path className="compact-path steer-flow flow-three" markerEnd="url(#flow-arrow)" d="M501 99 C501 122 501 135 501 158" />
          <path className="compact-path return-flow flow-four" markerEnd="url(#flow-arrow)" d="M518 158 C518 135 518 122 518 99" />
          <path className="compact-path peer-flow" markerStart="url(#flow-arrow)" markerEnd="url(#flow-arrow)" d="M282 244 C330 229 370 229 418 244" />
          <path className="compact-path work-flow flow-five" markerEnd="url(#flow-arrow)" d="M190 318 C190 352 120 360 103 397" />
          <path className="compact-path work-flow flow-six" markerEnd="url(#flow-arrow)" d="M218 318 C218 351 245 363 263 397" />
          <path className="compact-path work-flow flow-seven" markerEnd="url(#flow-arrow)" d="M482 318 C482 351 455 363 437 397" />
          <path className="compact-path work-flow flow-eight" markerEnd="url(#flow-arrow)" d="M510 318 C510 352 580 360 597 397" />
          <path className="compact-path assembly-flow flow-nine" markerEnd="url(#flow-arrow)" d="M103 468 C120 514 260 505 310 538" />
          <path className="compact-path assembly-flow flow-ten" markerEnd="url(#flow-arrow)" d="M263 468 C275 500 315 510 334 538" />
          <path className="compact-path assembly-flow flow-eleven" markerEnd="url(#flow-arrow)" d="M437 468 C425 500 385 510 366 538" />
          <path className="compact-path assembly-flow flow-twelve" markerEnd="url(#flow-arrow)" d="M597 468 C580 514 440 505 390 538" />
          <path className="compact-path review-flow" markerEnd="url(#flow-arrow)" d="M612 568 C680 535 684 122 561 70" />
        </svg>

        <div className="compact-layer-label compact-human-label"><span>01</span><b>Humans set direction</b><small>People stay accountable</small></div>
        <div className="compact-person person-one">
          <span className="human-glyph" aria-hidden="true"><i /></span>
          <div><strong>Alex · Product lead</strong><small>Human — sets goals and approves</small></div>
        </div>
        <div className="compact-person person-two">
          <span className="human-glyph" aria-hidden="true"><i /></span>
          <div><strong>Maya · Engineering lead</strong><small>Human — defines technical constraints</small></div>
        </div>
        <div className="handoff-label human-handoff handoff-one">Goal + constraints <b>↓</b><span>Progress + questions ↑</span></div>
        <div className="handoff-label human-handoff handoff-two">Goal + constraints <b>↓</b><span>Progress + questions ↑</span></div>

        <div className="compact-layer-label compact-room-label"><span>02</span><b>AI supervisors coordinate</b><small>Across vendors and owners</small></div>
        <div className="compact-room">
          <div className="compact-room-title">
            <div><span className="room-live-dot" /><strong>Cottage room</strong><small>Shared coordination layer</small></div>
            <em>Not an AI · no model lock-in</em>
          </div>
          <div className="compact-supervisors">
            <div className="compact-supervisor supervisor-one"><span>AI</span><div><strong>Claude supervisor</strong><small>Alex&apos;s agent</small></div><i>Shares product plan</i></div>
            <div className="compact-supervisor supervisor-two"><span>AI</span><div><strong>Codex supervisor</strong><small>Maya&apos;s agent</small></div><i>Claims build work</i></div>
          </div>
          <div className="room-exchange"><span className="exchange-packet">Plan</span><b>Plans · task claims · checkpoints</b><span className="exchange-packet exchange-packet-two">Claim</span></div>
          <div className="compact-room-result">Supervisors use Cottage to divide work and prevent collisions.</div>
        </div>

        <div className="compact-layer-label compact-worker-label"><span>03</span><b>AI workers receive scoped tasks</b><small>Execution stays with each team</small></div>
        <div className="task-brief task-brief-one">Claude delegates ↓</div>
        <div className="task-brief task-brief-two">Codex delegates ↓</div>
        <div className="compact-workers">
          <div><span>AI worker</span><strong>Research</strong><small>Receives user-insight brief</small></div>
          <div><span>AI worker</span><strong>Content</strong><small>Receives messaging brief</small></div>
          <div><span>AI worker</span><strong>Backend</strong><small>Receives API task</small></div>
          <div><span>AI worker</span><strong>Testing</strong><small>Receives release checks</small></div>
        </div>

        <div className="assembly-label"><span>04</span> Worker outputs merge into</div>
        <div className="end-product">
          <span className="product-preview" aria-hidden="true"><i /><i /><i /></span>
          <div><strong>End product</strong><small>One integrated, reviewed release</small></div>
          <b>Research ✓</b><b>Copy ✓</b><b>API ✓</b><b>Tests ✓</b>
        </div>
        <div className="review-label">Human review + next direction ↗</div>
      </div>

      <figcaption className="hive-stats" id="coordination-graph-summary">
        <div><strong>Human</strong><span>controls direction</span></div>
        <div><strong>Cottage</strong><span>coordinates shared state</span></div>
        <div><strong>Agents</strong><span>own execution</span></div>
      </figcaption>
    </figure>
  );
}
