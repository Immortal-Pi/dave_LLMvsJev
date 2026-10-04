import type { RunDecision } from "@/lib/bundle";

const S = 16;
const PAD = 18;

/** Where decisions were taken: darker cells saw more decisions; red marks a fatal start tile. */
export function MiniMap({ decisions }: { decisions: RunDecision[] }) {
  const counts = new Map<string, { n: number; fatal: boolean }>();
  for (const d of decisions) {
    if (!d.tile) continue;
    const key = d.tile.join(",");
    const c = counts.get(key) ?? { n: 0, fatal: false };
    counts.set(key, { n: c.n + 1, fatal: c.fatal || d.fatal });
  }
  if (!counts.size) return null;
  const tiles = [...counts.keys()].map((k) => k.split(",").map(Number));
  const cols = Math.max(20, ...tiles.map((t) => t[0] + 1));
  const rows = Math.max(...tiles.map((t) => t[1] + 1)) + 1;
  const peak = Math.max(...[...counts.values()].map((c) => c.n));
  return (
    <section className="panel">
      <h2>Where it decided</h2>
      <svg className="minimap" viewBox={`0 0 ${PAD + cols * S + 2} ${PAD + rows * S + 2}`} role="img" aria-label="Decision heat map">
        {Array.from({ length: cols }, (_, c) => (
          <text key={`c${c}`} x={PAD + c * S + S / 2} y={12} className="axis" textAnchor="middle">
            {c}
          </text>
        ))}
        {Array.from({ length: rows }, (_, r) => (
          <text key={`r${r}`} x={PAD - 4} y={PAD + r * S + S / 2 + 3} className="axis" textAnchor="end">
            {r}
          </text>
        ))}
        {Array.from({ length: rows }, (_, r) =>
          Array.from({ length: cols }, (_, c) => {
            const v = counts.get(`${c},${r}`);
            return (
              <rect key={`${c}-${r}`} x={PAD + c * S + 1} y={PAD + r * S + 1} width={S - 1} height={S - 1}
                    className={v?.fatal ? "mm fatal" : v ? "mm hit" : "mm"}
                    style={v ? { opacity: 0.3 + (0.7 * v.n) / peak } : undefined}>
                <title>{`(${c},${r})${v ? `: ${v.n} decisions${v.fatal ? ", a fatal one" : ""}` : ""}`}</title>
              </rect>
            );
          }),
        )}
      </svg>
      <p className="muted">
        Tile (col, row) of each decision. Darker means more decisions; red means a decision there was fatal. Hover a
        cell for its count.
      </p>
    </section>
  );
}
