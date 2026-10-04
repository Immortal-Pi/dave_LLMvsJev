import { CELL } from "@/lib/view";
import type { LevelMapView, Tile } from "@/lib/live";

const C = 12; // px per cell in the SVG's own units (it scales to the panel width)

/** The explored level map the planner read (control/level_map.py): every cell seen so far,
 * unseen cells dimmed, the current screen outlined. On top: Dave now, the path the planner gave
 * the engine (Dave → waypoints → goal) and the waypoint the engine is heading for. */
export function LevelMap({ map, dave, waypoints, goal, heading }: {
  map: LevelMapView;
  dave: Tile | null;
  waypoints: Tile[];
  goal: Tile | null;
  heading: Tile | null;
}) {
  const [c0] = map.origin;
  const rows = map.rows.map((r) => r.slice(3)); // drop the "RR " row number
  const width = Math.max(...rows.map((r) => r.length));
  const at = ([c, r]: Tile) => [(c - c0) * C + C / 2, r * C + C / 2] as const;
  const path = [dave, ...waypoints, goal].filter((t): t is Tile => !!t);
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
      {path.length > 1 ? <polyline className="plan-path" points={path.map((t) => at(t).join(",")).join(" ")} /> : null}
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
