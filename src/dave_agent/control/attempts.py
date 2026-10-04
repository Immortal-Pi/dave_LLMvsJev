"""What the planner already tried on this level this episode, and which moves kept failing.

The planner is stateless between calls; without this it re-proposes a route that just stalled.
Kept by the goal manager, reset on a new level and each episode, identical for every arm (the
learned graph is separate: only graph arms remember across runs).

- ``attempts``: one entry per goal: its waypoints, how it ended (achieved, failed, expired,
  replaced) and why, how many waypoints were reached and the furthest tile.
- ``failed links``: a (from cell, to cell) move toward the goal's waypoint that left Dave stuck,
  or killed him. Each failure makes the move cost more in the reach estimate, so the engine's
  route goes another way when there is one; after ``avoid_after`` failures it is shown as
  ``avoid`` to the planner.
- ``deaths``: (cause, tile) of each death on this level.
"""

from __future__ import annotations

from typing import Any

from dave_agent.control.reach import Cell

MAX_ATTEMPTS = 6
MAX_LINKS = 8
AVOID_AFTER = 2


class AttemptLog:
    def __init__(self, avoid_after: int = AVOID_AFTER) -> None:
        self.avoid_after = avoid_after
        self.reset(None)

    def reset(self, level_id: str | None) -> None:
        self.level_id = level_id
        self.attempts: list[dict[str, Any]] = []
        self.links: dict[tuple[Cell, Cell], dict[str, Any]] = {}
        self.deaths: list[dict[str, Any]] = []
        self._open: dict[str, Any] | None = None

    # -- attempts ----------------------------------------------------------------------------
    def start(self, goal_id: str, target_ref: str, waypoints: list[Cell], frame: int, here: Cell | None) -> None:
        """A new goal; an attempt still open is closed as ``replaced``."""
        if self._open is not None:
            self.finish("replaced", "replanned", frame)
        self._open = {"goal_id": goal_id, "goal": target_ref, "waypoints": [list(w) for w in waypoints],
                      "start": list(here) if here else None, "start_frame": frame, "waypoints_reached": 0,
                      "furthest": list(here) if here else None}

    def progress(self, here: Cell | None, reached: int = 0) -> None:
        a = self._open
        if a is None:
            return
        a["waypoints_reached"] += reached
        if here is not None and (a["start"] is None or a["furthest"] is None or
                                 _dist(here, a["start"]) > _dist(tuple(a["furthest"]), a["start"])):
            a["furthest"] = list(here)

    def finish(self, outcome: str, reason: str, frame: int) -> None:
        a = self._open
        if a is None:
            return
        self._open = None
        n = len(a["waypoints"])
        entry = {"goal": a["goal"], "waypoints": a["waypoints"], "outcome": outcome, "reason": reason,
                 "start": a["start"], "furthest": a["furthest"], "frames": frame - a["start_frame"]}
        if n:
            entry["waypoints_reached"] = f"{a['waypoints_reached']}/{n}"
        self.attempts.append(entry)
        del self.attempts[:-MAX_ATTEMPTS]

    @property
    def open_goal(self) -> str | None:
        return None if self._open is None else self._open["goal_id"]

    # -- failed moves ------------------------------------------------------------------------
    def fail(self, frm: Cell, to: Cell, how: str) -> None:
        if frm == to:
            return
        link = self.links.setdefault((frm, to), {"from": list(frm), "to": list(to), "times": 0, "how": []})
        link["times"] += 1
        if how not in link["how"]:
            link["how"].append(how)

    def succeed(self, frm: Cell, to: Cell) -> None:
        """Dave just made this move: it no longer counts as failed."""
        self.links.pop((frm, to), None)

    def failures(self) -> dict[tuple[Cell, Cell], int]:
        """(from, to) -> times failed: the reach estimate's extra cost."""
        return {k: v["times"] for k, v in self.links.items()}

    def failed_links(self) -> list[dict[str, Any]]:
        ranked = sorted(self.links.values(), key=lambda v: -v["times"])[:MAX_LINKS]
        return [{**v, "avoid": v["times"] >= self.avoid_after} for v in ranked]

    def death(self, cause: str, tile: list[int] | None) -> None:
        self.deaths.append({"cause": cause, "tile": tile})

    def views(self) -> list[dict[str, Any]]:
        """The finished attempts, then the goal still being pursued (outcome ``active``)."""
        out = list(self.attempts)
        a = self._open
        if a is not None:
            entry = {"goal": a["goal"], "waypoints": a["waypoints"], "outcome": "active", "start": a["start"],
                     "furthest": a["furthest"]}
            if a["waypoints"]:
                entry["waypoints_reached"] = f"{a['waypoints_reached']}/{len(a['waypoints'])}"
            out.append(entry)
        return out[-MAX_ATTEMPTS:]


def _dist(a: Cell, b: list[int] | tuple[int, ...]) -> int:
    return abs(a[0] - b[0]) + abs(a[1] - b[1])
