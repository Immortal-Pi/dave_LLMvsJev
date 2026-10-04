import { CELL } from "@/lib/view";
import type { FailedLink, LevelMapView, PathStep, PlatformView, ThreatPath, Tile } from "@/lib/live";

const C = 12; // px per cell in the SVG's own units (it scales to the panel width)

/** The explored level map the planner read (control/level_map.py): every cell seen so far,
 * unseen cells dimmed, the current screen outlined. On top:
 * - the reachable platforms (faint outline) and the moves that failed this level (red dashes);
 * - the estimated moves Dave -> waypoints -> goal (control/goals.py ``_planned_path``): walks as
 *   lines, jumps as arcs, falls as drops; a leg the estimate cannot make is red and dashed;
 * - the planner's numbered waypoints, the goal, and the waypoint the engine is heading for;
 * - deaths this episode (x) and each visible threat's predicted path (orange dashes);
 * - Dave now. */
export function LevelMap({ map, dave, waypoints, goal, heading, path = [], platforms = [], failed = [],
                           deaths = [], threats = [] }: {
  map: LevelMapView;
  dave: Tile | null;
  waypoints: Tile[];
  goal: Tile | null;
  heading: Tile | null;
  path?: PathStep[];
  platforms?: PlatformView[];
  failed?: FailedLink[];
  deaths?: { cause: string; tile: Tile | null }[];
  threats?: ThreatPath[];
}) {
  const [c0] = map.origin;
  const rows = map.rows.map((r) => r.slice(3)); // drop the "RR " row number
  const width = Math.max(...rows.map((r) => r.length));
  const at = ([c, r]: Tile) => [(c - c0) * C + C / 2, r * C + C / 2] as const;
  // Threat paths come in tile units from each threat's centre.
  const atPx = ([x, y]: [number, number]) => [(x - c0) * C, y * C] as const;
  const legs = path.slice(1).map((step, i) => {
    const [x1, y1] = at([path[i][0], path[i][1]]);
    const [x2, y2] = at([step[0], step[1]]);
    const kind = step[2];
    if (kind === "jump") {
      const top = Math.min(y1, y2) - C * 1.6;
      return { kind, d: `M${x1},${y1} Q${(x1 + x2) / 2},${top} ${x2},${y2}` };
    }
    if (kind === "fall") return { kind, d: `M${x1},${y1} L${x2},${y1} L${x2},${y2}` };
    return { kind, d: `M${x1},${y1} L${x2},${y2}` };
  });
  // Without an estimated path (no reach envelope), join the points as before.
  const straight = [dave, ...waypoints, goal].filter((t): t is Tile => !!t);
  return (
    <svg className="level-map" viewBox={`-18 -14 ${width * C + 22} ${rows.length * C + 18}`}
         width={(width * C + 22) * 2} height={(rows.length * C + 18) * 2} role="img"
         aria-label="Explored level map with the planner's path">
      {rows.map((line, r) =>
        [...line].map((ch, i) => {
          const cell = ch === "?" ? null : CELL[ch] ?? CELL["."];
          const cls = ch === "?" ? "cell-unseen" : /[1-5@*M]/.test(ch) ? "cell-empty" : cell?.className;
          return <rect key={`${r}-${i}`} x={i * C} y={r * C} width={C} height={C} className={cls} />;
        }),
      )}
      {rows.map((_, r) => (
        <text key={`r${r}`} x={-4} y={r * C + C * 0.75} className="axis" textAnchor="end">
          {r}
        </text>
      ))}
      {Array.from({ length: width }, (_, i) =>
        (c0 + i) % 5 === 0 ? (
          <text key={`c${i}`} x={i * C + C / 2} y={-4} className="axis" textAnchor="middle">
            {c0 + i}
          </text>
        ) : null,
      )}
      <rect className="screen-box" x={(map.screen_cols[0] - c0) * C} y={0}
            width={(map.screen_cols[1] - map.screen_cols[0] + 1) * C} height={rows.length * C} />
      {platforms.filter((p) => p.reachable).map((p) => (
        <rect key={p.id} className="platform-reach" x={(p.cols[0] - c0) * C + 1} y={p.row * C + 1}
              width={(p.cols[1] - p.cols[0] + 1) * C - 2} height={C - 2}>
          <title>{`${p.id}: reachable in ${p.hops ?? 0} moves`}</title>
        </rect>
      ))}
      {failed.map((f, i) => {
        const [x1, y1] = at(f.from);
        const [x2, y2] = at(f.to);
        return (
          <line key={`f${i}`} className={f.avoid ? "failed-link avoid" : "failed-link"} x1={x1} y1={y1} x2={x2} y2={y2}>
            <title>{`failed ${f.times}x: ${f.how.join(", ")}`}</title>
          </line>
        );
      })}
      {legs.length
        ? legs.map((l, i) => <path key={`p${i}`} className={l.kind === "unknown" ? "plan-path unknown" : "plan-path"} d={l.d} />)
        : straight.length > 1 ? <polyline className="plan-path" points={straight.map((t) => at(t).join(",")).join(" ")} /> : null}
      {threats.map((t) =>
        t.path.length > 1 ? (
          <polyline key={t.id} className="threat-path" points={t.path.map((p) => atPx(p).join(",")).join(" ")}>
            <title>{`${t.kind}: predicted path`}</title>
          </polyline>
        ) : null,
      )}
      {deaths.filter((d) => d.tile).map((d, i) => {
        const [x, y] = at(d.tile as Tile);
        const s = C * 0.3;
        return (
          <g key={`d${i}`} className="death-mark">
            <path d={`M${x - s},${y - s} L${x + s},${y + s} M${x + s},${y - s} L${x - s},${y + s}`} />
            <title>{`died: ${d.cause}`}</title>
          </g>
        );
      })}
      {waypoints.map((w, i) => {
        const [x, y] = at(w);
        return (
          <g key={`w${i}`} className="waypoint">
            <circle cx={x} cy={y} r={C * 0.45} />
            <text x={x} y={y + C * 0.28} textAnchor="middle">
              {i + 1}
            </text>
          </g>
        );
      })}
      {goal ? <rect className="goal-box" x={(goal[0] - c0) * C + 1} y={goal[1] * C + 1} width={C - 2} height={C - 2} /> : null}
      {heading ? <circle className="heading" cx={at(heading)[0]} cy={at(heading)[1]} r={C * 0.6} /> : null}
      {dave ? <circle className="dave-dot" cx={at(dave)[0]} cy={at(dave)[1]} r={C * 0.38} /> : null}
    </svg>
  );
}
