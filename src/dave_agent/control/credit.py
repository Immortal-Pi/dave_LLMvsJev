"""Goal credit: what each skill did toward the goal's target, from each standing cell.

The learned graph counts a move a success when Dave lands alive on another platform, so a jump
back and forth between two platforms looks perfect while it leads nowhere (live level 3: 37/37
c2 -> c6 and 29/29 back, while the gun was never taken). Credit measures progress instead: after
each skill, the estimated remaining cost to the goal's target (control/reach.py ``ReachMap.cost``)
from where the skill started and from where it ended.

- This episode (every arm): ``GoalCredit`` keeps, per target and per (cell, skill), how often the
  skill was tried, got closer, got farther, came back to a cell already visited while going for
  that target, and killed Dave. ``note`` turns it into a candidate note.
- Across runs (graph-enabled arms): when a goal ends, ``finish`` returns the steps it took and
  whether it was achieved; the episode loop hands that to the learned graph
  (memory/graph.py ``WorldGraph.record_credit``) when the graph is learning.

Notes and route costs only: nothing is masked, and the tactical model still chooses.
"""

from __future__ import annotations

from dataclasses import dataclass, field

Cell = tuple[int, int]


@dataclass
class StepCredit:
    tries: int = 0
    closer: int = 0
    farther: int = 0
    loops: int = 0  # ended on a cell already visited while going for this target
    died: int = 0


@dataclass
class GoalSteps:
    """One goal's steps, handed to the learned graph when it ends."""

    target_ref: str
    level_id: str
    reached: bool
    steps: list[tuple[Cell, str, bool]] = field(default_factory=list)  # (start cell, skill, closer)


class GoalCredit:
    def __init__(self) -> None:
        self._table: dict[str, dict[Cell, dict[str, StepCredit]]] = {}
        self._visited: dict[str, set[Cell]] = {}
        self._current: GoalSteps | None = None

    def reset(self) -> None:
        self._table.clear()
        self._visited.clear()
        self._current = None

    def start(self, target_ref: str, level_id: str, here: Cell | None) -> None:
        self._current = GoalSteps(target_ref, level_id, reached=False)
        if here is not None:
            self._visited.setdefault(target_ref, set()).add(here)

    def record(self, start: Cell, skill: str, before: float | None, after: float | None, end: Cell | None,
               died: bool) -> None:
        """One skill run for the current goal from standing cell ``start``. ``before``/``after``
        are the estimated remaining costs to the target (None: no known way); ``end`` is the
        standing cell after it (None: not standing)."""
        goal = self._current
        if goal is None:
            return
        rec = self._table.setdefault(goal.target_ref, {}).setdefault(start, {}).setdefault(skill, StepCredit())
        rec.tries += 1
        closer = after is not None and (before is None or after < before)
        if died:
            rec.died += 1
        elif closer:
            rec.closer += 1
        elif before is not None and (after is None or after > before):
            rec.farther += 1
        visited = self._visited.setdefault(goal.target_ref, set())
        if end is not None and end != start and end in visited:
            rec.loops += 1
        if end is not None:
            visited.add(end)
        goal.steps.append((start, skill, closer and not died))

    def finish(self, achieved: bool) -> GoalSteps | None:
        goal, self._current = self._current, None
        if goal is None or not goal.steps:
            return None
        goal.reached = achieved
        return goal

    def note(self, target_ref: str, here: Cell, skill: str) -> str | None:
        rec = self._table.get(target_ref, {}).get(here, {}).get(skill)
        if rec is None or not rec.tries:
            return None
        parts = [f"{rec.tries}x"]
        if rec.died:
            parts.append(f"{rec.died} died")
        if rec.closer:
            parts.append(f"{rec.closer} closer")
        elif rec.tries >= 2:
            parts.append("never closer")
        if rec.loops:
            parts.append(f"{rec.loops}x back where Dave had already been")
        return "for this goal from here: " + ", ".join(parts)
