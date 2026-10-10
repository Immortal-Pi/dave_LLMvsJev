"""Live move scores from the learned graph: the best outcome from each position (docs/graph.md,
"Live move scores"). Graph-enabled arms only; the scores inform, nothing is masked or reordered.

- ``V(p)``: the cost of the best way from platform ``p`` to the goal's target, choosing the best
  move at every step. One reverse Dijkstra from the target over the same edge costs as
  ``find_route`` (``memory/routes.py``: time, risk, uncertainty, goal credit). With no reachable
  target it is the cost to the nearest platform with an unexplored side (exploration).
- ``Q(here, a)``: the cost of doing ``a`` from here plus ``V`` of where it ends. A move ends where
  its edges most often landed, or, untried, at the reach estimate's end cell. An action that stays
  on the platform (shoot, wait, a blocked move) costs its time and death risk plus ``V(here)``; a
  shot predicted to hit a monster gets ``kill_bonus`` off; otherwise a shot from a platform where
  shots killed a monster before gets ``kill_bonus`` times that rate off.
- ``regret = Q - min Q``: 0 is the best option from this position.

The graph is the live one, so every move recorded this episode changes the next decision's scores.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

import networkx as nx

from dave_agent.config import GraphConfig
from dave_agent.memory.graph import WorldGraph, edge_key, success_probability
from dave_agent.memory.routes import _best, credit_factor, edge_cost, held_items, target_requirements
from dave_agent.schemas import SkillCandidate

SHOOT_SKILLS = frozenset({"shoot", "shoot_left", "shoot_right"})
STAY_SKILLS = SHOOT_SKILLS | {"wait", "wait_short"}  # never meant to leave the platform
WALK_FRAMES_PER_TILE = 24  # Dave walks 2 px every 3 ticks (configs/skills.yaml)
NOTE_MAX = 60


@dataclass(frozen=True)
class MoveScore:
    q: float | None  # cost to the goal after this option (lower is better); None when unknown
    regret: float | None  # q minus the best option's q; 0 for the best
    best: bool
    p_ok: float  # Laplace success probability of the move from here
    attempts: int
    successes: int
    fatal: int
    land: str | None  # the platform it is expected to end on
    mode: str  # goal | explore
    reason: str | None = None  # why q is unknown

    def note(self) -> str:
        stats = (f"ok {self.successes}/{self.attempts}" + (f", died {self.fatal}" if self.fatal else "")
                 if self.attempts else "untried")
        if self.q is None:
            text = f"score: ? ({self.reason}; {stats})"
        elif self.best:
            text = f"score: best ({self.q:.1f} to {self.mode}; {stats})"
        else:
            text = f"score: +{self.regret:.1f} vs best ({stats})"
        return text[:NOTE_MAX]

    def view(self) -> dict[str, Any]:
        return {"q": None if self.q is None else round(self.q, 3),
                "regret": None if self.regret is None else round(self.regret, 3), "best": self.best,
                "p_ok": round(self.p_ok, 3), "attempts": self.attempts, "fatal": self.fatal,
                "land": self.land, "mode": self.mode}


def position_values(graph: WorldGraph, target: str | None, inventory: dict[str, int] | None,
                    cfg: GraphConfig, target_ref: str | None = None) -> tuple[dict[str, float], str]:
    """({platform: V}, mode): costs to ``target`` (mode ``goal``), or, when it is unknown or
    needs items not held, to the nearest platform with an unexplored side (``explore``)."""
    items = held_items(inventory)
    reverse = graph.g.reverse(copy=False)

    def weight(v: str, u: str, edges: dict) -> float | None:
        # Reversed: the original edge runs u -> v, so the credit is the start platform u's.
        credit = graph.g.nodes[u].get("credit") if target_ref else None
        best = _best(edges, items, cfg, credit, target_ref)
        return None if best is None else best[1]

    if target is not None:
        target = graph.resolve(target)
        if target in graph.g and all(i in items for i in target_requirements(graph, target)):
            return nx.single_source_dijkstra_path_length(reverse, target, weight=weight), "goal"
    frontier = [n for n, d in graph.g.nodes(data=True) if d["open_left"] or d["open_right"]]
    if not frontier:
        return {}, "explore"
    return nx.multi_source_dijkstra_path_length(reverse, frontier, weight=weight), "explore"


def _evidence(graph: WorldGraph, here: str, skill: str, context: list[str]) -> tuple[dict[str, Any], str | None]:
    """The move's outcomes from ``here`` with the held items, summed over its edges and the
    platform's unattributed failures, and the platform it most often landed on."""
    agg = {"attempts": 0, "successes": 0, "failures": 0, "fatal": 0, "frames_total": 0}
    land, most = None, 0
    for _, target, edge in graph.g.out_edges(here, data=True):
        if edge["skill"] != skill or edge["inventory_context"] != context:
            continue
        for field in agg:
            agg[field] += edge[field]
        if edge["successes"] > most:
            land, most = target, edge["successes"]
    failed = graph.g.nodes[here]["failed_attempts"].get(edge_key(skill, tuple(context)))
    if failed:
        agg["attempts"] += failed["attempts"]
        agg["failures"] += failed["attempts"]
        agg["fatal"] += failed["fatal"]
    return agg, land


