"""Learned world graph: standable platform segments as nodes, observed skill transitions as edges.

Built only from local observations and executed skills (see docs/graph.md):

- Segmentation is deterministic. A cell is *standable* when it is inside the observed region,
  holds no solid or hazard tile, and sits directly above an observed solid tile. A *segment*
  is a maximal horizontal run of standable cells. A segment that overlaps or touches an
  existing node on the same level and row is merged into it, and the older id is kept.
- An edge A->B is created only after a completed skill that started on A and ended standing
  on B. Failures attach to an edge only when its target is identifiable; otherwise they are
  kept on the start node. Nothing is ever added for an unseen destination.
- Moving enemies are never topology; they live in working memory.
"""

from __future__ import annotations

from typing import Any

import networkx as nx

from dave_agent.control.skills import ExecutionResult
from dave_agent.control.threats import contact_cause
from dave_agent.memory.working import player_tile
from dave_agent.schemas import Observation

# Tile kinds recorded as known items of a segment (as last observed).
ITEM_KINDS = frozenset({"collectible", "required_item", "item", "exit", "climbable"})
BLOCKING_KINDS = frozenset({"solid", "hazard"})
STANDING_STATES = frozenset({"standing", "walking"})


def inventory_context(obs: Observation) -> tuple[str, ...]:
    """Items held (value > 0) when a skill starts: the edge's inventory precondition context."""
    return tuple(sorted(k for k, v in (obs.inventory or {}).items() if v > 0))


def edge_key(skill: str, context: tuple[str, ...]) -> str:
    return f"{skill}|{','.join(context)}"


def segments(obs: Observation) -> list[dict[str, Any]]:
    """Standable runs in one observation, ordered by row then column."""
    kinds: dict[tuple[int, int], str] = {(t.pos.col, t.pos.row): t.kind for t in obs.tiles}
    region = obs.region
    found: list[dict[str, Any]] = []
    for row in range(region.min.row, region.max.row):  # the row below must be observed too
        run: list[int] = []
        for col in range(region.min.col, region.max.col + 2):
            standable = (
                col <= region.max.col
                and kinds.get((col, row)) not in BLOCKING_KINDS
                and kinds.get((col, row + 1)) == "solid"
            )
            if standable:
                run.append(col)
            elif run:
                found.append({
                    "row": row, "col_min": run[0], "col_max": run[-1],
                    "open_left": run[0] == region.min.col, "open_right": run[-1] == region.max.col,
                })
                run = []
    return found


