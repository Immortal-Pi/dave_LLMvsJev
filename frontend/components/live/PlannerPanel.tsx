"use client";
import { fmtTile } from "@/lib/view";
import { goalTile, type LiveState } from "@/lib/live";
import { LevelMap } from "./LevelMap";

/** The strategic planner's view: the explored map it read, the goal it chose, the waypoints it
 * gave the engine, and why. */
export function PlannerPanel({ live }: { live: LiveState }) {
  const plan = live.plans.at(-1);
  if (!plan) {
    return (
      <section className="panel planner">
        <h2>Planner</h2>
        <p className="muted">The planner&apos;s map, goal and route appear here once a run starts.</p>
      </section>
    );
  }
  const lastDecision = [...live.feed].reverse().find((i) => i.kind === "decision");
  const heading = lastDecision?.kind === "decision" ? lastDecision.d.goal?.waypoint ?? null : null;
  const threats = lastDecision?.kind === "decision" ? lastDecision.d.threats ?? [] : [];
  const chosenPath = plan.candidates.find((c) => c.id === plan.chosen)?.path ?? null;
  const tried = plan.tried ?? [];
  const failed = plan.failed_links ?? [];
  const route = plan.route as { status?: string; steps?: number; reason?: string; incidents?: { cause: string; tile: number[] }[] } | null;
  const who = plan.calls[0]?.provider === "mock" ? "Rule planner (mock)" : plan.fallback ? "Fallback" : "LLM planner";
  return (
    <section className="panel planner">
      <h2>
        {who} <span className="muted small">frame {plan.frame} · {live.plans.length} plans this run</span>
      </h2>
      <div className="planner-grid">
        <div className="map-wrap">
          {plan.map ? (
            <LevelMap map={plan.map} dave={live.now?.tile ?? null} waypoints={plan.waypoints}
                      goal={goalTile(plan.chosen)} heading={heading} path={plan.path} platforms={plan.platforms}
                      failed={failed} deaths={plan.deaths} threats={threats} />
          ) : (
            <p className="muted">No map yet.</p>
          )}
          <p className="small muted legend-line">
            <span className="key dave" /> Dave now <span className="key path" /> planner&apos;s path
            <span className="key wp" /> waypoints <span className="key goal" /> goal <span className="key heading" />
            engine heading to <span className="key reach" /> reachable platform <span className="key failed" />
            move that failed here (stuck or died) <span className="key threat" /> a shot&apos;s path (dashed: not fired
            yet, number = ticks until it is) <span className="key monster" /> a monster&apos;s route (monsters fly through
            walls, shots stop at them)
            ✕ death <span className="key screen" /> on screen · ? not seen yet · jumps are arcs, a red dashed leg has no
            known way
          </p>
        </div>
        <dl className="facts">
          <div className="fact-row">
            <dt>Goal</dt>
            <dd className="mono">{plan.chosen}</dd>
          </div>
          <div className="fact-row">
            <dt>Why</dt>
            <dd>{plan.rationale ?? "—"}</dd>
          </div>
          <div className="fact-row">
            <dt>Triggers</dt>
            <dd>
              {plan.triggers.map((t) => (
                <span key={t} className="chip">
                  {t}
                </span>
              ))}
            </dd>
          </div>
          <div className="fact-row">
            <dt>Waypoints</dt>
            <dd>{plan.waypoints.length ? plan.waypoints.map((w, i) => `${i + 1}: ${fmtTile(w)}`).join("  →  ") : "none (engine follows its own route)"}</dd>
          </div>
          <div className="fact-row">
            <dt>Path estimate</dt>
            <dd className="mono small">{chosenPath ?? "—"}</dd>
          </div>
          <div className="fact-row">
            <dt>Engine heading to</dt>
            <dd>{fmtTile(heading)}</dd>
          </div>
          {route ? (
            <div className="fact-row">
              <dt>Learned route</dt>
              <dd>
                {route.status}
                {route.steps ? `, ${route.steps} steps` : ""}
                {route.reason ? ` (${route.reason})` : ""}
                {route.incidents?.length ? (
                  <span className="badge warn">
                    {route.incidents.length} past deaths: {route.incidents.map((i) => `${i.cause} ${fmtTile(i.tile as [number, number])}`).join(", ")}
                  </span>
                ) : null}
              </dd>
            </div>
          ) : null}
          {tried.length ? (
            <div className="fact-row">
              <dt>Tried this level</dt>
              <dd className="small">
                <ol className="tried-list">
                  {tried.map((a, i) => (
                    <li key={i}>
                      <span className="mono">{a.goal}</span>{" "}
                      <span className={`outcome-${a.outcome}`}>{a.outcome}</span>
                      {a.reason ? ` (${a.reason})` : ""}
                      {a.waypoints_reached ? ` · waypoints ${a.waypoints_reached}` : ""}
                      {a.furthest ? ` · got to ${fmtTile(a.furthest)}` : ""}
                    </li>
                  ))}
                </ol>
              </dd>
            </div>
          ) : null}
          {failed.length ? (
            <div className="fact-row">
              <dt>Failed moves</dt>
              <dd className="small">
                {failed.map((f, i) => (
                  <span key={i} className={f.avoid ? "badge warn" : "chip"}>
                    {fmtTile(f.from)} → {fmtTile(f.to)} ×{f.times} ({f.how.join(", ")}){f.avoid ? " avoid" : ""}
                  </span>
                ))}
              </dd>
            </div>
          ) : null}
          {plan.errors.length ? (
            <div className="fact-row">
              <dt>Rejected</dt>
              <dd className="small">{plan.errors.join("; ")}</dd>
            </div>
          ) : null}
          <div className="fact-row">
            <dt>Options</dt>
            <dd className="small">
              {plan.candidates.map((c) => (
                <span key={c.id} className={c.id === plan.chosen ? "chip on" : "chip"} title={c.description}>
                  {c.id}
                </span>
              ))}
            </dd>
          </div>
        </dl>
      </div>
    </section>
  );
}
