"""Platforms: the explored map as places Dave can stand and the moves between them.

The planner reads a character map poorly, especially which column a wall is in. This turns the
same explored cells into a list of platforms (maximal runs of standable cells on one row), each
with its exits (the walks off an edge and jumps ``ReachMap`` estimates from it, with the game's
physics) and whether Dave can get there from where he stands. A wall shows up as a missing exit.
Built from observed cells only, identical for every arm; an estimate, not a learned route.

Platform ids are ``c<left col>r<row>`` (e.g. ``c8r4``): stable while the platform's left end stays
the same, and readable next to the map.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Any

from dave_agent.control.reach import Cell, ReachMap, frontier

TOP_ROW = 1  # row 0 is above the level's top wall: standable on paper, never reachable
MAX_PLATFORMS = 40


@dataclass
class Platform:
    pid: str
    row: int
    cols: tuple[int, int]
    exits: dict[str, dict[str, Any]] = field(default_factory=dict)  # to pid -> {by, from_col, cost}
    hops: int | None = None  # moves from Dave's platform; None when not reachable
    items: list[str] = field(default_factory=list)  # candidate goal ids taken from here
    open: list[str] = field(default_factory=list)  # ends next to unseen cells: "left", "right"
    danger: list[str] = field(default_factory=list)

    def cells(self) -> list[Cell]:
        return [(c, self.row) for c in range(self.cols[0], self.cols[1] + 1)]

    def view(self) -> dict[str, Any]:
        out: dict[str, Any] = {"id": self.pid, "row": self.row, "cols": list(self.cols),
                               "reachable": self.hops is not None}
        if self.hops is not None:
            out["hops"] = self.hops
        out["exits"] = [{"to": to, "by": e["by"], "from_col": e["from_col"], **({"note": e["note"]} if "note" in e
                                                                                 else {})}
                        for to, e in sorted(self.exits.items())]
        for key in ("items", "open", "danger"):
            if getattr(self, key):
                out[key] = list(getattr(self, key))
        return out


def _how(kind: str, frm: Cell, to: Cell) -> str:
    side = "up" if to[0] == frm[0] else ("right" if to[0] > frm[0] else "left")
    return f"{kind} {side}" if kind != "walk" else f"walk {side}"


class Platforms:
    """Every explored platform of the level, their exits and reachability from ``here``."""

    def __init__(self, reach: ReachMap, here: Cell | None) -> None:
        self.reach, self.here = reach, here
        self.by_cell: dict[Cell, Platform] = {}
        self.items: dict[str, Platform] = {}
        cells = sorted((c for c in reach.cells if c[1] >= TOP_ROW and reach.standable(c)), key=lambda c: (c[1], c[0]))
        for col, row in cells:
            left = self.by_cell.get((col - 1, row))
            if left is not None:
                left.cols = (left.cols[0], col)
                self.by_cell[(col, row)] = left
                continue
            p = Platform(pid=f"c{col}r{row}", row=row, cols=(col, col))
            self.by_cell[(col, row)] = p
            self.items[p.pid] = p
        for p in self.items.values():
            for cell in p.cells():
                for to, kind, cost in reach.moves(cell):
                    q = self.by_cell.get(to)
                    if q is None or q is p:
                        continue
                    best = p.exits.get(q.pid)
                    if best is None or cost < best["cost"]:
                        p.exits[q.pid] = {"by": _how(kind, cell, to), "from_col": cell[0], "cost": cost}
            for side, col in (("left", p.cols[0] - 1), ("right", p.cols[1] + 1)):
                if (col, p.row) not in reach.cells or (col, p.row + 1) not in reach.cells:
                    p.open.append(side)
        start = self.by_cell.get(here) if here is not None else None
        if start is not None:
            start.hops = 0
            queue = deque([start])
            while queue:
                p = queue.popleft()
                for to in p.exits:
                    q = self.items[to]
                    if q.hops is None:
                        q.hops = p.hops + 1  # type: ignore[operator]
                        queue.append(q)

    def of(self, cell: Cell) -> Platform | None:
        return self.by_cell.get(cell)

    def resolve(self, pid: str, toward: Cell | None = None) -> Cell | None:
        """The cell of platform ``pid`` nearest ``toward`` (its middle without one)."""
        p = self.items.get(pid)
        if p is None:
            return None
        col = (p.cols[0] + p.cols[1]) // 2 if toward is None else min(max(toward[0], p.cols[0]), p.cols[1])
        return (col, p.row)

    def nearest_reachable(self, target: Cell) -> Platform | None:
        """The reachable platform closest to ``target`` (columns, then rows)."""
        best = None
        for p in self.items.values():
            if p.hops is None:
                continue
            dc = max(p.cols[0] - target[0], 0, target[0] - p.cols[1])
            key = (dc + abs(p.row - target[1]), p.hops)
            if best is None or key < best[0]:
                best = (key, p)
        return None if best is None else best[1]

    def chain(self, steps: list[tuple[Cell, str]], start: Cell) -> str:
        """``c1r9 -jump right-> c4r7 -walk-> ...`` for a ``ReachMap.path`` from ``start``; walks
        within one platform are merged."""
        parts = [self.by_cell[start].pid if start in self.by_cell else f"({start[0]},{start[1]})"]
        prev = start
        for cell, kind in steps:
            p = self.by_cell.get(cell)
            if p is not None and self.by_cell.get(prev) is p:
                prev = cell
                continue
            parts.append(f"-{_how(kind, prev, cell)}-> {p.pid if p else f'({cell[0]},{cell[1]})'}")
            prev = cell
        return " ".join(parts)

    def path_note(self, target: Cell, explore: int = 0) -> str:
        """How Dave gets from ``here`` to take something at ``target``, as a platform chain, or
        why no path is known. ``explore`` (-1 left, 1 right): the target is unexplored, so the
        path leads to the nearest reachable platform with an unexplored end that way."""
        if self.here is None or self.of(self.here) is None:
            return "unknown: Dave is not standing on a known platform"
        if explore:
            side = "right" if explore > 0 else "left"
            steps = self.reach.path(self.here, frontier(self.reach, explore))
            if steps is None:
                return f"no reachable platform with an unexplored end to the {side}"
            if not steps:
                return f"Dave is at an unexplored end: walk {side}"
            return f"{self.chain(steps, self.here)} (its {side} end is unexplored)"
        steps = self.reach.path(self.here, self.reach.targets_for(target))
        if steps is not None:
            return self.chain(steps, self.here) + (" (already there)" if not steps else "")
        near = self.nearest_reachable(target)
        if near is None:
            return "no known path over the explored platforms"
        return f"no known path over the explored platforms; nearest reachable platform: {near.pid}"

    def note_items(self, items: dict[str, Cell]) -> None:
        """Mark each candidate goal on the platforms it can be taken from."""
        for gid, target in items.items():
            for cell in self.reach.targets_for(target):
                p = self.by_cell.get(cell)
                if p is not None and gid not in p.items:
                    p.items.append(gid)

    def note_danger(self, deaths: list[dict[str, Any]], label: str) -> None:
        """``died here <n>x (<causes>)`` on the platform at, or just above, each death tile."""
        counts: dict[str, list[str]] = {}
        for d in deaths:
            tile = d.get("tile")
            if not tile:
                continue
            p = next((self.by_cell[(tile[0], r)] for r in range(tile[1], tile[1] - 3, -1)
                      if (tile[0], r) in self.by_cell), None)
            if p is not None:
                counts.setdefault(p.pid, []).append(str(d.get("cause", "unknown")))
        for pid, causes in counts.items():
            kinds = ", ".join(sorted(set(causes)))
            self.items[pid].danger.append(f"{label} {len(causes)}x ({kinds})")

    def note_fire(self, cells: set[Cell]) -> None:
        """``in the line of fire`` on the platforms a predicted shot crosses."""
        for p in self.items.values():
            if any(c in cells for c in p.cells()):
                p.danger.append("in the line of fire of a monster's shots")

    def note_failed(self, links: list[dict[str, Any]]) -> None:
        for link in links:
            p, q = self.by_cell.get(tuple(link["from"])), self.by_cell.get(tuple(link["to"]))
            if p is not None and q is not None and q.pid in p.exits:
                p.exits[q.pid]["note"] = f"failed {link['times']}x this level"

    def views(self, limit: int = MAX_PLATFORMS) -> list[dict[str, Any]]:
        """Reachable platforms first (fewest hops), then the rest, top to bottom."""
        ranked = sorted(self.items.values(),
                        key=lambda p: (p.hops is None, p.hops if p.hops is not None else 0, p.row, p.cols[0]))
        return [p.view() for p in ranked[:limit]]
