"""Deterministic route search over the learned world graph (never LLM traversal).

Edge cost, normalized (docs/graph.md):

    cost = wt * expected_frames / reference_frames
         + wr * -log(clip(success_p, p_min, p_max))
         + wu * 1 / (attempts + 1)

With a goal's ``target_ref``, an edge's cost is also scaled by its start platform's goal credit
for that skill and target (control/credit.py; ``credit_factor``): moves that led to the target in
past goals cost less, moves tried for it that never led there cost more.

The reliability term treats edge outcomes as independent. Correlated failures and
state-dependent enemy behaviour violate that, so it is an approximation.
Dijkstra suits these non-negative additive costs. Between two nodes the cheapest usable
parallel edge is used. Edges whose inventory context is not held are filtered out first.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

import networkx as nx

from dave_agent.config import GraphConfig
from dave_agent.memory.graph import WorldGraph, expected_frames, success_probability

# The exit door completes the level only while holding the trophy: verified for deadly-dave
# (docs/feasibility.md section 3) and defined by the fixture rules.
EXIT_REQUIRES = ("trophy",)


@dataclass(frozen=True)
class RouteStep:
    source: str
    target: str
    key: str
    skill: str
    cost: float


@dataclass(frozen=True)
class Route:
    status: str  # found | at_target | unreachable
    start: str
    target: str
    nodes: tuple[str, ...] = ()
    steps: tuple[RouteStep, ...] = ()
    cost: float | None = None
    reason: str | None = None
    frontier: tuple[str, ...] = field(default=())


def held_items(inventory: dict[str, int] | None) -> frozenset[str]:
    return frozenset(k for k, v in (inventory or {}).items() if v > 0)


def edge_cost(edge: dict[str, Any], cfg: GraphConfig) -> float:
    p = min(max(success_probability(edge), cfg.p_min), cfg.p_max)
    return (cfg.weights.time * expected_frames(edge) / cfg.reference_frames
            + cfg.weights.risk * -math.log(p)
            + cfg.weights.uncertainty / (edge["attempts"] + 1))


def credit_factor(rec: dict[str, int] | None, cfg: GraphConfig) -> float:
    """Cost multiplier from one ``skill|target`` credit record: 1 with no past goals, down to
    ``1 - credit_bonus`` when every goal that used the move was achieved, up to
    ``1 + credit_penalty`` when none was."""
    if not rec or not rec.get("goals"):
        return 1.0
    rate = rec["reached"] / rec["goals"]
    return 1.0 + cfg.credit_penalty * (1.0 - rate) - cfg.credit_bonus * rate


def _usable(edge: dict[str, Any], items: frozenset[str]) -> bool:
    return set(edge["inventory_context"]) <= items


def _best(edges: dict[str, dict[str, Any]], items: frozenset[str], cfg: GraphConfig,
          credit: dict[str, Any] | None = None, target_ref: str | None = None) -> tuple[str, float] | None:
    def cost(d: dict[str, Any]) -> float:
        factor = credit_factor(credit.get(f"{d['skill']}|{target_ref}"), cfg) if credit and target_ref else 1.0
        return edge_cost(d, cfg) * factor

    options = sorted((cost(d), k) for k, d in edges.items() if _usable(d, items))
    return (options[0][1], options[0][0]) if options else None


def target_requirements(graph: WorldGraph, node: str) -> tuple[str, ...]:
    items = graph.g.nodes[node]["items"]
    return EXIT_REQUIRES if any(i["kind"] == "exit" for i in items) else ()


def find_route(graph: WorldGraph, start: str, target: str, inventory: dict[str, int] | None,
               cfg: GraphConfig, target_ref: str | None = None) -> Route:
    """The cheapest learned route; ``target_ref`` (the goal's) weighs edges by goal credit."""
    start, target = graph.resolve(start), graph.resolve(target)
    items = held_items(inventory)

    def credit(u: str) -> dict[str, Any] | None:
        return graph.g.nodes[u].get("credit") if target_ref else None

    def weight(u: str, v: str, edges: dict) -> float | None:
        best = _best(edges, items, cfg, credit(u), target_ref)
        return None if best is None else best[1]

    if start not in graph.g:
        return Route("unreachable", start, target, reason="unknown_start")
    lengths = nx.single_source_dijkstra_path_length(graph.g, start, weight=weight)
    if target not in graph.g:
        return Route("unreachable", start, target, reason="unknown_target", frontier=_frontier(graph, lengths))
    missing = [i for i in target_requirements(graph, target) if i not in items]
    if missing:
        return Route("unreachable", start, target, reason=f"requires:{','.join(missing)}",
                     frontier=_frontier(graph, lengths))
    if start == target:
        return Route("at_target", start, target, nodes=(start,), cost=0.0)
    if target not in lengths:
        return Route("unreachable", start, target, reason="no_verified_route", frontier=_frontier(graph, lengths))
    path = nx.dijkstra_path(graph.g, start, target, weight=weight)
    steps = []
    for u, v in zip(path, path[1:]):
        key, cost = _best(graph.g.get_edge_data(u, v), items, cfg, credit(u), target_ref)  # type: ignore[misc]
        steps.append(RouteStep(u, v, key, graph.g.edges[u, v, key]["skill"], cost))
    return Route("found", start, target, nodes=tuple(path), steps=tuple(steps), cost=lengths[target])


def _frontier(graph: WorldGraph, lengths: dict[str, float]) -> tuple[str, ...]:
    """Where exploration can continue: reachable segments with an unexplored (view-clipped)
    side, by cost; then discovered segments never stood on, by id."""
    reachable = sorted((cost, n) for n, cost in lengths.items()
                       if graph.g.nodes[n]["open_left"] or graph.g.nodes[n]["open_right"])
    unvisited = sorted(n for n, d in graph.g.nodes(data=True) if not d["visited"] and n not in lengths)
    return tuple(n for _, n in reachable) + tuple(unvisited)


class RouteTracker:
    """Follows a route at decision boundaries and says when to replan (never per frame)."""

    def __init__(self, route: Route, graph: WorldGraph, inventory: dict[str, int] | None) -> None:
        self.route = route
        self.index = 0
        self._version = graph.topology_version
        self._items = held_items(inventory)

    @property
    def next_step(self) -> RouteStep | None:
        steps = self.route.steps
        return steps[self.index] if self.route.status == "found" and self.index < len(steps) else None

    def update(self, graph: WorldGraph, node: str | None, inventory: dict[str, int] | None,
               step_failed: bool = False) -> str | None:
        """Advance along the route. Returns a replan reason, or None to keep following it."""
        if self.route.status != "found":
            return f"no_route:{self.route.reason or self.route.status}"
        node = graph.resolve(node) if node is not None else None
        if node == graph.resolve(self.route.target):
            return "target_reached"
        if step_failed:
            return "edge_failed"
        if held_items(inventory) != self._items:
            return "inventory_changed"
        if graph.topology_version != self._version:
            return "topology_changed"
        if node is None:
            return None  # airborne or unmapped: wait for a landing
        nodes = [graph.resolve(n) for n in self.route.nodes]
        if node == nodes[self.index]:
            return None
        if node in nodes[self.index + 1:]:
            self.index = nodes.index(node, self.index + 1)
            return None
        return "off_route"
