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
FAILED_MOVE_COST = 6.0  # per failure in play: about three extra jumps


class ReachMap:
    def __init__(self, cells: Mapping[Cell, str], cfg: ReachConfig,
                 failed: Mapping[tuple[Cell, Cell], int] | None = None) -> None:
        """``cells`` maps every observed cell to its tile kind ("empty" when nothing is there);
        ``failed`` counts (from, to) moves that failed in play this level: each failure adds
        ``FAILED_MOVE_COST`` to the move, so routes avoid it when there is another way but still
        use it when it is the only one (a weak executor can fail a move that is right)."""
        self.cells, self.cfg = cells, cfg
        self.failed = dict(failed or {})
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
        rows = {(y + 2) // TILE, (y + TILE - 1) // TILE}  # the game's side checks: y+2 and y+15 (dave.c)
        return {(c, r) for c in cols for r in rows}

    def _blocked(self, x: int, y: int) -> bool:
        return any(self.cells.get(c) is None or self.cells[c] in BLOCKING for c in self._box_cells(x, y))

    def _head_blocked(self, x: int, y: int) -> bool:
        """The game's top check (dave.c ``dave_collision_top``) with Dave's top at pixel y: the
        head points at x + ``head_px`` one pixel below the top."""
        return any(self.cells.get(((x + dx) // TILE, (y + 1) // TILE)) in (None, *BLOCKING)
                   for dx in self.cfg.head_px)

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
        return self.trace(start[0] * TILE, start[1] * TILE, direction, hold)[1]

    def trace(self, x: int, y0: int, direction: int, hold: int | None,
              stop_on_hazard: bool = True) -> tuple[list[tuple[int, int]], Cell | None]:
        """Dave's pixel position after every tick of a jump from standing at pixel (x, y0), and
        the landing cell (None when it lands nowhere safe). ``stop_on_hazard`` off flies through
        hazard cells, for callers that test contact with the game's smaller hazard boxes. Once the
        arc ends in the air (or a ceiling cuts it) Dave free-falls; a direction pressed during the
        fall turns him, and from then on he drifts that way with no key held (dave.c)."""
        arc, cfg = self.cfg.arc_px, self.cfg
        start = _cell(x, y0)
        drifting = False
        y, falling = y0, False
        path: list[tuple[int, int]] = []
        for t in range(1, MAX_FLIGHT_TICKS):
            held = bool(direction) and (hold is None or t <= hold)
            if held:
                for _ in range(cfg.air_px_per_tick):
                    if not self._blocked(x + direction, y):
                        x += direction
            if not falling and t < len(arc):
                target = y0 - arc[t]
                step = -1 if target < y else 1
                while y != target:
                    # The game tests the head 2 px ahead when a direction is held and only stops the
                    # jump when that is blocked too, so a jump slips past a ledge corner.
                    if step < 0 and self._head_blocked(x, y - 1) and (
                            not held or self._head_blocked(x + 2 * direction, y - 1)):
                        falling = True  # head hit the tile above: fall from here
                        break
                    if step > 0 and self._supported(x, y):
                        path.append((x, y))
                        return path, self._ground(x, y)
                    y += step
            else:
                falling = True
                drifting = drifting or held
                if drifting and not held:
                    for _ in range(cfg.air_px_per_tick):
                        if not self._blocked(x + direction, y):
                            x += direction
                for _ in range(cfg.fall_px_per_tick):
                    if self._supported(x, y):
                        path.append((x, y))
                        return path, self._ground(x, y)
                    y += 1
            path.append((x, y))
            if (stop_on_hazard and self._deadly(x, y)) or y > (self.max_row + 1) * TILE:
                return path, None
            descending = falling or (t < len(arc) and arc[t] <= arc[t - 1] and t > 1)
            if descending and self._supported(x, y) and _cell(x, y) != start:
                return path, self._ground(x, y)
        return path, None

    def walk(self, x: int, y: int, direction: int, ticks: int, facing: int = 0) -> list[tuple[int, int]]:
        """Dave's pixel position after every tick of walking ``ticks`` ticks (``walk_px_per_3_ticks``
        along the floor, stopped by walls). Off an edge he falls 1 px/tick and moves
        ``air_px_per_tick`` the way he faces: a held key turns him, and once turned he keeps
        drifting with no key held (dave.c freefalling). ``facing`` (-1, 0 or 1) is a drift
        already under way when the walk starts."""
        path: list[tuple[int, int]] = []
        moved = 0
        drift = direction or facing
        for t in range(1, ticks + 1):
            due = t * self.cfg.walk_px_per_3_ticks // 3
            airborne = not self._supported(x, y)
            step, way = (self.cfg.air_px_per_tick, drift) if airborne else (due - moved, direction)
            if way and step > 0:
                for _ in range(step):
                    if not self._blocked(x + way, y):
                        x += way
            moved = due
            if not self._supported(x, y) and y <= (self.max_row + 1) * TILE:
                for _ in range(self.cfg.fall_px_per_tick):
                    if self._supported(x, y):
                        break
                    y += 1
            path.append((x, y))
        return path

    def edge_x(self, cell: Cell, direction: int) -> int:
        """Dave's furthest pixel x toward ``direction`` while still standing on ``cell``'s ground:
        the game supports him while either foot point is over a brick, so at a platform's end he
        stands overhanging it, and a jump from there carries further than one from mid-cell."""
        x, y = cell[0] * TILE, cell[1] * TILE
        while abs(x - cell[0] * TILE) < TILE and not self._blocked(x + direction, y) \
                and self._ground(x + direction, y) == cell:
            x += direction
        return x

    def moves(self, cell: Cell) -> list[tuple[Cell, str, float]]:
        """(landing cell, kind, cost) for every estimated move from a standable cell. Jumps are
        launched from the cell and from either end of it (``edge_x``). Moves that failed in play
        cost more (``failed``)."""
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
        shapes = ((0, 0), (-1, self.cfg.short_hold_ticks), (1, self.cfg.short_hold_ticks), (-1, None), (1, None))
        # Edge launches only at a platform's end (no floor in the next cell): walking stops Dave
        # there overhanging the edge (real game, level 2: x 72 on the ledge at (4,3)).
        launches = [(col * TILE, 0.0)] + [(self.edge_x(cell, d), 0.3) for d in (-1, 1)
                                          if self.passable((col + d, row)) and not self.standable((col + d, row))]
        for x, extra in launches:
            for d, hold in shapes:
                land = self.trace(x, row * TILE, d, hold)[1]
                if land is not None and land != cell and self.standable(land):
                    cost = 2.0 + extra + 0.1 * abs(land[0] - col)
                    if land not in out or out[land][1] > cost:
                        out[land] = ("jump", cost)
        self._moves[cell] = sorted((c, k, w + FAILED_MOVE_COST * self.failed.get((cell, c), 0))
                                   for c, (k, w) in out.items())
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


def trace_skill(reach: ReachMap, x: int, y: int, spec: SkillSpec, facing: int = 0) -> list[tuple[int, int]]:
    """Estimated pixel position after every tick of ``spec`` from pixel (x, y), standing or
    falling, read from the skill's phases like ``estimate_end``; hazard cells do not stop it
    (callers test contact). A skill that presses no direction or jump keeps a standing Dave where
    he is; a falling one drifts the way he is turned (``facing`` -1, 0 or 1; 0 for no drift)."""
    phases = spec.phases
    if "jump" in phases[0].buttons:
        direction, hold = 0, 0
        for phase in phases[1:]:
            d = next((DIRECTIONS[b] for b in phase.buttons if b in DIRECTIONS), 0)
            if d:
                direction = d
                hold = None if phase.until is not None else hold + (phase.ticks or 0)
        path = reach.trace(x, y, direction, hold, stop_on_hazard=False)[0] or [(x, y)]
        if hold and len(path) < hold:
            # Landed early (a ceiling stopped the jump): the direction is still held, so he walks.
            path += reach.walk(*path[-1], direction, hold - len(path), direction)
        return path
    direction = next((DIRECTIONS[b] for p in phases for b in p.buttons if b in DIRECTIONS), 0)
    # Walks and waits; in the air (free fall) the same walk steers and falls.
    path = reach.walk(x, y, direction, spec.max_frames, facing) or [(x, y)]
    if not reach._supported(*path[-1]):
        # The skill ends in the air: with no further input he keeps falling, drifting the way he
        # now faces, until he lands. A walk off a ledge is judged by where that fall ends.
        path += reach.walk(*path[-1], 0, MAX_FLIGHT_TICKS, direction or facing)
    return path


def estimate_end_at(reach: ReachMap, x: int, y: int, spec: SkillSpec, facing: int = 0) -> Cell | None:
    """Estimated standing cell after running ``spec`` from Dave's actual pixel position
    (``trace_skill``): where he stands inside his cell matters at a ledge's end (from x 72 on the
    level 2 ledge at (4,3) jump_right reaches (8,4); from x 56 it falls short). None when the
    path touches a hazard cell or does not end standing."""
    path = trace_skill(reach, x, y, spec, facing)
    if any(reach._deadly(px, py) for px, py in path):
        return None
    return reach._ground(*path[-1])


def iter_jumps(reach: ReachMap, cell: Cell) -> Iterable[tuple[str, Cell | None]]:
    """Every catalog jump shape from ``cell`` and its estimated landing (for checks and docs)."""
    short = reach.cfg.short_hold_ticks
    for name, d, hold in (("jump_up", 0, 0), ("jump_left_short", -1, short), ("jump_right_short", 1, short),
                          ("jump_left", -1, None), ("jump_right", 1, None)):
        yield name, reach.fly(cell, d, hold)