class WorldGraph:
    """The learned graph of one level. ``level_id`` None accepts any level (tests and legacy
    checkpoints); a ``GraphStore`` always sets it, so observing another level is a bug."""

    def __init__(self, adapter: str, build_id: str, observation_policy: str, evidence_limit: int = 20,
                 level_id: str | None = None) -> None:
        self.adapter, self.build_id, self.observation_policy = adapter, build_id, observation_policy
        self.evidence_limit = evidence_limit
        self.level_id = level_id
        self.g = nx.MultiDiGraph()
        self.aliases: dict[str, str] = {}
        self.suggestions: list[dict[str, Any]] = []
        self.scenarios: set[str] = set()
        self.lineage: list[dict[str, Any]] = []
        self.parent_sha256: str | None = None
        self.topology_version = 0
        self.unanchored = 0
        self._last_signature: tuple | None = None

    # -- nodes -------------------------------------------------------------
    def resolve(self, node: str) -> str:
        while node in self.aliases:
            node = self.aliases[node]
        return node

    def node_at(self, level_id: str, row: int, col: int) -> str | None:
        for node, data in self.g.nodes(data=True):
            if data["level_id"] == level_id and data["row"] == row and data["col_min"] <= col <= data["col_max"]:
                return node
        return None

    def observe(self, obs: Observation) -> None:
        """Fold the observation's platform segments and items into the graph, and mark the
        segment Dave stands on as visited. Re-segments only when the view or tiles changed."""
        if self.level_id is not None and obs.level_id != self.level_id:
            raise ValueError(f"graph of {self.level_id} cannot observe {obs.level_id}")
        signature = (obs.level_id, obs.region, obs.tiles)
        if signature != self._last_signature:
            self._last_signature = signature
            for seg in segments(obs):
                self._merge_segment(obs, seg)
            self._refresh_items(obs)
        node = self.locate(obs)
        if node is not None:
            data = self.g.nodes[node]
            data["visited"] = True
            data["last_verified_frame"] = obs.frame

    def _merge_segment(self, obs: Observation, seg: dict[str, Any]) -> None:
        level, row = obs.level_id, seg["row"]
        touching = [
            n for n, d in self.g.nodes(data=True)
            if d["level_id"] == level and d["row"] == row
            and d["col_min"] - 1 <= seg["col_max"] and seg["col_min"] <= d["col_max"] + 1
        ]
        if not touching:
            node = f"{level}:r{row}:c{seg['col_min']}"
            suffix = 1
            while node in self.g or node in self.aliases:
                suffix += 1
                node = f"{level}:r{row}:c{seg['col_min']}#{suffix}"
            self.g.add_node(node, level_id=level, row=row, col_min=seg["col_min"], col_max=seg["col_max"],
                            open_left=seg["open_left"], open_right=seg["open_right"], surface="solid",
                            visited=False, items=[], stays=0, inconclusive=0, failed_attempts={},
                            evidence=[], evidence_count=0, incidents=[], last_verified_frame=obs.frame)
            self.topology_version += 1
            return
        keep, *others = touching  # node iteration order is creation order: the oldest id survives
        for other in others:
            self._absorb(keep, other)
        data = self.g.nodes[keep]
        before = (data["col_min"], data["col_max"], data["open_left"], data["open_right"])
        if seg["col_min"] < data["col_min"]:
            data["col_min"], data["open_left"] = seg["col_min"], seg["open_left"]
        elif seg["col_min"] == data["col_min"]:
            data["open_left"] = data["open_left"] and seg["open_left"]
        if seg["col_max"] > data["col_max"]:
            data["col_max"], data["open_right"] = seg["col_max"], seg["open_right"]
        elif seg["col_max"] == data["col_max"]:
            data["open_right"] = data["open_right"] and seg["open_right"]
        data["last_verified_frame"] = obs.frame
        if others or before != (data["col_min"], data["col_max"], data["open_left"], data["open_right"]):
            self.topology_version += 1

    def _absorb(self, keep: str, other: str) -> None:
        """Merge node ``other`` into ``keep``: one platform seen in two partial views."""
        a, b = self.g.nodes[keep], self.g.nodes[other]
        if b["col_min"] < a["col_min"]:
            a["col_min"], a["open_left"] = b["col_min"], b["open_left"]
        if b["col_max"] > a["col_max"]:
            a["col_max"], a["open_right"] = b["col_max"], b["open_right"]
        a["visited"] = a["visited"] or b["visited"]
        a["items"] = sorted(a["items"] + b["items"], key=lambda i: (i["col"], i["row"], i["kind"], i["name"]))
        a["stays"] += b["stays"]
        a["inconclusive"] += b["inconclusive"]
        for key, rec in b["failed_attempts"].items():
            self._add_failure_record(a["failed_attempts"], key, rec["attempts"], rec["fatal"], rec["evidence"])
        self._add_evidence(a, b["evidence"], b["evidence_count"])
        a["incidents"] = (a.get("incidents", []) + b.get("incidents", []))[-self.evidence_limit:]
        for u, v, key, data in list(self.g.in_edges(other, keys=True, data=True)) + \
                list(self.g.out_edges(other, keys=True, data=True)):
            u2, v2 = (keep if u == other else u), (keep if v == other else v)
            if u2 == v2:  # the transition stayed on one platform after all
                self.g.nodes[keep]["stays"] += data["successes"]
                continue
            if self.g.has_edge(u2, v2, key):
                self._combine_edges(self.g.edges[u2, v2, key], data)
            else:
                self.g.add_edge(u2, v2, key=key, **data)
        self.g.remove_node(other)
        self.aliases[other] = keep

    def _refresh_items(self, obs: Observation) -> None:
        """Items on each node's cells within the current view, replaced by what is seen now."""
        region = obs.region
        visible = {(t.pos.col, t.pos.row): t for t in obs.tiles if t.kind in ITEM_KINDS}
        for _, data in self.g.nodes(data=True):
            if data["level_id"] != obs.level_id or not region.min.row <= data["row"] <= region.max.row:
                continue
            lo, hi = max(data["col_min"], region.min.col), min(data["col_max"], region.max.col)
            if lo > hi:
                continue
            kept = [i for i in data["items"] if not lo <= i["col"] <= hi]
            seen = [{"kind": t.kind, "name": t.name, "col": c, "row": r}
                    for (c, r), t in visible.items() if r == data["row"] and lo <= c <= hi]
            data["items"] = sorted(kept + seen, key=lambda i: (i["col"], i["row"], i["kind"], i["name"]))

    def locate(self, obs: Observation) -> str | None:
        """The segment Dave stands on, or None when airborne, not standing, or unmapped.
        Dave's 20 px hitbox can straddle two cells, so the neighbouring cells are tried too."""
        if not obs.grounded or obs.player_state not in STANDING_STATES or obs.player_position is None:
            return None
        tile = player_tile(obs.player_position)
        for col in (tile.col, tile.col - 1, tile.col + 1):
            node = self.node_at(obs.level_id, tile.row, col)
            if node is not None:
                return node
        return None

    # -- edges -------------------------------------------------------------
    def record_execution(self, start: Observation, run: ExecutionResult, ref: str) -> str:
        """Update evidence from one executed skill. Returns what was recorded:
        success | stay | inconclusive | failure_edge | failure_node | unanchored | level_changed.
        A fatal skill also leaves an incident (what touched Dave, and where) on its start node."""
        self.observe(start)
        if run.observation.level_id != start.level_id:
            return "level_changed"  # a transition between levels is never an edge of either graph
        self.observe(run.observation)
        source = self.locate(start)
        if source is None:
            self.unanchored += 1
            return "unanchored"
        context = inventory_context(start)
        key = edge_key(run.skill, context)
        fatal = run.reason == "hazard_contact" or any(e.event_type == "death" for e in run.events)
        self._add_evidence(self.g.nodes[source], [ref], 1)
        if fatal:
            self._add_incident(self.g.nodes[source], run, ref)

        if run.outcome == "completed":
            target = self.locate(run.observation)
            if target is None:
                # Completed but not standing on a mapped segment (e.g. still falling): no
                # transition was observed, and the skill did not fail either.
                self.g.nodes[source]["inconclusive"] += 1
                return "inconclusive"
            if target == source:
                self.g.nodes[source]["stays"] += 1
                return "stay"
            self._add_evidence(self.g.nodes[target], [ref], 1)
            if self.g.has_edge(source, target, key):
                edge = self.g.edges[source, target, key]
            else:
                self.g.add_edge(source, target, key=key, skill=run.skill, inventory_context=list(context),
                                validation="observed", attempts=0, successes=0, failures=0, fatal=0,
                                frames_total=0, evidence=[], evidence_count=0, last_outcome=None)
                self.topology_version += 1
                edge = self.g.edges[source, target, key]
            edge["attempts"] += 1
            edge["successes"] += 1
            edge["frames_total"] += run.frames
            edge["last_outcome"] = "success"
            self._add_evidence(edge, [ref], 1)
            return "success"

        candidates = [(v, k) for _, v, k in self.g.out_edges(source, keys=True) if k == key]
        if len(candidates) == 1:
            (target, _), = candidates
            edge = self.g.edges[source, target, key]
            edge["attempts"] += 1
            edge["failures"] += 1
            edge["fatal"] += int(fatal)
            edge["last_outcome"] = "fatal" if fatal else "failure"
            self._add_evidence(edge, [ref], 1)
            return "failure_edge"
        self._add_failure_record(self.g.nodes[source]["failed_attempts"], key, 1, int(fatal), [ref])
        return "failure_node"

    def skill_evidence(self, obs: Observation) -> dict[str, dict[str, Any]]:
        """Per skill, the recorded outcomes of starting it from the segment Dave stands on with the
        same held items: attempts, successes and fatal over the out-edges and the segment's failed
        attempts, and ``lands`` (row, col_min, col_max), the segment most often reached. Empty when
        Dave is not on a mapped segment."""
        source = self.locate(obs)
        if source is None:
            return {}
        context = list(inventory_context(obs))
        out: dict[str, dict[str, Any]] = {}

        def entry(skill: str) -> dict[str, Any]:
            return out.setdefault(skill, {"attempts": 0, "successes": 0, "fatal": 0, "lands": None})

        best: dict[str, int] = {}
        for _, target, edge in self.g.out_edges(source, data=True):
            if edge["inventory_context"] != context:
                continue
            rec = entry(edge["skill"])
            for field in ("attempts", "successes", "fatal"):
                rec[field] += edge[field]
            if edge["successes"] > best.get(edge["skill"], 0):
                best[edge["skill"]] = edge["successes"]
                node = self.g.nodes[target]
                rec["lands"] = (node["row"], node["col_min"], node["col_max"])
        for key, failed in self.g.nodes[source]["failed_attempts"].items():
            skill, _, items = key.partition("|")
            if (items.split(",") if items else []) != context:
                continue
            rec = entry(skill)
            rec["attempts"] += failed["attempts"]
            rec["fatal"] += failed["fatal"]
        return out

    def suggest(self, level_id: str, col: int, row: int, source: str, rationale: str = "") -> None:
        """Record an exploration target proposed by a model or planner. Suggestions are
        never topology: they create no nodes or edges and route search ignores them."""
        if source == "observed":
            raise ValueError("suggestions come from models or planners, not observation")
        self.suggestions.append({"level_id": level_id, "col": col, "row": row, "source": source,
                                 "rationale": rationale})

    # -- helpers ------------------------------------------------------------
    def _add_evidence(self, item: dict[str, Any], refs: list[str], count: int) -> None:
        item["evidence"] = (item["evidence"] + [r for r in refs if r not in item["evidence"]])[-self.evidence_limit:]
        item["evidence_count"] += count

    def _add_incident(self, node: dict[str, Any], run: ExecutionResult, ref: str) -> None:
        """What touched Dave on the first burning frame of a fatal skill, and where."""
        hit = next((s.observation for s in run.steps if s.observation.player_state == "burning"), run.observation)
        cause, tile = contact_cause(hit)
        incidents = node.setdefault("incidents", [])
        incidents.append({"skill": run.skill, "cause": cause, "tile": tile, "ref": ref})
        del incidents[:-self.evidence_limit]

    def _add_failure_record(self, records: dict, key: str, attempts: int, fatal: int, refs: list[str]) -> None:
        rec = records.setdefault(key, {"attempts": 0, "fatal": 0, "evidence": [], "evidence_count": 0})
        rec["attempts"] += attempts
        rec["fatal"] += fatal
        self._add_evidence(rec, refs, len(refs))

    def _combine_edges(self, a: dict[str, Any], b: dict[str, Any]) -> None:
        for field in ("attempts", "successes", "failures", "fatal", "frames_total"):
            a[field] += b[field]
        self._add_evidence(a, b["evidence"], b["evidence_count"])

    def add_lineage(self, run_id: str, episode_key: str, arm: str, scenario_id: str) -> None:
        self.scenarios.add(scenario_id)
        self.lineage.append({"run_id": run_id, "episode_key": episode_key, "arm": arm, "scenario_id": scenario_id})

    def counts(self) -> dict[str, int]:
        edges = [d for _, _, d in self.g.edges(data=True)]
        return {
            "nodes": self.g.number_of_nodes(),
            "visited_nodes": sum(1 for _, d in self.g.nodes(data=True) if d["visited"]),
            "edges": len(edges),
            "attempts": sum(e["attempts"] for e in edges),
            "successes": sum(e["successes"] for e in edges),
            "fatal": sum(e["fatal"] for e in edges),
            "node_failures": sum(r["attempts"] for _, d in self.g.nodes(data=True)
                                 for r in d["failed_attempts"].values()),
            "unanchored": self.unanchored,
            "suggestions": len(self.suggestions),
        }


