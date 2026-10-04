// Pure helpers shared by server and client components (no Node APIs here).
import type { Tile } from "./bundle";

export const TILE_PX = 16;

// The tactical request's grid legend (models/tactical.py GRID_LEGEND).
export const CELL: Record<string, { label: string; className: string }> = {
  "#": { label: "solid", className: "cell-solid" },
  X: { label: "hazard (sets Dave burning)", className: "cell-hazard" },
  $: { label: "loot", className: "cell-loot" },
  T: { label: "trophy", className: "cell-trophy" },
  D: { label: "door", className: "cell-door" },
  "|": { label: "climbable", className: "cell-climb" },
  I: { label: "gun / jetpack", className: "cell-item" },
  M: { label: "monster", className: "cell-monster" },
  "*": { label: "plasma or bullet", className: "cell-shot" },
  "@": { label: "Dave", className: "cell-dave" },
  ".": { label: "empty", className: "cell-empty" },
};

/** Grid position (in cells, fractional) of the centre of Dave's sprite at pixel (x, y). */
export function pxToCell([x, y]: Tile): Tile {
  return [(x + TILE_PX / 2) / TILE_PX, (y + TILE_PX / 2) / TILE_PX];
}

/** The reach estimate appended to a candidate description, if any. */
export function estimateOf(description: string): { tile: Tile | null; note: string } | null {
  const m = description.match(/estimated end tile \[(-?\d+), (-?\d+)\]/);
  if (m) return { tile: [Number(m[1]), Number(m[2])], note: description.includes("(no movement)") ? "no movement" : "" };
  if (description.includes("estimated: no safe landing")) return { tile: null, note: "no safe landing" };
  return null;
}

/** Split a candidate description into the skill text and its appended notes. */
export function splitNotes(description: string): { base: string; notes: string[] } {
  const [base, ...notes] = description.split("; ");
  return { base, notes };
}

export const sameTile = (a: Tile | null, b: Tile | null) => !!a && !!b && a[0] === b[0] && a[1] === b[1];

export const fmtTile = (t: Tile | null | undefined) => (t ? `(${t[0]},${t[1]})` : "—");

/** Indices of decisions inside back-and-forth loops: the same (tile, skill) seen at least 3 times
 * within the last 8 decisions. */
export function loopIndices(rows: { tile: Tile | null; skill: string | null }[]): Set<number> {
  const out = new Set<number>();
  rows.forEach((row, i) => {
    if (!row.tile) return;
    const window = rows.slice(Math.max(0, i - 7), i + 1);
    const same = window.filter((r) => sameTile(r.tile, row.tile) && r.skill === row.skill).length;
    if (same >= 3) out.add(i);
  });
  return out;
}
