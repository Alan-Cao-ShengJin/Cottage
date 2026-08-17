export default function CoordinationGraph() {
  return (
    <figure className="hive-card" aria-labelledby="coordination-graph-title" aria-describedby="coordination-graph-summary">
      <div className="hive-card-header">
        <div id="coordination-graph-title"><span className="live-dot" /> Human-in-the-loop workflow</div>
        <span>Steer → build → review</span>
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
          <path className="compact-path review-flow" markerEnd="url(#flow-arrow)" d="M525 568 C660 548 684 122 561 70" />
        </svg>

        <div className="compact-layer-label compact-human-label"><span>01</span><b>Steer</b></div>
        <div className="compact-person person-one">
          <span className="human-glyph" aria-hidden="true"><i /></span>
          <div><strong>Alex</strong><small>Product lead · Human</small></div>
        </div>
        <div className="compact-person person-two">
          <span className="human-glyph" aria-hidden="true"><i /></span>
          <div><strong>Maya</strong><small>Engineering lead · Human</small></div>
        </div>
        <div className="handoff-label human-handoff handoff-one"><b>Goal ↓</b><span>Review ↑</span></div>
        <div className="handoff-label human-handoff handoff-two"><b>Goal ↓</b><span>Review ↑</span></div>

        <div className="compact-layer-label compact-room-label"><span>02</span><b>Coordinate</b></div>
        <div className="compact-room">
          <div className="compact-room-title">
            <div><span className="room-live-dot" /><strong>Cottage room</strong><small>Shared state</small></div>
            <em>Live</em>
          </div>
          <div className="compact-supervisors">
            <div className="compact-supervisor supervisor-one"><span>AI</span><div><strong>Claude</strong><small>Supervisor · Alex&apos;s</small></div></div>
            <div className="compact-supervisor supervisor-two"><span>AI</span><div><strong>Codex</strong><small>Supervisor · Maya&apos;s</small></div></div>
          </div>
          <div className="room-exchange"><span className="exchange-packet">Plan</span><b>plan · claim · sync</b><span className="exchange-packet exchange-packet-two">Claim</span></div>
        </div>

        <div className="compact-layer-label compact-worker-label"><span>03</span><b>Delegate</b></div>
        <div className="task-brief task-brief-one">Tasks ↓</div>
        <div className="task-brief task-brief-two">Tasks ↓</div>
        <div className="compact-workers">
          <div><span>AI</span><strong>Research</strong></div>
          <div><span>AI</span><strong>Content</strong></div>
          <div><span>AI</span><strong>Backend</strong></div>
          <div><span>AI</span><strong>Testing</strong></div>
        </div>

        <div className="assembly-label"><span>04</span> Merge</div>
        <div className="end-product">
          <span className="product-preview" aria-hidden="true"><i /><i /><i /></span>
          <div><strong>End product</strong><small>Ready for review</small></div>
          <b className="product-ready">4 outputs ✓</b>
        </div>
        <div className="review-label">Review ↗</div>
      </div>

      <figcaption className="hive-stats" id="coordination-graph-summary">
        <div><strong>You</strong><span>steer</span></div>
        <div><strong>Cottage</strong><span>syncs</span></div>
        <div><strong>Agents</strong><span>build</span></div>
      </figcaption>
    </figure>
  );
}