def _stay_cost(agg: dict[str, Any], frames: int, cfg: GraphConfig) -> float:
    """Time plus death risk of an action that stays on the platform (no uncertainty: no edge)."""
    alive = 1.0 - agg["fatal"] / (agg["attempts"] + 1)
    p = min(max(alive, cfg.p_min), cfg.p_max)
    return cfg.weights.time * frames / cfg.reference_frames + cfg.weights.risk * -math.log(p)


def score_moves(graph: WorldGraph, here: str, values: dict[str, float], mode: str,
                candidates: list[SkillCandidate], ends: dict[str, tuple[int, int] | None],
                inventory: dict[str, int] | None, cfg: GraphConfig, target_ref: str | None = None,
                goal: tuple[str, int] | None = None, col: int | None = None,
                hits: frozenset[str] = frozenset()) -> dict[str, MoveScore]:
    """One ``MoveScore`` per candidate, from platform ``here`` (``ends``: each candidate's
    estimated standing cell, from the reach map). ``goal`` (target platform, goal column) and
    ``col`` (Dave's column) add the walk along the target platform to the goal tile, so options
    that end on it are told apart (``V`` is per platform). ``hits``: the shooting candidates whose
    bullet is predicted to hit a monster; they get the whole ``kill_bonus``."""
    here = graph.resolve(here)

    def walk(land: str, end: tuple[int, int] | None) -> float:
        if goal is None or mode != "goal" or land != graph.resolve(goal[0]):
            return 0.0
        node = graph.g.nodes[land]
        at = (end[0] if end is not None and graph.node_at(node["level_id"], end[1], end[0]) == land
              else col if land == here and col is not None
              else min(max(goal[1], node["col_min"]), node["col_max"]))
        return cfg.weights.time * WALK_FRAMES_PER_TILE * abs(at - goal[1]) / cfg.reference_frames

    data = graph.g.nodes[here]
    context = sorted(held_items(inventory))
    credit = data.get("credit") if target_ref else None
    raw: dict[str, tuple[float | None, dict[str, Any], str | None, str | None]] = {}
    for c in candidates:
        agg, land = _evidence(graph, here, c.skill, context)
        end = ends.get(c.candidate_id)
        reason = None
        if land is not None:
            cost: float | None = edge_cost(agg, cfg)
        elif c.skill in STAY_SKILLS:
            land, cost, end = here, _stay_cost(agg, c.max_frames, cfg), None
        else:
            land = None if end is None else graph.node_at(data["level_id"], end[1], end[0])
            if end is None:
                cost, reason = None, "no safe landing"
            elif land is None:
                cost, reason = None, "lands off the map"
            elif land == here:
                cost = _stay_cost(agg, c.max_frames, cfg)
            else:
                # Untried (no successful edge): the time term uses the skill's frame cap.
                cost = edge_cost(agg, cfg) + cfg.weights.time * c.max_frames / cfg.reference_frames
        if cost is not None and land != here and target_ref and credit:
            cost *= credit_factor(credit.get(f"{c.skill}|{target_ref}"), cfg)
        if cost is not None and c.candidate_id in hits:
            cost -= cfg.kill_bonus  # the shot is predicted to hit (control/threats.py shot)
        elif cost is not None and c.skill in SHOOT_SKILLS and data.get("shots"):
            cost -= cfg.kill_bonus * data.get("kills", 0) / data["shots"]
        q = None
        if cost is not None and land is not None:
            if land in values:
                q = max(cost, 0.0) + values[land] + walk(land, end)
            else:
                reason = f"no known way to the {mode} from there"
        raw[c.candidate_id] = (q, agg, land, reason)
    known = [q for q, *_ in raw.values() if q is not None]
    top = min(known) if known else None
    out = {}
    for cid, (q, agg, land, reason) in raw.items():
        out[cid] = MoveScore(q=q, regret=None if q is None or top is None else q - top,
                             best=q is not None and q == top, p_ok=success_probability(agg),
                             attempts=agg["attempts"], successes=agg["successes"], fatal=agg["fatal"],
                             land=land, mode=mode, reason=None if q is not None else reason or "unknown")
    return out
