import type { DecisionBundle } from "@/lib/bundle";
import { fmtTile } from "@/lib/view";

/** The goal the tactical request carries, and the planner choice behind it. */
export function GoalPanel({ decision }: { decision: DecisionBundle }) {
  const goal = decision.request.goal;
  const planned = decision.planner.goal;
  return (
    <section className="panel">
      <h2>Goal</h2>
      {goal ? (
        <dl className="facts">
          <dt>Goal</dt>
          <dd>
            {goal.goal_type} <span className="mono">{goal.target}</span>
          </dd>
          <dt>Waypoint</dt>
          <dd>
            <span className="mono">{fmtTile(goal.waypoint)}</span>, offset{" "}
            <span className="mono">{goal.waypoint_offset ? `[${goal.waypoint_offset.join(", ")}]` : "—"}</span>
            <span className="muted"> (+col right, +row down)</span>
          </dd>
          <dt>Frames left</dt>
          <dd>{goal.frames_left ?? "—"}</dd>
        </dl>
      ) : (
        <p className="muted">No goal.</p>
      )}
      {planned && (
        <>
          <h3>Planner (frame {planned.frame})</h3>
          <p>
            Chose <span className="mono">{planned.target_ref}</span>
            {planned.fallback ? <span className="badge warn">fallback</span> : null}
            {planned.triggers?.length ? <span className="muted"> · triggers: {planned.triggers.join(", ")}</span> : null}
          </p>
          {planned.rationale && <blockquote>{planned.rationale}</blockquote>}
          {planned.candidates && (
            <p className="muted">
              Options: {planned.candidates.map((c) => <span key={c} className={c === planned.target_ref ? "chip on" : "chip"}>{c}</span>)}
            </p>
          )}
          {planned.route && (
            <p className="muted">
              Learned route: <span className="mono wrap">{JSON.stringify(planned.route)}</span>
            </p>
          )}
        </>
      )}
    </section>
  );
}
