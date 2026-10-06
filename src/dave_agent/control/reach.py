"""Estimated reachability over the observed tile map: the next landing spot toward a goal.

A deterministic estimate, not a learned or verified route (that is the world graph's job,
docs/graph.md). Cells come only from tiles observed this episode; unseen cells are blocked.
Moves from a standable cell:

- walk one column along a floor, or walk off an edge and fall to the floor below;
- the catalog's jump shapes (straight up, direction held for ``short_hold_ticks`` or each of
  ``mid_hold_ticks``, direction held until landing), simulated tick by tick with the measured jump arc, 1 px/tick air control
  and 1 px/tick free fall (``ReachConfig``; measurements in configs/skills.yaml). Walls use the
  measured body box, ground support the measured foot points. Flights touching a hazard, or falling off
  the bottom of the map, are dropped.

The result only sets the goal's waypoint. The tactical model still chooses every skill. The
simulation is checked against the real game in tests/integration/test_dave_reach.py.
"""

from __future__ import annotations

import heapq
from collections import deque
from collections.abc import Iterable, Mapping

from dave_agent.config import ReachConfig, SkillSpec

Cell = tuple[int, int]  # (col, row); row 0 is the top
TILE = 16
BLOCKING = frozenset({"solid"})
DEADLY = frozenset({"hazard"})
MAX_FLIGHT_TICKS = 400
FAILED_MOVE_COST = 6.0  # per failure in play: about three extra jumps
# The jetpack (dave.c dave_state_jetpacking_routine): one axis per tick, 1 px, no gravity, one
# fuel bar per tick whatever Dave does. A flight costs more than walking or jumping, so routes
# fly only where nothing else gets there (level 4's trophy at (5,2) and door at (97,2)).
FLY_TICKS_PER_CELL = 16
FLY_COST, FLY_CELL_COST = 8.0, 1.0  # switching on and off (and the fuel), then per cell flown
JET_STEPS = {"left": (-1, 0), "right": (1, 0), "jump": (0, -1), "down": (0, 1)}  # key -> step; jump is up
RISKY_COST = 4.0  # landing in a monster's line of fire
# Dave's collision box from his sprite's top-left pixel (dx, dy, width, height): the game tests it
# against items' full 16x16 cells (game.c collision_detect; tile.c item offsets are 0) and against
# hazard boxes (control/threats.py).
DAVE_BOX = (2, 2, 14, 16)
# Within a 16 px cell, the union of the game's hazard boxes (tile.c): fire x+6 w4 and vines x+4 w8.
HAZARD_BOX = (4, 0, 8, 16)
GRAB_COLS = 8  # how far (columns) from an item the take-offs of jumps that pick it up are searched
GRAB_ROWS = 4  # and how many rows below it


