"use client";
import { fmtTile } from "@/lib/view";
import type { GraphEdge, GraphNode, LiveState, Tile } from "@/lib/live";

const C = 12; // px per cell in the SVG's own units, as in LevelMap

/** Edge tone: red once a move has killed Dave, green when it usually works, amber otherwise. */
function tone(e: GraphEdge): "ok" | "warn" | "danger" {
  if (e.fatal > 0) return "danger";
  return e.p >= 0.75 ? "ok" : "warn";
}

function onNode(n: GraphNode, tile: Tile | null): boolean {
  return !!tile && tile[1] === n.row && n.col_min - 1 <= tile[0] && tile[0] <= n.col_max + 1;
}

/** The learned world graph of the current level (memory/graph.py), drawn where the platforms
 * are: each node is a platform segment at its row and columns, each edge a skill move Dave really
 * made from one platform to another, coloured by how it went and thicker the more it was tried. */
function GraphDrawing({ nodes, edges, dave, fresh, seq }: {
  nodes: GraphNode[];
  edges: GraphEdge[];
  dave: Tile | null;
  fresh: [string, string, string] | null;
  seq: number;
}) {
  const c0 = Math.min(...nodes.map((n) => n.col_min)) - 1;
  const c1 = Math.max(...nodes.map((n) => n.col_max)) + 2;
  const r0 = Math.max(0, Math.min(...nodes.map((n) => n.row)) - 2);
  const r1 = Math.max(...nodes.map((n) => n.row)) + 2;
  const byId = new Map(nodes.map((n) => [n.id, n]));
  const y = (row: number) => (row - r0) * C + C * 0.85; // a platform's top surface
  const centre = (n: GraphNode) => ((n.col_min + n.col_max + 1) / 2 - c0) * C;
  // Moves between the same two platforms fan out, and A->B bends away from B->A.
  const seen = new Map<string, number>();
  const width = (c1 - c0) * C;
  const height = (r1 - r0) * C;
  return (
    <svg className="graph-map" viewBox={`-20 -6 ${width + 26} ${height + 12}`} width={(width + 26) * 2}
         height={(height + 12) * 2} role="img" aria-label="Learned world graph of this level">
      <defs>
        {(["ok", "warn", "danger", "fresh"] as const).map((t) => (
          <marker key={t} id={`arrow-${t}`} viewBox="0 0 8 8" refX="7" refY="4" markerWidth="6" markerHeight="6"
                  markerUnits="userSpaceOnUse" orient="auto-start-reverse">
            <path d="M0,0 L8,4 L0,8 z" className={`arrow-${t}`} />
          </marker>
        ))}
      </defs>
      {Array.from({ length: r1 - r0 }, (_, i) => r0 + i).map((row) =>
        row % 2 === 0 ? (
          <text key={row} x={-4} y={y(row)} className="axis" textAnchor="end">
            {row}
          </text>
        ) : null,
      )}
      {nodes.map((n) => (
        <g key={n.id} className={`gnode${n.visited ? " visited" : ""}${onNode(n, dave) ? " here" : ""}`}>
          <title>
            {`${n.id}: row ${n.row}, cols ${n.col_min}-${n.col_max}${n.visited ? ", visited" : ", seen only"}` +
              `\nstayed ${n.stays}, inconclusive ${n.inconclusive}, failed from here ${n.failed}` +
              (n.items.length ? `\nitems: ${n.items.map((i) => `${i.kind}@${i.col}`).join(", ")}` : "") +
              (n.incidents.length ? `\ndeaths: ${n.incidents.map((i) => `${i.cause} ${fmtTile(i.tile)} (${i.skill})`).join(", ")}` : "") +
              creditLines(n)}
          </title>
          <rect x={(n.col_min - c0) * C} y={y(n.row) - 2} width={(n.col_max - n.col_min + 1) * C} height={4} rx={2}
                className="gnode-bar" strokeDasharray={n.open_left || n.open_right ? "3 2" : undefined} />
          {n.items.map((i, k) => (
            <circle key={k} cx={(i.col - c0 + 0.5) * C} cy={y(n.row) - 6} r={2} className={`gitem ${i.kind}`} />
          ))}
          {n.incidents.length ? (
            <text x={(n.col_min - c0) * C - 2} y={y(n.row) + 1} className="gdeath" textAnchor="end">
              ✕{n.incidents.length > 1 ? n.incidents.length : ""}
            </text>
          ) : null}
        </g>
      ))}
      {edges.map((e) => {
        const a = byId.get(e.source);
        const b = byId.get(e.target);
        if (!a || !b) return null;
        const pair = [e.source, e.target].sort().join("|");
        const k = seen.get(pair) ?? 0;
        seen.set(pair, k + 1);
        const [x1, y1, x2, y2] = [centre(a), y(a.row) - 2, centre(b), y(b.row) - 2];
        const dx = x2 - x1;
        const dy = y2 - y1;
        const len = Math.hypot(dx, dy) || 1;
        const bend = (e.source < e.target ? 1 : -1) * (10 + 6 * k);
        const [mx, my] = [(x1 + x2) / 2 - (dy / len) * bend, (y1 + y2) / 2 + (dx / len) * bend - 6];
        const isFresh = !!fresh && fresh[0] === e.source && fresh[1] === e.target && fresh[2] === e.key;
        const t = tone(e);
        return (
          <path key={`${e.source}>${e.target}>${e.key}${isFresh ? `#${seq}` : ""}`}
                d={`M${x1},${y1} Q${mx},${my} ${x2},${y2}`}
                className={`gedge ${t}${isFresh ? " fresh" : ""}`}
                strokeWidth={1 + Math.min(3, Math.log2(e.attempts + 1))}
                markerEnd={`url(#arrow-${isFresh ? "fresh" : t})`}>
            <title>
              {`${e.skill}${e.inventory_context.length ? ` holding ${e.inventory_context.join(", ")}` : ""}: ` +
                `${e.source} → ${e.target}\n${e.successes}/${e.attempts} ok, ${e.fatal} fatal, ` +
                `p ${e.p.toFixed(2)}, ~${Math.round(e.frames)} frames`}
            </title>
          </path>
        );
      })}
    </svg>
  );
}

/** A platform's goal credit for the tooltip: per skill and target, goals reached / goals tried. */
function creditLines(n: GraphNode): string {
  const entries = Object.entries(n.credit ?? {});
  if (!entries.length) return "";
  return "\ngoal credit:" + entries
    .sort((a, b) => b[1].goals - a[1].goals)
    .slice(0, 8)
    .map(([key, c]) => {
      const [skill, target] = key.split("|");
      return `\n  ${skill} toward ${target}: ${c.reached}/${c.goals} goals reached, ${c.closer}/${c.tries} tries closer`;
    })
    .join("");
}

const RECORDED: Record<string, string> = {
  success: "a move to another platform (edge added or strengthened)",
  failure_edge: "a failed attempt on a known move",
  failure_node: "a failed attempt with no known target (kept on the platform)",
  stay: "stayed on the same platform",
  inconclusive: "ended in the air or off the map",
  unanchored: "started off any mapped platform",
  level_changed: "changed level (never an edge)",
};

/** Arm C's learned world graph, live. Arms without a graph get a note instead. */
export function GraphPanel({ live }: { live: LiveState }) {
  const run = live.run;
  if (run && !run.arm_config.graph_enabled) {
    return (
      <section className="panel graph">
        <h2>Learned graph</h2>
        <p className="muted">
          Arm {run.arm} has no learned graph (graph_enabled is false). Choose arm C to watch it learn.
        </p>
      </section>
    );
  }
  const event = live.graph;
  const g = event?.graph;
  if (!event || !g || !g.nodes.length) {
    return (
      <section className="panel graph">
        <h2>Learned graph</h2>
        <p className="muted">
          {run ? "No platforms mapped on this level yet." : "Arm C's learned graph of the level appears here once a run starts."}
        </p>
      </section>
    );
  }
  const risky = [...g.edges].sort((a, b) => b.fatal - a.fatal || b.attempts - a.attempts).slice(0, 12);
  // The active goal's target: each move's credit toward it from its start platform.
  const lastDecision = [...live.feed].reverse().find((i) => i.kind === "decision");
  const target = lastDecision?.kind === "decision" ? lastDecision.d.goal?.target ?? null : null;
  const nodes = new Map(g.nodes.map((n) => [n.id, n]));
  const creditOf = (e: GraphEdge) => (target ? nodes.get(e.source)?.credit?.[`${e.skill}|${target}`] : undefined);
  const c = g.counts;
  return (
    <section className="panel graph">
      <h2>
        Learned graph{" "}
        <span className="muted small">
          {g.level_id} · {event.execution_mode === "real_time" ? "real-time memory" : "paused memory"} ·{" "}
          {event.source === "learning" ? "learning this run" : "frozen checkpoint"} · topology v{g.topology_version}
        </span>
      </h2>
      <div className="planner-grid">
        <div className="map-wrap">
          <GraphDrawing nodes={g.nodes} edges={g.edges} dave={live.now?.tile ?? null} fresh={event.last?.edge ?? null}
                        seq={event.seq} />
          <p className="small muted legend-line">
            <span className="key gn-visited" /> platform Dave stood on <span className="key gn-seen" /> platform seen only
            <span className="key gn-here" /> Dave now · edges: <span className="key ge-ok" /> usually works (p ≥ 0.75)
            <span className="key ge-warn" /> mixed <span className="key ge-danger" /> killed Dave at least once
            <span className="key ge-fresh" /> just learned · thicker = tried more · ✕ deaths · dashed = runs past the screen
            · hover for details
          </p>
        </div>
        <dl className="facts">
          <div className="fact-row">
            <dt>Platforms</dt>
            <dd>
              {c.nodes} ({c.visited_nodes} visited)
            </dd>
          </div>
          <div className="fact-row">
            <dt>Moves</dt>
            <dd>
              {c.edges} edges · {c.attempts} attempts · {c.successes} ok ·{" "}
              <span className={c.fatal ? "fatal-count" : ""}>{c.fatal} fatal</span>
            </dd>
          </div>
          <div className="fact-row">
            <dt>Not on an edge</dt>
            <dd>
              {c.node_failures} failures with no known target · {c.unanchored} unanchored
            </dd>
          </div>
          {event.last ? (
            <div className="fact-row">
              <dt>Last skill</dt>
              <dd>
                <span className="mono">{event.last.skill}</span>: {RECORDED[event.last.recorded ?? ""] ?? event.last.recorded}
              </dd>
            </div>
          ) : null}
          <div className="fact-row">
            <dt>Riskiest moves</dt>
            <dd>
              <table className="edge-table small">
                <thead>
                  <tr>
                    <th>skill</th>
                    <th>from → to</th>
                    <th>ok</th>
                    <th>fatal</th>
                    <th>p</th>
                    <th title={target ? `past goals for ${target} that used this move from its platform` : "no active goal"}>
                      led to goal
                    </th>
                  </tr>
                </thead>
                <tbody>
                  {risky.map((e) => (
                    <tr key={`${e.source}>${e.target}>${e.key}`} className={e.fatal ? "risky" : undefined}>
                      <td className="mono">{e.skill}</td>
                      <td className="mono">
                        {e.source.split(":").slice(1).join(":")} → {e.target.split(":").slice(1).join(":")}
                      </td>
                      <td>
                        {e.successes}/{e.attempts}
                      </td>
                      <td>{e.fatal}</td>
                      <td>{e.p.toFixed(2)}</td>
                      <td>
                        {(() => {
                          const c = creditOf(e);
                          return c ? `${c.reached}/${c.goals}` : "—";
                        })()}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
              {g.edges.length > risky.length ? <span className="muted small">{g.edges.length - risky.length} more (hover the drawing)</span> : null}
            </dd>
          </div>
        </dl>
      </div>
    </section>
  );
}
