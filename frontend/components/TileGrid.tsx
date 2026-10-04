import type { Candidate, Outcome, TacticalRequest, Tile } from "@/lib/bundle";
import { CELL, estimateOf, pxToCell } from "@/lib/view";

const S = 26; // px per tile in the drawing
const PAD = 22; // room for the axis labels
const GLYPHS = new Set(["T", "D", "$", "M", "*", "I", "|"]);

interface Props {
  request: TacticalRequest;
  outcomes: Outcome[] | null;
  selected: string | null;
  chosen: string;
}

/** The local view exactly as the request encodes it (view.rows from view.origin), with Dave's
 * pixel position, the goal waypoint, and for the selected candidate its estimated end tile
 * (dashed) versus where it really went (solid path; red when fatal). */
export function TileGrid({ request, outcomes, selected, chosen }: Props) {
  const { origin, rows } = request.view;
  const width = Math.max(...rows.map((r) => r.length));
  const height = rows.length;
  const at = (col: number, row: number) => [PAD + (col - origin[0]) * S, PAD + (row - origin[1]) * S] as const;
  // Fractional tile coordinates (pxToCell: the sprite centre / 16) -> drawing coordinates.
  const centre = (c: Tile) => at(c[0], c[1]);
  const waypoint = request.goal?.waypoint ?? null;
  const inView = (t: Tile) => t[0] >= origin[0] && t[0] < origin[0] + width && t[1] >= origin[1] && t[1] < origin[1] + height;
  const candidates = new Map<string, Candidate>(request.candidates.map((c) => [c.id, c]));
  const shown = (outcomes ?? []).filter((o) => o.candidate_id === (selected ?? chosen));
  const daveCell = request.player.px ? pxToCell(request.player.px) : null;

  return (
    <svg
      className="tile-grid"
      viewBox={`0 0 ${PAD + width * S + 4} ${PAD + height * S + 4}`}
      role="img"
      aria-label="The tile grid the models are given"
    >
      {Array.from({ length: width }, (_, i) => (
        <text key={`c${i}`} x={PAD + i * S + S / 2} y={14} className="axis" textAnchor="middle">
          {origin[0] + i}
        </text>
      ))}
      {rows.map((_, j) => (
        <text key={`r${j}`} x={PAD - 6} y={PAD + j * S + S / 2 + 4} className="axis" textAnchor="end">
          {origin[1] + j}
        </text>
      ))}
      {rows.map((line, j) =>
        Array.from(line).map((ch, i) => (
          <g key={`${i}-${j}`}>
            <rect x={PAD + i * S} y={PAD + j * S} width={S} height={S} className={`cell ${CELL[ch]?.className ?? "cell-empty"}`}>
              <title>{`(${origin[0] + i},${origin[1] + j}) ${CELL[ch]?.label ?? ch}`}</title>
            </rect>
            {GLYPHS.has(ch) && (
              <text x={PAD + i * S + S / 2} y={PAD + j * S + S / 2 + 5} className="glyph" textAnchor="middle">
                {ch}
              </text>
            )}
          </g>
        )),
      )}
      {waypoint && inView(waypoint) && (
        <g className="waypoint">
          <rect x={at(...waypoint)[0] + 2} y={at(...waypoint)[1] + 2} width={S - 4} height={S - 4} />
          <text x={at(...waypoint)[0] + S / 2} y={at(...waypoint)[1] - 3} textAnchor="middle">
            waypoint
          </text>
        </g>
      )}
      {shown.map((o) => {
        const est = estimateOf(candidates.get(o.candidate_id)?.description ?? "");
        const points = o.trajectory.map((p) => centre(pxToCell(p)).join(",")).join(" ");
        const end = o.end_px ? centre(pxToCell(o.end_px)) : null;
        return (
          <g key={o.candidate_id} className={o.fatal ? "path fatal" : "path"}>
            {est?.tile && inView(est.tile) && (
              <g className="estimate">
                <rect x={at(...est.tile)[0] + 4} y={at(...est.tile)[1] + 4} width={S - 8} height={S - 8} />
                <text x={at(...est.tile)[0] + S / 2} y={at(...est.tile)[1] + S + 10} textAnchor="middle">
                  estimated
                </text>
              </g>
            )}
            {points && <polyline points={points} />}
            {end && <circle cx={end[0]} cy={end[1]} r={6} className="end" />}
          </g>
        );
      })}
      {daveCell && (
        <g className="dave">
          <circle cx={centre(daveCell)[0]} cy={centre(daveCell)[1]} r={7} />
          <text x={centre(daveCell)[0]} y={centre(daveCell)[1] + 4} textAnchor="middle">
            @
          </text>
        </g>
      )}
    </svg>
  );
}