class ReachMap:
    def __init__(self, cells: Mapping[Cell, str], cfg: ReachConfig,
                 failed: Mapping[tuple[Cell, Cell], int] | None = None, risky: Iterable[Cell] = (),
                 open_unseen: bool = False, fuel: int = 0) -> None:
        """``cells`` maps every observed cell to its tile kind ("empty" when nothing is there);
        ``failed`` counts (from, to) moves that failed in play this level: each failure adds
        ``FAILED_MOVE_COST`` to the move, so routes avoid it when there is another way but still
        use it when it is the only one (a weak executor can fail a move that is right). Landing
        on a ``risky`` cell (in a monster's line of fire, control/threats.py ``firing_cells``)
        costs ``RISKY_COST`` more. Unseen cells block movement, so routes stay on the known
        map; with ``open_unseen`` they are open air instead, for paths that leave the map
        (``leaves_map``). With jetpack ``fuel`` (ticks), routes may also fly (``fly_cells``)."""
        self.cells, self.cfg = cells, cfg
        self.fuel = fuel
        self._flies: dict[Cell, tuple[dict[Cell, int], dict[Cell, Cell | None]]] = {}
        self.open_unseen = open_unseen
        self.failed = dict(failed or {})
        self.risky = frozenset(risky)
        self.max_row = max((r for _, r in cells), default=0)
        self._moves: dict[Cell, list[tuple[Cell, str, float]]] = {}
        # (from, landing) -> (launch x, direction, hold) of the jump ``moves`` chose: how to draw it.
        self.launch: dict[tuple[Cell, Cell], tuple[int, int, int | None]] = {}
        self._grabs: dict[Cell, dict[Cell, tuple[Cell, tuple[int, int, int | None]]]] = {}
        self._open: ReachMap | None = None

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

    def opened(self) -> ReachMap:
        """The same cells with unseen ones as open air: where a path past the screen edge goes."""
        return ReachMap(self.cells, self.cfg, self.failed, self.risky, open_unseen=True, fuel=self.fuel)

    def _wall(self, cell: Cell) -> bool:
        kind = self.cells.get(cell)
        return kind in BLOCKING or (kind is None and not self.open_unseen)

    def _blocked(self, x: int, y: int, direction: int) -> bool:
        """Whether Dave at pixel (x, y), having moved ``direction`` (-1 or 1), would be in a wall.
        Only the leading edge counts, as in the game (dave.c tests x+12 before a step right and
        x+1 before a step left): rising past a brick beside him does not stop him moving away."""
        left, right = self.cfg.body_px
        col = (x + (right if direction > 0 else left)) // TILE
        return any(self._wall((col, r)) for r in {(y + 2) // TILE, (y + TILE - 1) // TILE})

    def known_flight(self, x: int, y: int, direction: int, hold: int | None
                     ) -> tuple[list[tuple[int, int]], Cell | None] | None:
        """``trace`` for a route move, or None when the flight enters an unseen cell: what it meets
        there is unknown. Unseen cells as walls would bounce it off an edge that is not there
        (level 3: the long jump from (29,6) to the screen's last column, (34,6), went on into the
        fire beyond it)."""
        if self.open_unseen:
            return self.trace(x, y, direction, hold)
        if self._open is None:
            self._open = self.opened()
        path, land = self._open.trace(x, y, direction, hold)
        return None if self.leaves_map(path) is not None else (path, land)

    def leaves_map(self, path: Iterable[tuple[int, int]]) -> int | None:
        """The first tick (1-based) at which Dave's body on ``path`` covers an unseen cell, or None."""
        for i, (x, y) in enumerate(path):
            if any(c not in self.cells for c in self._box_cells(x, y)):
                return i + 1
        return None

    def _head_blocked(self, x: int, y: int) -> bool:
        """The game's top check (dave.c ``dave_collision_top``) with Dave's top at pixel y: the
        head points at x + ``head_px`` one pixel below the top."""
        return any(self._wall(((x + dx) // TILE, (y + 1) // TILE)) for dx in self.cfg.head_px)

    def _deadly(self, x: int, y: int) -> bool:
        return any(self.cells.get(c) in DEADLY for c in self._box_cells(x, y))

    def burns(self, path: Iterable[tuple[int, int]]) -> bool:
        """Whether Dave's box touches a hazard's box anywhere on ``path`` (the game's contact test,
        as the threat screen checks it). Stricter than ``_deadly``, which tests the cells his
        narrower body covers: from x 110 on level 3 the long jump over the vine "lands" on the far
        platform by cells, but its landing touches the next vine and he burns."""
        hazards = [(c * TILE, r * TILE) for (c, r), kind in self.cells.items() if kind in DEADLY]
        if not hazards:
            return False
        dx, dy, w, h = DAVE_BOX
        hx0, hy0, hw, hh = HAZARD_BOX
        return any(x + dx < hx + hx0 + hw and x + dx + w > hx + hx0 and y + dy < hy + hy0 + hh and y + dy + h > hy + hy0
                   for x, y in path for hx, hy in hazards)

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

    def platform_of(self, cell: Cell) -> tuple[int, int, int] | None:
        """(row, first col, last col) of the run of standable cells ``cell`` belongs to, or None."""
        if not self.standable(cell):
            return None
        col, row = cell
        lo = hi = col
        while self.standable((lo - 1, row)):
            lo -= 1
        while self.standable((hi + 1, row)):
            hi += 1
        return row, lo, hi

    def same_platform(self, a: Cell | None, b: Cell | None) -> bool:
        """Two standing cells on one platform: a landing a cell short of the planned one (Dave's
        x is rarely a multiple of 16 in the real game) still makes the same move."""
        if a is None or b is None:
            return False
        return a == b or (a[1] == b[1] and self.platform_of(a) is not None and self.platform_of(a) == self.platform_of(b))

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
              stop_on_hazard: bool = True, t0: int = 1, y_now: int | None = None
              ) -> tuple[list[tuple[int, int]], Cell | None]:
        """Dave's pixel position after every tick of a jump from standing at pixel (x, y0), and
        the landing cell (None when it lands nowhere safe). ``stop_on_hazard`` off flies through
        hazard cells, for callers that test contact with the game's smaller hazard boxes. Once the
        arc ends in the air (or a ceiling cuts it) Dave free-falls; a direction pressed during the
        fall turns him, and from then on he drifts that way with no key held (dave.c). ``t0``
        and ``y_now`` continue a jump already under way: at tick ``t0`` of the arc, at pixel
        ``y_now`` (``hold`` then counts from ``t0``)."""
        arc, cfg = self.cfg.arc_px, self.cfg
        start = _cell(x, y0)
        drifting = False
        y, falling = (y0 if y_now is None else y_now), t0 >= len(arc)
        path: list[tuple[int, int]] = []
        bumped = False
        for t in range(t0, t0 + MAX_FLIGHT_TICKS):
            held = bool(direction) and (hold is None or t - t0 < hold)
            if bumped:
                # dave.c: the tick after a head bump (jump_state 94) Dave lands 2 px lower if
                # there is ground there, else he starts to fall from where he is.
                bumped, falling = False, True
                if self._supported(x, y + 2):
                    path.append((x, y + 2))
                    return path, self._ground(x, y + 2)
            elif not falling and t < len(arc):
                # Jumping (dave.c): y first, then x.
                target = y0 - arc[t]
                step = -1 if target < y else 1
                while y != target:
                    if step < 0:
                        y -= 1
                        # The game tests the head 2 px ahead when a direction is held and only stops
                        # the jump when that is blocked too, so a jump slips past a ledge corner.
                        if self._head_blocked(x, y) and (not held or self._head_blocked(x + 2 * direction, y)):
                            bumped = True  # head hit the tile above: no sideways step this tick
                            break
                        continue
                    if self._supported(x, y):
                        path.append((x, y))
                        return path, self._ground(x, y)
                    y += 1
                if held and not bumped and (t - t0) % 2 == 0:
                    # 2 px every other tick with the key held (dave.c walk_state cooldown).
                    for _ in range(2 * cfg.air_px_per_tick):
                        if not self._blocked(x + direction, y, direction):
                            x += direction
            else:
                # Free fall (dave.c): ground check, 1 px down, then the drift: a key pressed in
                # the air turns Dave, and from then on he drifts that way with no key held.
                falling = True
                drifting = drifting or held
                for _ in range(cfg.fall_px_per_tick):
                    if self._supported(x, y):
                        path.append((x, y))
                        return path, self._ground(x, y)
                    y += 1
                if drifting:
                    for _ in range(cfg.air_px_per_tick):
                        if not self._blocked(x + direction, y, direction):
                            x += direction
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
            # 2 px on the first tick of every 3 (dave.c walking: a step on entering, then two
            # cooldown ticks), so a walk is 2 px ahead on its first tick.
            due = (t + 2) // 3 * self.cfg.walk_px_per_3_ticks
            airborne = not self._supported(x, y)
            step, way = (self.cfg.air_px_per_tick, drift) if airborne else (due - moved, direction)
            if way and step > 0:
                for _ in range(step):
                    if not self._blocked(x + way, y, way):
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
        while abs(x - cell[0] * TILE) < TILE and not self._blocked(x + direction, y, direction) \
                and self._ground(x + direction, y) == cell:
            x += direction
        return x

    def shapes(self) -> tuple[tuple[int, int | None], ...]:
        """(direction, hold) of every catalog jump: straight up, each fixed hold both ways, and
        the direction held until landing."""
        holds = (self.cfg.short_hold_ticks, *self.cfg.mid_hold_ticks)
        return ((0, 0), *((d, h) for h in holds for d in (-1, 1)), (-1, None), (1, None))

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
        shapes = self.shapes()
        # Edge launches only at a platform's end (no floor in the next cell): walking stops Dave
        # there overhanging the edge (real game, level 2: x 72 on the ledge at (4,3)).
        launches = [(col * TILE, 0.0)] + [(self.edge_x(cell, d), 0.3) for d in (-1, 1)
                                          if self.passable((col + d, row)) and not self.standable((col + d, row))]
        for x, extra in launches:
            for d, hold in shapes:
                flown = self.known_flight(x, row * TILE, d, hold)
                land = None if flown is None else flown[1]
                if land is not None and land != cell and self.standable(land):
                    cost = 2.0 + extra + 0.1 * abs(land[0] - col)
                    if land not in out or out[land][1] > cost:
                        out[land] = ("jump", cost)
                        self.launch[(cell, land)] = (x, d, hold)
        self._moves[cell] = sorted((c, k, w + FAILED_MOVE_COST * self.failed.get((cell, c), 0)
                                    + (RISKY_COST if c in self.risky else 0.0))
                                   for c, (k, w) in out.items())
        return self._moves[cell]

    # -- search ------------------------------------------------------------------------------
    def path(self, start: Cell, targets: set[Cell]) -> list[tuple[Cell, str]] | None:
        """Cheapest move sequence from ``start`` to any target: [(cell, move kind), ...], or None."""
        found = self._search(start, targets)
        return None if found is None else found[1]

    def cost(self, start: Cell, targets: set[Cell]) -> float | None:
        """The estimated cost of that cheapest sequence (0 at a target), or None with no known way."""
        found = self._search(start, targets)
        return None if found is None else found[0]

    def _search(self, start: Cell, targets: set[Cell]) -> tuple[float, list[tuple[Cell, str]]] | None:
        if not self.standable(start) and not (self.fuel and self.passable(start)):
            return None  # in the air only with the jetpack (then it flies on from there)
        if start in targets:
            return 0.0, []
        best: dict[Cell, float] = {start: 0.0}
        back: dict[Cell, tuple[Cell, str]] = {}
        queue = [(0.0, start)]
        while queue:
            cost, cell = heapq.heappop(queue)
            if cell in targets:
                total = cost
                out = []
                while cell != start:
                    prev, kind = back[cell]
                    out.append((cell, kind))
                    cell = prev
                return total, out[::-1]
            if cost > best[cell]:
                continue
            for nxt, kind, step in (self.moves(cell) if self.standable(cell) else []) + self._fly_moves(cell, targets):
                if cost + step < best.get(nxt, float("inf")):
                    best[nxt], back[nxt] = cost + step, (cell, kind)
                    heapq.heappush(queue, (cost + step, nxt))
        return None

    def fly_cells(self, start: Cell) -> tuple[dict[Cell, int], dict[Cell, Cell | None]]:
        """(cells flown -> distance in cells, predecessor) for a jetpack flight from ``start``
        through known open cells (no brick, no hazard, nothing unseen), as far as the fuel goes."""
        if start in self._flies:
            return self._flies[start]
        limit = max(0, self.fuel - 2) // FLY_TICKS_PER_CELL
        dist: dict[Cell, int] = {start: 0}
        prev: dict[Cell, Cell | None] = {start: None}
        queue = deque([start])
        while queue:
            cell = queue.popleft()
            if dist[cell] >= limit:
                continue
            for dc, dr in ((1, 0), (-1, 0), (0, -1), (0, 1)):
                nxt = (cell[0] + dc, cell[1] + dr)
                if nxt not in dist and self.passable(nxt):
                    dist[nxt], prev[nxt] = dist[cell] + 1, cell
                    queue.append(nxt)
        self._flies[start] = (dist, prev)
        return dist, prev

    def fly_path(self, start: Cell, end: Cell) -> list[Cell]:
        """The cells of the shortest flight ``start`` -> ``end`` (both included), or []."""
        dist, prev = self.fly_cells(start)
        if end not in dist:
            return []
        out: list[Cell] = []
        cell: Cell | None = end
        while cell is not None:
            out.append(cell)
            cell = prev[cell]
        return out[::-1]

    def _fly_moves(self, cell: Cell, targets: set[Cell]) -> list[tuple[Cell, str, float]]:
        """Flights from ``cell`` to every standable cell and target in fuel range."""
        if not self.fuel:
            return []
        dist, _ = self.fly_cells(cell)
        return [(c, "fly", FLY_COST + FLY_CELL_COST * n + (RISKY_COST if c in self.risky else 0.0))
                for c, n in dist.items() if n and (self.standable(c) or c in targets)]

    def jet_step(self, x: int, y: int, key: str) -> tuple[int, int]:
        """Dave's position after one jetpack tick with ``key`` held (dave.c): left and right test
        the leading edge, up the head points, down the ground (it stops on the floor)."""
        dx, dy = JET_STEPS[key]
        if dx and not self._blocked(x + dx, y, dx):
            return x + dx, y
        if dy < 0 and not self._head_blocked(x, y):
            return x, y - 1
        if dy > 0 and not self._supported(x, y):
            return x, y + 1
        return x, y

    def targets_for(self, goal: Cell) -> set[Cell]:
        """Cells to stand on to take something at ``goal``: the goal cell itself if standable,
        else standable cells up to 2 rows below it (a jump touches it), and the take-offs of
        catalog jumps that pick it up in flight and land safely (``grab_takeoffs``): level 3's
        gun floats over a vine and is taken only mid-jump."""
        if self.standable(goal):
            return {goal}
        col, row = goal
        under = {(col, r) for r in range(row + 1, row + 3) if self.standable((col, r))}
        # With the jetpack, the cell itself: Dave flies into it (level 4's trophy at (5,2)).
        flown = {goal} if self.fuel and self.passable(goal) else set()
        return under | set(self.grab_takeoffs(goal)) | flown

    def grab_takeoffs(self, item: Cell) -> dict[Cell, tuple[Cell, tuple[int, int, int | None]]]:
        """Take-off cell -> (landing, (launch x, direction, hold)) for the catalog jump shapes
        whose simulated flight touches ``item``'s cell and lands safely, from the cells within
        ``GRAB_COLS`` columns and ``GRAB_ROWS`` rows below. Launches as in ``moves``: mid-cell and,
        at a platform's end, overhanging it."""
        if item in self._grabs:
            return self._grabs[item]
        out: dict[Cell, tuple[Cell, tuple[int, int, int | None]]] = {}
        if not self.passable(item):
            return out  # unseen, solid or a hazard: nothing to take there
        shapes = self.shapes()
        icol, irow = item
        for row in range(irow, irow + GRAB_ROWS + 1):
            for col in range(icol - GRAB_COLS, icol + GRAB_COLS + 1):
                cell = (col, row)
                if not self.standable(cell):
                    continue
                launches = [col * TILE] + [self.edge_x(cell, d) for d in (-1, 1)
                                           if self.passable((col + d, row)) and not self.standable((col + d, row))]
                for x in launches:
                    for d, hold in shapes:
                        flown = self.known_flight(x, row * TILE, d, hold)
                        if flown is None:
                            continue
                        path, land = flown
                        if land is not None and self.standable(land) and grabs(path, item) and not self.burns(path):
                            out.setdefault(cell, (land, (x, d, hold)))
        self._grabs[item] = out
        return out


def grabs(path: Iterable[tuple[int, int]], item: Cell) -> bool:
    """Whether Dave's box touches ``item``'s 16x16 cell at any pixel position of ``path`` (Dave's
    top-left), as the game's pickup test does."""
    dx, dy, w, h = DAVE_BOX
    left, top = item[0] * TILE, item[1] * TILE
    return any(x + dx < left + TILE and x + dx + w > left and y + dy < top + TILE and y + dy + h > top
               for x, y in path)


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
    if route and route[-1][0] in reach.grab_takeoffs(goal):
        return route[-1][0]  # walk to the take-off of the jump that picks it up in flight
    return goal


DIRECTIONS = {"left": -1, "right": 1}


def flight(reach: ReachMap, frm: Cell, to: Cell) -> list[tuple[int, int]]:
    """Dave's pixel path for the move ``frm`` -> ``to`` as ``moves`` estimated it: for a jump,
    the walk from mid-cell to its take-off then the simulated flight; for a walk or a fall, the
    straight line. For drawing."""
    reach.moves(frm)
    launch = reach.launch.get((frm, to))
    y = frm[1] * TILE
    if launch is None:
        return [(frm[0] * TILE, y), (to[0] * TILE, to[1] * TILE)]
    x, d, hold = launch
    return [(frm[0] * TILE, y), (x, y), *reach.trace(x, y, d, hold)[0]]


def frontier(reach: ReachMap, side: int) -> set[Cell]:
    """Standable cells at the explored map's edge toward ``side`` (-1 left, 1 right): the next
    cell that way has not been seen. Exploring that way means getting to one of them first. When
    none is (level 3: the screen's last column is a fire pit), the standable cells furthest that
    way: getting there scrolls the screen or leaves a jump that does."""
    edge = {c for c in reach.cells if c[1] >= 1 and reach.standable(c) and (c[0] + side, c[1]) not in reach.cells}
    if edge:
        return edge
    standable = [c for c in reach.cells if c[1] >= 1 and reach.standable(c)]
    if not standable:
        return set()
    far = max(c[0] * side for c in standable)
    return {c for c in standable if c[0] * side == far}


def next_landing(reach: ReachMap, start: Cell, targets: set[Cell]) -> Cell | None:
    """Like ``next_waypoint`` for a set of target cells: the first jump or fall landing on the
    cheapest path to any of them, the target reached when only walking is left, or None when no
    path is known or Dave is already on one."""
    route = reach.path(start, targets)
    if not route:
        return None
    return next((cell for cell, kind in route if kind != "walk"), route[-1][0])


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
    if direction and phases[0].until == "airborne":
        # Step off: walk to the platform's end, then drop straight down (no key held).
        col, row = cell
        while reach.standable((col + direction, row)):
            col += direction
        if not reach.passable((col + direction, row)):
            return (col, row)  # a wall at the end: he never leaves the floor
        col, r = col + direction, row + 1
        while reach.passable((col, r)) and not reach.standable((col, r)):
            r += 1
        return (col, r) if reach.standable((col, r)) else None
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


def takeoff_delay(cooldown: int | None) -> int:
    """Ticks a standing Dave stays put after Up is pressed: the game decrements its jump cooldown
    (``Observation.jump_cooldown``, 5 after a landing) and jumps once it is 0, and the tick the
    jump starts does not rise yet (real game: rises 0, 1, 1, 3 ... with no cooldown; four more
    still ticks after a landing)."""
    return max(0, (cooldown or 0) - 1) + 1


def trace_skill(reach: ReachMap, x: int, y: int, spec: SkillSpec, facing: int = 0,
                cooldown: int | None = 0) -> list[tuple[int, int]]:
    """Estimated pixel position after every tick of ``spec`` from pixel (x, y), standing or
    falling, read from the skill's phases like ``estimate_end``; hazard cells do not stop it
    (callers test contact). A skill that presses no direction or jump keeps a standing Dave where
    he is; a falling one drifts the way he is turned (``facing`` -1, 0 or 1; 0 for no drift). A
    jump starts after the game's landing cooldown (``cooldown``, ``takeoff_delay``)."""
    phases = spec.phases
    if "jump" in phases[0].buttons:
        wait = [(x, y)] * takeoff_delay(cooldown)
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
        return wait + path
    direction = next((DIRECTIONS[b] for p in phases for b in p.buttons if b in DIRECTIONS), 0)
    if direction and phases[0].until == "airborne":
        # Step off: walk until the floor ends, then no key: entering the fall faces Dave front,
        # so he drops straight down (dave.c).
        path = []
        for sx, sy in reach.walk(x, y, direction, phases[0].budget, facing):
            if sy != y or not reach._supported(sx, y):
                # Real game (level 4, (64,5)): the step past the edge, then a tick entering the
                # fall at the same spot (dave.c tests the ground at the start of a tick), then
                # 1 px a tick down until he lands.
                fall = reach.walk(sx, y, 0, MAX_FLIGHT_TICKS, 0) or [(sx, y)]
                down = next((i for i, p in enumerate(fall) if reach._supported(*p)), len(fall) - 1)
                return path + [(sx, y), (sx, y)] + fall[:down + 1]
            path.append((sx, sy))
        return path or [(x, y)]
    # Walks and waits; in the air (free fall) the same walk steers and falls.
    path = reach.walk(x, y, direction, spec.max_frames, facing) or [(x, y)]
    if not reach._supported(*path[-1]):
        # The skill ends in the air: with no further input he keeps falling, drifting the way he
        # now faces, until he lands. A walk off a ledge is judged by where that fall ends.
        path += reach.walk(*path[-1], 0, MAX_FLIGHT_TICKS, direction or facing)
    return path


def trace_flying(reach: ReachMap, x: int, y: int, spec: SkillSpec, fuel: int | None = None) -> list[tuple[int, int]]:
    """Dave's pixel path while ``spec`` runs with the jetpack on: each phase's key moves him
    1 px a tick (left before right before up before down, dave.c), no key hovers. The jetpack
    key turns it off, and so does running out of ``fuel``: he then falls until he lands."""
    path: list[tuple[int, int]] = []
    left = fuel if fuel is not None else 10**9
    for phase in spec.phases:
        keys = [k for k in ("left", "right", "jump", "down") if k in phase.buttons]
        for _ in range(phase.ticks or phase.max_ticks or 0):
            if "jetpack" in phase.buttons or left <= 0:
                # Off (the tick of the key keeps him in place), then free fall to the ground.
                fall = reach.walk(x, y, 0, MAX_FLIGHT_TICKS) or [(x, y)]
                down = next((i for i, p in enumerate(fall) if reach._supported(*p)), len(fall) - 1)
                return path + [(x, y)] + fall[:down + 1]
            if keys:
                x, y = reach.jet_step(x, y, keys[0])
            left -= 1
            path.append((x, y))
    return path or [(x, y)]


def trace_in_jump(reach: ReachMap, x: int, y: int, jump_tick: int, spec: SkillSpec) -> list[tuple[int, int]]:
    """Dave's estimated pixel path while ``spec`` runs from mid-jump (``jump_tick`` ticks into
    the arc, at pixel (x, y)): a walk or a jump skill steers with its direction for the skill's
    ticks (air control), anything else lets him fly on; then the arc continues to the ground."""
    direction = next((DIRECTIONS[b] for p in spec.phases for b in p.buttons if b in DIRECTIONS), 0)
    arc = reach.cfg.arc_px
    rise = arc[min(jump_tick, len(arc) - 1)]
    path, _ = reach.trace(x, y + rise, direction, spec.max_frames if direction else 0,
                          stop_on_hazard=False, t0=jump_tick + 1, y_now=y)
    path = path or [(x, y)]
    if direction and len(path) < spec.max_frames:
        # Landed with the key still held: he walks on for the rest of the skill (level 3: a
        # move_left_3 chosen just before landing walked Dave into a vine).
        path += reach.walk(*path[-1], direction, spec.max_frames - len(path), direction)
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
    names = [("jump_up", 0, 0), ("jump_left_short", -1, short), ("jump_right_short", 1, short)]
    # jump_*_<n> holds the direction n tiles' worth of ticks (1 px/tick in the air).
    names += [(f"jump_{side}_{round(h / TILE)}", d, h) for h in reach.cfg.mid_hold_ticks
              for side, d in (("left", -1), ("right", 1))]
    for name, d, hold in names + [("jump_left", -1, None), ("jump_right", 1, None)]:
        yield name, reach.fly(cell, d, hold)
