"""Estimated reachability over the observed tile map: the next landing spot toward a goal.

A deterministic estimate, not a learned or verified route (that is the world graph's job,
docs/graph.md). Cells come only from tiles observed this episode; unseen cells are blocked.
Moves from a standable cell:

- walk one column along a floor, or walk off an edge and fall to the floor below;
- the catalog's jump shapes (straight up, direction held for ``short_hold_ticks``, direction
  held until landing), simulated tick by tick with the measured jump arc, 1 px/tick air control
  and 1 px/tick free fall (``ReachConfig``; measurements in configs/skills.yaml). Walls use the
  measured body box, ground support the measured foot points. Flights touching a hazard, or falling off
  the bottom of the map, are dropped.

The result only sets the goal's waypoint. The tactical model still chooses every skill. The
simulation is checked against the real game in tests/integration/test_dave_reach.py.
"""

from __future__ import annotations

import heapq
from collections.abc import Iterable, Mapping

from dave_agent.config import ReachConfig, SkillSpec

Cell = tuple[int, int]  # (col, row); row 0 is the top
TILE = 16
BLOCKING = frozenset({"solid"})
DEADLY = frozenset({"hazard"})
MAX_FLIGHT_TICKS = 400


class ReachMap:
    def __init__(self, cells: Mapping[Cell, str], cfg: ReachConfig) -> None:
        """``cells`` maps every observed cell to its tile kind ("empty" when nothing is there)."""
        self.cells, self.cfg = cells, cfg
        self.max_row = max((r for _, r in cells), default=0)
        self._moves: dict[Cell, list[tuple[Cell, str, float]]] = {}

    # -- cells -------------------------------------------------------------------------------
    def passable(self, cell: Cell) -> bool:
        kind = self.cells.get(cell)
        return kind is not None and kind not in BLOCKING and kind not in DEADLY

    def standable(self, cell: Cell) -> bool:
        col, row = cell
        return self.passable(cell) and self.cells.get((col, row + 1)) in BLOCKING

    def _box_cells(self, x: int, y: int) -> set[Cell]:
        left, right = self.cfg.body_px
        cols = {(x + left) // TILE, (x + right) // TILE}
        rows = {y // TILE, (y + TILE - 1) // TILE}
        return {(c, r) for c in cols for r in rows}

    def _blocked(self, x: int, y: int) -> bool:
        return any(self.cells.get(c) is None or self.cells[c] in BLOCKING for c in self._box_cells(x, y))

    def _deadly(self, x: int, y: int) -> bool:
        return any(self.cells.get(c) in DEADLY for c in self._box_cells(x, y))

    def _supported(self, x: int, y: int) -> bool:
        return self._ground(x, y) is not None

    def _ground(self, x: int, y: int) -> Cell | None:
        """The cell Dave stands in when his feet rest on ground at pixel (x, y): the column of the
        first foot point with solid ground under it. None when not standing."""
        if y % TILE:
            return None
        row = y // TILE + 1
        for f in self.cfg.foot_px:
            col = (x + f) // TILE
            if self.cells.get((col, row)) in BLOCKING and self.passable((col, row - 1)):
                return (col, row - 1)
        return None

    def locate(self, x: int, y: int) -> Cell | None:
        """The standable cell for Dave standing at pixel (x, y), or None."""
        cell = self._ground(x, y)
        return cell if cell is not None and self.standable(cell) else None

    # -- simulation --------------------------------------------------------------------------
    def fly(self, start: Cell, direction: int, hold: int | None) -> Cell | None:
        """Landing cell of a jump from standing at ``start``: direction held for ``hold`` ticks
        (None: until landing; 0 or direction 0: straight up). None when it lands nowhere safe."""
        arc, cfg = self.cfg.arc_px, self.cfg
        x, y0 = start[0] * TILE, start[1] * TILE
        y, peak, falling = y0, 0, False
        for t in range(1, MAX_FLIGHT_TICKS):
            if direction and (hold is None or t <= hold):
                for _ in range(cfg.air_px_per_tick):
                    if not self._blocked(x + direction, y):
                        x += direction
            if not falling and t < len(arc):
                target = y0 - arc[t]
                step = -1 if target < y else 1
                while y != target:
                    if step < 0 and self._blocked(x, y - 1):
                        falling = True  # head hit the tile above: fall from here
                        break
                    if step > 0 and self._supported(x, y):
                        return self._ground(x, y)
                    y += step
                peak = max(peak, y0 - y)
            else:
                falling = True
                for _ in range(cfg.fall_px_per_tick):
                    if self._supported(x, y):
                        return self._ground(x, y)
                    y += 1
            if self._deadly(x, y) or y > (self.max_row + 1) * TILE:
                return None
            descending = falling or (t < len(arc) and arc[t] <= arc[t - 1] and t > 1)
            if descending and self._supported(x, y) and _cell(x, y) != start:
                return self._ground(x, y)
        return None

    def moves(self, cell: Cell) -> list[tuple[Cell, str, float]]:
        """(landing cell, kind, cost) for every estimated move from a standable cell."""
        if cell in self._moves:
            return self._moves[cell]
        out: dict[Cell, tuple[str, float]] = {}
        col, row = cell
        for d in (-1, 1):
            nxt = (col + d, row)
            if not self.passable(nxt):
                continue
            if self.standable(nxt):
                out.setdefault(nxt, ("walk", 1.0))
                continue
            r = row + 1  # walked off an edge: fall straight down
            while self.passable((col + d, r)) and not self.standable((col + d, r)):
                r += 1
            if self.standable((col + d, r)):
                out.setdefault((col + d, r), ("fall", 1.0 + 0.1 * (r - row)))
        for d, hold in ((0, 0), (-1, self.cfg.short_hold_ticks), (1, self.cfg.short_hold_ticks), (-1, None), (1, None)):
            land = self.fly(cell, d, hold)
            if land is not None and land != cell and self.standable(land):
                cost = 2.0 + 0.1 * abs(land[0] - col)
                if land not in out or out[land][1] > cost:
                    out[land] = ("jump", cost)
        self._moves[cell] = sorted((c, k, w) for c, (k, w) in out.items())
        return self._moves[cell]

    # -- search ------------------------------------------------------------------------------
    def path(self, start: Cell, targets: set[Cell]) -> list[tuple[Cell, str]] | None:
        """Cheapest move sequence from ``start`` to any target: [(cell, move kind), ...], or None."""
        if not self.standable(start):
            return None
        if start in targets:
            return []
        best: dict[Cell, float] = {start: 0.0}
        back: dict[Cell, tuple[Cell, str]] = {}
        queue = [(0.0, start)]
        while queue:
            cost, cell = heapq.heappop(queue)
            if cell in targets:
                out = []
                while cell != start:
                    prev, kind = back[cell]
                    out.append((cell, kind))
                    cell = prev
                return out[::-1]
            if cost > best[cell]:
                continue
            for nxt, kind, step in self.moves(cell):
                if cost + step < best.get(nxt, float("inf")):
                    best[nxt], back[nxt] = cost + step, (cell, kind)
                    heapq.heappush(queue, (cost + step, nxt))
        return None

    def targets_for(self, goal: Cell) -> set[Cell]:
        """Cells to stand on to take something at ``goal``: the goal cell itself if standable,
        else standable cells up to 2 rows below it (a jump touches it)."""
        if self.standable(goal):
            return {goal}
        col, row = goal
        return {(col, r) for r in range(row + 1, row + 3) if self.standable((col, r))}


def _cell(x: int, y: int) -> Cell:
    return ((x + TILE // 2) // TILE, (y + TILE // 2) // TILE)


def next_waypoint(reach: ReachMap, start: Cell, goal: Cell) -> Cell | None:
    """The landing cell of the first jump or fall on the estimated path to ``goal`` (walking
    needs no waypoint), the goal itself when only walking is left, or None when no path is known."""
    route = reach.path(start, reach.targets_for(goal))
    if route is None:
        return None
    for cell, kind in route:
        if kind != "walk":
            return cell
    return goal


DIRECTIONS = {"left": -1, "right": 1}


def estimate_end(reach: ReachMap, cell: Cell, spec: SkillSpec) -> Cell | None:
    """Estimated standing cell after running ``spec`` from standing at ``cell``, read from the
    skill's own phases: a jump (Up in its first phase) is flown with the direction held for its
    fixed-tick phases or until landing; a walk moves ``walk_px_per_3_ticks`` along the floor and
    falls off edges; anything else ends where it started. None when it lands nowhere safe."""
    phases = spec.phases
    if "jump" in phases[0].buttons:
        direction, hold = 0, 0
        for phase in phases[1:]:
            d = next((DIRECTIONS[b] for b in phase.buttons if b in DIRECTIONS), 0)
            if d:
                direction = d
                hold = None if phase.until is not None else hold + (phase.ticks or 0)
        return reach.fly(cell, direction, hold)
    direction = next((DIRECTIONS[b] for p in phases for b in p.buttons if b in DIRECTIONS), 0)
    if not direction or len(phases) != 1 or phases[0].ticks is None:
        return cell
    px = phases[0].ticks * reach.cfg.walk_px_per_3_ticks // 3
    col, row = cell
    for _ in range(round(px / TILE)):
        nxt = (col + direction, row)
        if not reach.passable(nxt):
            break  # wall: stops here
        col += direction
        if not reach.standable((col, row)):
            r = row + 1  # walked off an edge: fall straight down
            while reach.passable((col, r)) and not reach.standable((col, r)):
                r += 1
            return (col, r) if reach.standable((col, r)) else None
    return (col, row)


def iter_jumps(reach: ReachMap, cell: Cell) -> Iterable[tuple[str, Cell | None]]:
    """Every catalog jump shape from ``cell`` and its estimated landing (for checks and docs)."""
    short = reach.cfg.short_hold_ticks
    for name, d, hold in (("jump_up", 0, 0), ("jump_left_short", -1, short), ("jump_right_short", 1, short),
                          ("jump_left", -1, None), ("jump_right", 1, None)):
        yield name, reach.fly(cell, d, hold)