def success_probability(edge: dict[str, Any]) -> float:
    """Laplace prior: (successes + 1) / (attempts + 2). An untried edge is 0.5, not certain."""
    return (edge["successes"] + 1) / (edge["attempts"] + 2)


def expected_frames(edge: dict[str, Any]) -> float:
    return edge["frames_total"] / edge["successes"] if edge["successes"] else 0.0


class GraphStore:
    """One ``WorldGraph`` per level, created on first sight of the level (docs/graph.md). The
    episode loop and the goal manager use it like a graph: every call is routed by the
    observation's ``level_id``, so no node, edge, frontier or route ever spans two levels."""

    def __init__(self, adapter: str, build_id: str, observation_policy: str, evidence_limit: int = 20) -> None:
        self.adapter, self.build_id, self.observation_policy = adapter, build_id, observation_policy
        self.evidence_limit = evidence_limit
        self.levels: dict[str, WorldGraph] = {}

    def for_level(self, level_id: str) -> WorldGraph:
        if level_id not in self.levels:
            self.levels[level_id] = WorldGraph(self.adapter, self.build_id, self.observation_policy,
                                               self.evidence_limit, level_id)
        return self.levels[level_id]

    def get(self, level_id: str) -> WorldGraph | None:
        return self.levels.get(level_id)

    def observe(self, obs: Observation) -> None:
        self.for_level(obs.level_id).observe(obs)

    def record_execution(self, start: Observation, run: ExecutionResult, ref: str) -> str:
        if run.observation.level_id != start.level_id:
            self.observe(run.observation)  # the new level's first view, in its own graph
        return self.for_level(start.level_id).record_execution(start, run, ref)

    def skill_evidence(self, obs: Observation) -> dict[str, dict[str, Any]]:
        graph = self.get(obs.level_id)
        return {} if graph is None else graph.skill_evidence(obs)

    def add_lineage(self, run_id: str, episode_key: str, arm: str, scenario_id: str) -> None:
        for graph in self.levels.values():
            graph.add_lineage(run_id, episode_key, arm, scenario_id)

    @property
    def lineage(self) -> list[dict[str, Any]]:
        seen, out = set(), []
        for graph in self.levels.values():
            for entry in graph.lineage:
                key = (entry["run_id"], entry["episode_key"])
                if key not in seen:
                    seen.add(key)
                    out.append(entry)
        return out

    def counts(self) -> dict[str, Any]:
        total: dict[str, Any] = dict.fromkeys(WorldGraph("", "", "").counts(), 0)
        for graph in self.levels.values():
            for name, value in graph.counts().items():
                total[name] += value
        return {"levels": sorted(self.levels), **total}
