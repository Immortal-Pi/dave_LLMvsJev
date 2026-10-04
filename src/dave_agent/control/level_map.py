"""The explored level map the planner reads: what a human player has seen of this level.

Built from every cell observed on the current level this episode (the goal manager's cell
memory), so it holds the current screen plus every screen seen before, never the parts of the
level not yet scrolled into view (``?``). Same legend as the tactical view
(models/tactical.py), plus ``?`` unseen and ``1``..``5`` the planner's waypoints. Dave and the
visible monsters and shots are drawn from the current observation. Identical for every arm.
"""

from __future__ import annotations

from typing import Any

from dave_agent.memory.working import player_tile
from dave_agent.models.tactical import EMPTY_CHAR, GRID_LEGEND, MONSTER_CHAR, PLAYER_CHAR, SHOT_CHAR, SHOT_TYPES, \
    TILE_CHARS
from dave_agent.schemas import Observation

UNSEEN_CHAR = "?"
MAP_LEGEND = f"{GRID_LEGEND}, ? not seen yet, 1-5 your waypoints"


def render(cells: dict[tuple[int, int], str], obs: Observation,
           waypoints: tuple[tuple[int, int], ...] = ()) -> dict[str, Any] | None:
    """{origin, col_ruler, rows, screen_cols, legend}: one string per map row, "RR " (the row
    number) then the cells from column ``origin[0]``; the two ruler lines give each column's
    tens and units digit. None before anything was observed."""
    if not cells:
        return None
    lo = min(c for c, _ in cells)
    hi = max(c for c, _ in cells)
    rows = max(r for _, r in cells) + 1
    grid = [[UNSEEN_CHAR] * (hi - lo + 1) for _ in range(rows)]

    def put(col: int, row: int, char: str) -> None:
        if lo <= col <= hi and 0 <= row < rows:
            grid[row][col - lo] = char

    for (col, row), kind in cells.items():
        put(col, row, TILE_CHARS.get(kind, EMPTY_CHAR))
    for i, (col, row) in enumerate(waypoints[:5]):
        put(col, row, str(i + 1))
    for e in obs.entities:
        if e.visible:
            t = player_tile(e.position)
            put(t.col, t.row, SHOT_CHAR if e.entity_type in SHOT_TYPES else MONSTER_CHAR)
    if obs.player_position is not None:
        t = player_tile(obs.player_position)
        put(t.col, t.row, PLAYER_CHAR)
    cols = range(lo, hi + 1)
    # Column rulers (tens and units digits) so a tile can be read off as (col, row index).
    ruler = ["   " + "".join(str(c // 10 % 10) for c in cols), "   " + "".join(str(c % 10) for c in cols)]
    return {"origin": [lo, 0], "col_ruler": ruler, "rows": [f"{r:02d} {''.join(g)}" for r, g in enumerate(grid)],
            "screen_cols": [obs.region.min.col, obs.region.max.col], "legend": MAP_LEGEND}
