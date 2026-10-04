"""Deterministic goal manager: candidate goals, goal lifecycle and shared planning triggers.

Lifecycle: absent -> planned (a validated choice) -> active (in working memory) -> achieved,
failed or expired. The planner only chooses among candidates generated here from observed
facts, so an unknown target is rejected by a membership check. Every arm runs this same code
with the same settings. The waypoint is the next landing spot on an estimated route over the
observed tiles (control/reach.py, when the adapter has a reach envelope), else the target;
graph-enabled arms use their learned route instead whenever it has one (see docs/planner.md).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from dave_agent.config import GraphConfig, PlanningConfig, ReachConfig, SkillSpec, ThreatConfig
from dave_agent.control.attempts import AttemptLog
from dave_agent.control.level_map import render
from dave_agent.control.platforms import Platforms
from dave_agent.control.reach import DIRECTIONS, TILE, Cell, ReachMap, estimate_end_at, frontier, next_landing, \
    next_waypoint, trace_skill
from dave_agent.control.skills import ExecutionResult
from dave_agent.control.threats import assess, contact_cause, screen
from dave_agent.memory.graph import BLOCKING_KINDS, STANDING_STATES, GraphStore, WorldGraph
from dave_agent.memory.routes import Route, RouteTracker, find_route, held_items
from dave_agent.memory.working import FAILED_OUTCOMES, WorkingMemory, player_tile
from dave_agent.models.planner import (
    GoalCandidate,
    PlanChoice,
    PlanningRequest,
    PlanOutputError,
    StrategicPlanner,
    parse_plan,
    priority,
    rule_choice,
)
from dave_agent.schemas import Event, Goal, ModelCallRecord, Observation, SkillCandidate, TilePos

TARGET_KINDS = frozenset({"required_item", "item", "collectible", "exit"})
# Hard triggers always plan (within the call cap); soft triggers are debounced.
HARD_TRIGGERS = frozenset({"no_goal", "goal_achieved", "goal_failed", "goal_expired"})
NO_PLANNING_STATES = frozenset({"burning", "dead"})  # inputs are ignored; plan after respawn
NEARBY_TILES = 3  # hazards/monsters within this many tiles are summarized for the planner
MAX_NEARBY = 10


@dataclass
class KnownTarget:
    kind: str
    name: str
    col: int
    row: int
    present: bool
    last_seen_frame: int


class TargetMemory:
    """Targets and hazards observed so far this episode on the current level. Only observed
    facts: a target is marked absent once its tile is in view without it. Same for every arm."""

    def __init__(self) -> None:
        self.level_id: str | None = None
        self.targets: dict[tuple[int, int, str], KnownTarget] = {}
        self.hazards: set[tuple[int, int]] = set()
        self.seen_cols: tuple[int, int] | None = None

    def reset(self, obs: Observation) -> None:
        self.level_id, self.targets, self.hazards, self.seen_cols = obs.level_id, {}, set(), None
        self.observe(obs)

    def observe(self, obs: Observation) -> None:
        if obs.level_id != self.level_id:
            self.level_id, self.targets, self.hazards, self.seen_cols = obs.level_id, {}, set(), None
        region = obs.region
        visible = {(t.pos.col, t.pos.row, t.kind): t for t in obs.tiles if t.kind in TARGET_KINDS}
        for key, target in self.targets.items():
            if region.contains(TilePos(col=target.col, row=target.row)):
                target.present = key in visible
                if target.present:
                    target.last_seen_frame = obs.frame
        for key, tile in visible.items():
            if key not in self.targets:
                self.targets[key] = KnownTarget(tile.kind, tile.name, key[0], key[1], True, obs.frame)
        self.hazards |= {(t.pos.col, t.pos.row) for t in obs.tiles if t.kind == "hazard"}
        lo, hi = region.min.col, region.max.col
        self.seen_cols = (lo, hi) if self.seen_cols is None else (min(self.seen_cols[0], lo),
                                                                  max(self.seen_cols[1], hi))

    def present(self, kinds: frozenset[str] | set[str]) -> list[KnownTarget]:
        return [t for t in self.targets.values() if t.present and t.kind in kinds]

    def hazard_near(self, col: int, row: int) -> bool:
        return any(abs(c - col) <= 1 and abs(r - row) <= 1 for c, r in self.hazards)


# -- candidate goals ---------------------------------------------------------------------
def _direction(here: TilePos, col: int, row: int) -> str:
    dx, dy = col - here.col, row - here.row
    parts = []
    if dx:
        parts.append(f"{abs(dx)} tiles {'right' if dx > 0 else 'left'}")
    if dy:
        parts.append(f"{abs(dy)} rows {'down' if dy > 0 else 'up'}")
    return ", ".join(parts) or "here"


def _candidate(obs: Observation, targets: TargetMemory, here: TilePos, goal_type: str, kind: str, name: str,
               col: int, row: int, predicate: str, description: str) -> GoalCandidate:
    cid = f"{goal_type}:{name}" if goal_type in ("explore", "recover") else f"{goal_type}:{name}:c{col}:r{row}"
    constraints = []
    if goal_type in ("collect", "reach") and targets.hazard_near(col, row):
        constraints.append("hazard_near_target")
    if not obs.region.contains(TilePos(col=col, row=row)):
        constraints.append("target_out_of_view")
    return GoalCandidate(candidate_id=cid, goal_type=goal_type, target_kind=kind, target_name=name,
                         target=TilePos(col=col, row=row),
                         description=f"{description} at tile ({col},{row}); {_direction(here, col, row)}",
                         success_predicate=predicate, constraints=tuple(constraints))


def goal_candidates(obs: Observation, targets: TargetMemory, cfg: PlanningConfig,
                    recover_tile: TilePos | None = None) -> tuple[GoalCandidate, ...]:
    """Deterministic candidate goals from observed facts, ordered by the fixed priority."""
    if obs.player_position is None:
        return ()
    here = player_tile(obs.player_position)
    held = held_items(obs.inventory)
    found: list[GoalCandidate] = []
    for t in targets.present({"exit"}):
        if "trophy" in held:  # verified rule: the door only works while holding the trophy
            found.append(_candidate(obs, targets, here, "reach", "exit", t.name, t.col, t.row, "level_complete",
                                    f"Go through the {t.name} (trophy held)"))
    for t in targets.present({"required_item", "item"}):
        found.append(_candidate(obs, targets, here, "collect", t.kind, t.name, t.col, t.row, "item_collected_at",
                                f"Collect the {t.name}"))
    loot = sorted(targets.present({"collectible"}),
                  key=lambda t: (abs(t.col - here.col) + abs(t.row - here.row), t.col, t.row))
    for t in loot[:cfg.nearest_collectibles]:
        found.append(_candidate(obs, targets, here, "collect", t.kind, t.name, t.col, t.row, "item_collected_at",
                                f"Collect {t.name} (score only)"))
    lo, hi = targets.seen_cols or (obs.region.min.col, obs.region.max.col)
    found.append(_candidate(obs, targets, here, "explore", "explore", "right", hi + 1, here.row, "area_discovered",
                            "Explore right to reveal map columns beyond those seen"))
    if lo > 0:
        found.append(_candidate(obs, targets, here, "explore", "explore", "left", lo - 1, here.row,
                                "area_discovered", "Explore left to reveal map columns beyond those seen"))
    if recover_tile is not None and recover_tile != here:
        found.append(_candidate(obs, targets, here, "recover", "recover", "safe", recover_tile.col, recover_tile.row,
                                "standing_at", "Return to the last tile where a skill ended safely"))
    return tuple(sorted(found, key=priority))  # stable: loot stays nearest-first


def recover_tile(memory: WorkingMemory) -> TilePos | None:
    """The end tile of the latest completed skill that left Dave standing."""
    for entry in reversed(memory.history):
        if entry.outcome == "completed" and entry.end_state in STANDING_STATES and entry.end_tile is not None:
            return entry.end_tile
    return None


# -- lifecycle ---------------------------------------------------------------------------
def evaluate(goal: Goal, level_id: str, obs: Observation, events: list[Event],
             targets: TargetMemory) -> tuple[str, str] | None:
    """(status, reason) when the goal has ended, else None. Status: achieved | failed | expired."""
    tile = _goal_tile(goal)
    if obs.level_id != level_id:
        return "failed", "level_changed"
    types = [e.event_type for e in events]
    if goal.success_predicate == "level_complete" and "level_complete" in types:
        return "achieved", "level_complete"
    if goal.success_predicate == "item_collected_at":
        if any(e.event_type == "item_collected" and e.location == tile for e in events):
            return "achieved", "item_collected"
        known = [t for (c, r, _), t in targets.targets.items() if (c, r) == (tile.col, tile.row)]
        if known and not any(t.present for t in known):
            return "failed", "target_invalid"
    if goal.success_predicate == "area_discovered":
        right = goal.target_ref == "explore:right"
        for e in events:
            if e.event_type == "area_discovered" and e.payload.get("level_id") == level_id:
                if (right and e.payload["max_col"] >= tile.col) or (not right and e.payload["min_col"] <= tile.col):
                    return "achieved", "area_discovered"
    if goal.success_predicate == "standing_at" and obs.player_position is not None:
        here = player_tile(obs.player_position)
        if obs.player_state in STANDING_STATES and here.row == tile.row and abs(here.col - tile.col) <= 1:
            return "achieved", "standing_at_target"
    if obs.terminal == "running" and goal.deadline_frame is not None and obs.frame >= goal.deadline_frame:
        return "expired", "deadline"
    return None


def _goal_tile(goal: Goal) -> TilePos:
    """The goal's final target tile, kept in its ``target:col,row`` constraint (graph arms move
    ``next_waypoint`` along the route, so the waypoint is not the target)."""
    for c in goal.constraints:
        if c.startswith("target:"):
            col, row = c[len("target:"):].split(",")
            return TilePos(col=int(col), row=int(row))
    raise ValueError(f"goal {goal.goal_id} has no target constraint")


# -- planning records ----------------------------------------------------------------------
@dataclass
class PlanningRecord:
    """One planning episode at a decision boundary (a planner call or a fallback)."""

    frame: int
    observation_id: int
    triggers: tuple[str, ...]
    request: PlanningRequest
    chosen: str
    fallback: bool
    fallback_reason: str | None
    attempts: int
    errors: list[str]
    goal_id: str
    route: dict[str, Any] | None
    route_ms: float
    model_ms: float


@dataclass
class PlanningStep:
    events: list[Event] = field(default_factory=list)
    calls: list[ModelCallRecord] = field(default_factory=list)
    record: PlanningRecord | None = None


def _route_summary(route: Route, graph: WorldGraph) -> dict[str, Any]:
    summary: dict[str, Any] = {"status": route.status}
    if route.status == "found":
        summary.update(steps=len(route.steps), cost=round(route.cost or 0.0, 3),
                       skills=[s.skill for s in route.steps][:5])
        # Deaths recorded on the route's platforms in past runs: what killed Dave, and where.
        incidents = [{"cause": i["cause"], "tile": i["tile"], "skill": i["skill"]}
                     for n in route.nodes[:-1] for i in graph.g.nodes[graph.resolve(n)].get("incidents", [])]
        if incidents:
            summary["incidents"] = incidents[-5:]
    elif route.status == "unreachable":
        summary.update(reason=route.reason, frontier=list(route.frontier[:3]))
    return summary


class GoalManager:
    def __init__(self, planner: StrategicPlanner, planning: PlanningConfig, max_retries: int,
                 graph: GraphStore | None = None, graph_cfg: GraphConfig | None = None,
                 reach: ReachConfig | None = None, threats: ThreatConfig | None = None) -> None:
        if (graph is None) != (graph_cfg is None):
            raise ValueError("graph and graph_cfg go together")
        self.planner, self.cfg, self.max_retries = planner, planning, max_retries
        self.graph, self.graph_cfg, self.reach, self.threats = graph, graph_cfg, reach, threats
        self.targets = TargetMemory()
        self._reset_state()

    def _reset_state(self) -> None:
        self.goal: Goal | None = None
        self.status = "absent"
        self.level_id: str | None = None
        self.calls = 0
        self.seq = 0
        self.last_call_frame: int | None = None
        self.previous: dict[str, Any] | None = None
        self._pending: set[str] = set()
        self._failures = 0
        self._held: frozenset[str] = frozenset()
        self._tracker: RouteTracker | None = None
        self._goal_start = 0
        self._cells: dict[tuple[int, int], str] = {}  # observed tiles this level (reach, map)
        self._cells_level: str | None = None
        self._waypoints: list[TilePos] = []  # the planner's waypoints still ahead, in order
        self.log = AttemptLog()  # what was tried on this level (control/attempts.py)
        self._stand: Cell | None = None  # Dave's last standing cell, and the waypoint he was heading for
        self._heading: Cell | None = None
        self.platforms: Platforms | None = None  # as last shown to the planner

    @property
    def level_graph(self) -> WorldGraph:
        """The learned graph of the current level (graph-enabled arms only)."""
        assert self.graph is not None and self.level_id is not None
        return self.graph.for_level(self.level_id)

    # -- observation feed ------------------------------------------------------------------
    def reset(self, obs: Observation, memory: WorkingMemory) -> PlanningStep:
        self._reset_state()
        self._learn(obs)
        self.targets.reset(obs)
        self.level_id = obs.level_id
        self._held = held_items(obs.inventory)
        self._pending.add("no_goal")
        return self._maybe_plan(obs, memory)

    def observe(self, obs: Observation) -> None:
        self.targets.observe(obs)

    def update(self, obs: Observation, memory: WorkingMemory, events: list[Event],
               run: ExecutionResult | None = None) -> PlanningStep:
        """After each executed skill: end the goal if due, collect triggers, maybe plan."""
        step = PlanningStep()
        self._learn(obs)
        launched, heading = self._stand, self._heading
        if any(e.event_type == "death" for e in events):
            self._record_death(obs, run, launched, heading)
        if self.goal is not None:
            ended = evaluate(self.goal, self.level_id or obs.level_id, obs, events, self.targets)
            if ended is not None:
                step.events.append(self._end(obs, *ended))
                memory.set_goal(None)
        if obs.level_id != self.level_id:
            self.level_id = obs.level_id
            self._pending.add("no_goal")
        if any(e.event_type == "death" for e in events):
            self._pending.add("death")
            # Dave respawns at the level start, so the goal's route and waypoint are stale: end it
            # now. The goal_failed hard trigger then replans at respawn, inside any debounce window.
            if self.goal is not None:
                step.events.append(self._end(obs, "failed", "death"))
                memory.set_goal(None)
        held = held_items(obs.inventory)
        if held != self._held:
            self._held = held
            self._pending.add("inventory_changed")
        if run is not None:
            self._failures = self._failures + 1 if run.outcome in FAILED_OUTCOMES else 0
        if self.goal is not None:
            self._advance_waypoints(obs, memory)
        if self.goal is not None and self.graph is not None:
            self._follow_route(obs, memory, run)
        if self.goal is not None:
            self._follow_reach(obs, memory)
        self._note_stand(obs)
        planned = self._maybe_plan(obs, memory)
        planned.events[:0] = step.events
        return planned

    # -- lifecycle -------------------------------------------------------------------------
    def _end(self, obs: Observation, status: str, reason: str) -> Event:
        goal = self.goal
        assert goal is not None
        self.status = status
        self.previous = {"target_ref": goal.target_ref, "status": status, "reason": reason,
                         "frames_active": obs.frame - self._goal_start}
        self.goal, self._tracker = None, None
        self._waypoints = []
        self.log.finish(status, reason, obs.frame)
        self._pending.add(f"goal_{status}")
        event_type = "goal_achieved" if status == "achieved" else "goal_failed"
        return Event(event_type=event_type, episode_id=obs.episode_id, frame=obs.frame, location=_goal_tile(goal),
                     entity_refs=(goal.goal_id,), certainty="derived",
                     payload={"goal_id": goal.goal_id, "target_ref": goal.target_ref, "status": status,
                              "reason": reason})

    def _triggers(self, memory: WorkingMemory) -> set[str]:
        triggers = set(self._pending)
        if self.goal is None and not triggers & HARD_TRIGGERS:
            triggers.add("no_goal")
        progress = memory.progress()
        if progress.stuck:
            triggers.add("stuck")
        if self._failures >= self.cfg.repeated_skill_failures:
            triggers.add("repeated_failures")
        return triggers

    def _maybe_plan(self, obs: Observation, memory: WorkingMemory) -> PlanningStep:
        step = PlanningStep()
        triggers = self._triggers(memory)
        if not triggers or obs.terminal != "running" or obs.player_state in NO_PLANNING_STATES:
            return step  # latched triggers wait (e.g. until respawn)
        hard = triggers & HARD_TRIGGERS
        if not hard and self.last_call_frame is not None and \
                obs.frame - self.last_call_frame < self.cfg.min_frames_between_calls:
            return step  # debounced soft trigger; it stays latched or is re-evaluated next time
        candidates = goal_candidates(obs, self.targets, self.cfg,
                                     recover_tile(memory) if triggers & {"death", "stuck", "repeated_failures",
                                                                         "goal_failed", "goal_expired"} else None)
        if not candidates:
            return step
        if self.goal is not None and triggers & {"stuck", "repeated_failures"}:
            self._record_stuck(obs, "repeated failures" if "repeated_failures" in triggers else "stuck")
        self.platforms = self._platforms(obs, candidates)
        if self.platforms is not None:
            candidates = tuple(c.model_copy(update={"path": self.platforms.path_note(
                (c.target.col, c.target.row), DIRECTIONS.get(c.target_name, 0) if c.goal_type == "explore" else 0)})
                for c in candidates)
        route_ms = 0.0  # Python route search, timed separately from model latency (graph arms only)
        if self.graph is not None:
            t0 = time.perf_counter()
            candidates = tuple(c.model_copy(update={"route": _route_summary(self._route(obs, c),
                                                                                 self.level_graph)})
                               for c in candidates)
            route_ms = (time.perf_counter() - t0) * 1000
        request = self._request(obs, memory, tuple(sorted(triggers)), candidates)

        chosen, fallback_reason, errors, attempts, model_ms = None, None, [], 0, 0.0
        partial = None  # a valid goal whose waypoints were rejected: used once the retries run out
        if self.calls >= self.cfg.max_calls_per_episode:
            fallback_reason = "call_cap"
        else:
            feedback = None
            while attempts <= self.max_retries and self.calls < self.cfg.max_calls_per_episode:
                attempts += 1
                self.calls += 1
                text, call = self.planner.propose(request, feedback)
                model_ms += call.latency_ms or 0.0
                if call.status == "ok":
                    try:
                        choice = parse_plan(text, request.candidate_ids)
                    except PlanOutputError as exc:
                        call = call.model_copy(update={"status": "invalid_output"})
                        feedback = str(exc)
                        errors.append(feedback)
                    else:
                        pick = next(c for c in candidates if c.candidate_id == choice.goal)
                        valid, problems = self._check_waypoints(choice, obs, pick)
                        if problems:
                            call = call.model_copy(update={"status": "invalid_output"})
                            feedback = "; ".join(problems)
                            errors.append(feedback)
                            partial = (pick, choice.rationale, valid)
                        else:
                            chosen = (pick, choice.rationale, valid)
                else:
                    errors.append(f"call {call.status}")
                    feedback = None
                step.calls.append(call)
                if call.status != "ok":
                    step.events.append(Event(event_type="model_failure", episode_id=obs.episode_id,
                                             frame=obs.frame, payload={"provider": call.provider,
                                                                       "model": call.model, "purpose": "planner",
                                                                       "status": call.status}))
                if chosen is not None:
                    break
            self.last_call_frame = obs.frame
            if chosen is None and partial is not None:
                chosen = partial  # the goal stands; only its invalid waypoints are dropped
            if chosen is None:
                fallback_reason = "planner_failed" if errors else "call_cap"
        if chosen is None:
            if self.goal is not None and not hard:
                self._pending.clear()  # keep the still-valid goal; nothing to replace it with
                self._failures = 0
                return step
            avoid = self.previous["target_ref"] if self.previous and self.previous["status"] != "achieved" else None
            pick = rule_choice(candidates, avoid)
            chosen = (pick, f"deterministic fallback ({fallback_reason})", [])

        candidate, rationale, waypoints = chosen
        if self.log.open_goal is not None:
            self.log.finish("replaced", "replanned: " + ", ".join(sorted(triggers)), obs.frame)
        t1 = time.perf_counter()
        goal, route = self._activate(obs, memory, candidate, rationale, waypoints)
        if self.graph is not None:
            route_ms += (time.perf_counter() - t1) * 1000
        record = PlanningRecord(frame=obs.frame, observation_id=obs.observation_id, triggers=request.triggers,
                                request=request, chosen=candidate.candidate_id, fallback=fallback_reason is not None,
                                fallback_reason=fallback_reason, attempts=attempts, errors=errors,
                                goal_id=goal.goal_id, route=route, route_ms=round(route_ms, 3),
                                model_ms=round(model_ms, 1))
        step.record = record
        step.events.append(Event(
            event_type="goal_set", episode_id=obs.episode_id, frame=obs.frame, location=candidate.target,
            entity_refs=(goal.goal_id,), certainty="derived",
            payload={"goal_id": goal.goal_id, "target_ref": goal.target_ref, "goal_type": goal.goal_type,
                     "triggers": list(record.triggers), "fallback": record.fallback,
                     "fallback_reason": fallback_reason, "attempts": attempts, "rationale": goal.rationale,
                     "waypoint": goal.next_waypoint.model_dump() if goal.next_waypoint else None,
                     "route": route, "route_ms": record.route_ms, "model_ms": record.model_ms,
                     "waypoints": [[w.col, w.row] for w in waypoints],
                     "path": self._planned_path(obs, waypoints, candidate),
                     "deaths": list(self.log.deaths),
                     "candidates": list(request.candidate_ids)}))
        return step

    def _activate(self, obs: Observation, memory: WorkingMemory, candidate: GoalCandidate,
                  rationale: str, waypoints: list[TilePos] | None = None) -> tuple[Goal, dict[str, Any] | None]:
        self.seq += 1
        self.status = "planned"
        goal = Goal(goal_id=f"g{self.seq}", goal_type=candidate.goal_type, target_ref=candidate.candidate_id,
                    next_waypoint=candidate.target, success_predicate=candidate.success_predicate,
                    constraints=(f"target:{candidate.target.col},{candidate.target.row}", *candidate.constraints),
                    deadline_frame=obs.frame + self.cfg.goal_timeout_frames, rationale=rationale[:500],
                    source_observation_id=obs.observation_id)
        route = None
        self._waypoints = list(waypoints or [])
        if self.graph is not None:
            planned = self._route(obs, candidate)
            route = _route_summary(planned, self.level_graph)
            self._tracker = RouteTracker(planned, self.level_graph, obs.inventory)
            goal = goal.model_copy(update={"next_waypoint": self._waypoint(planned, goal)})
        waypoint = self._reach_waypoint(obs, goal)
        if waypoint is not None:
            goal = goal.model_copy(update={"next_waypoint": waypoint})
        if self._waypoints:
            goal = goal.model_copy(update={"next_waypoint": self._step_toward(obs, self._waypoints[0])
                                           or self._waypoints[0]})
        self.goal, self.status, self._goal_start = goal, "active", obs.frame
        self.log.start(goal.goal_id, goal.target_ref, [(w.col, w.row) for w in self._waypoints], obs.frame,
                       self._located(obs))
        memory.set_goal(goal)
        self._pending.clear()
        self._failures = 0
        return goal, route

    # -- estimated reachability (every arm with a reach envelope) ----------------------------
    def _learn(self, obs: Observation) -> None:
        """Remember every observed cell of this level (empty cells too): the reach estimate and
        the planner's map."""
        if obs.level_id != self._cells_level:
            self._cells, self._cells_level = {}, obs.level_id
            self.log.reset(obs.level_id)
            self._stand = self._heading = None
        region = obs.region
        for col in range(region.min.col, region.max.col + 1):
            for row in range(region.min.row, region.max.row + 1):
                self._cells[(col, row)] = "empty"
        for t in obs.tiles:
            self._cells[(t.pos.col, t.pos.row)] = t.kind

    def _reach_waypoint(self, obs: Observation, goal: Goal) -> TilePos | None:
        """The next landing spot on the estimated route to the goal's target; the target itself
        when there is no known route; None when not applicable (no envelope, a graph arm with a
        learned route, or Dave not standing)."""
        if self.reach is None or obs.player_position is None or obs.player_state not in STANDING_STATES:
            return None
        if self._tracker is not None and self._tracker.route.status == "found":
            return None  # graph arms follow their learned route
        final = _goal_tile(goal)
        reach = self._reach_map()
        assert reach is not None
        here = reach.locate(obs.player_position.x, obs.player_position.y)
        cell = None if here is None else next_waypoint(reach, here, (final.col, final.row))
        side = DIRECTIONS.get(goal.target_ref.split(":")[-1], 0) if goal.goal_type == "explore" else 0
        if cell is None and here is not None and side:
            # The target is unexplored: head for the nearest platform whose end that way is.
            cell = next_landing(reach, here, frontier(reach, side))
        return final if cell is None else TilePos(col=cell[0], row=cell[1])

    def annotate(self, obs: Observation, candidates: list[SkillCandidate],
                 skills: tuple[SkillSpec, ...]) -> tuple[list[SkillCandidate], dict[str, str]]:
        """(candidates, masked {id: reason}), the same for every arm. Each candidate gets its
        estimated end tile and, with a threat config, its first predicted contact with plasma, a
        monster or a hazard (control/threats.py) appended to the description; candidates with a
        contact are then masked unless all have one. End tiles need Dave standing on a known
        cell; contacts are also predicted while he free-falls. Unchanged without a reach
        envelope, or mid-jump."""
        if self.reach is None or obs.player_position is None:
            return candidates, {}
        reach = ReachMap(self._cells, self.reach)
        here = (reach.locate(obs.player_position.x, obs.player_position.y)
                if obs.player_state in STANDING_STATES else None)
        specs = {s.name: s for s in skills}
        contacts = assess(obs, reach, candidates, specs, self.threats) if self.threats is not None else {}
        if here is None and not contacts:
            return candidates, {}  # mid-jump, or not on a known cell: nothing to estimate
        ends = {c.candidate_id: estimate_end_at(reach, obs.player_position.x, obs.player_position.y, specs[c.skill])
                for c in candidates} if here is not None else {}
        route = self._route_notes(obs, here, candidates, specs, ends) if here is not None else {}
        out = []
        for c in candidates:
            notes = []
            if here is not None:
                end = ends[c.candidate_id]
                notes.append("estimated: no safe landing" if end is None else
                             f"estimated end tile [{end[0]}, {end[1]}]" + (" (no movement)" if end == here else ""))
            text = "; ".join([c.description, *notes])
            if c.candidate_id in route:
                text = f"{route[c.candidate_id]}; {text}"  # leads, after a danger note
            if c.candidate_id in contacts:
                contact = contacts[c.candidate_id]
                # A danger note leads, so the 200-character cap never cuts it.
                text = f"{text}; no threat predicted" if contact is None else f"{contact.note()}; {text}"
            out.append(c.model_copy(update={"description": text[:200]}))
        if self.threats is None or not self.threats.mask:
            return out, {}
        return screen(out, contacts)

    def _route_notes(self, obs: Observation, here: Cell, candidates: list[SkillCandidate],
                     specs: dict[str, SkillSpec], ends: dict[str, Cell | None]) -> dict[str, str]:
        """``route:`` notes on the candidates that carry out the next move of the estimated path
        to the goal's waypoint, so the tactical model can follow the plan: the skills that land
        where that move lands; else the walks that bring Dave to its take-off (a jump that only
        works from a platform's end needs the walk there first); else, when only walking is
        left, the walks toward the waypoint. Same for every arm."""
        reach = self._reach_map()
        wp = self.goal.next_waypoint if self.goal is not None else None
        if reach is None or wp is None or obs.player_position is None:
            return {}
        steps = reach.path(here, reach.targets_for((wp.col, wp.row)))
        if not steps:
            return {}
        landing, kind = steps[-1]
        for cell, k in steps:
            if k != "walk":
                landing, kind = cell, k
                break
        x = obs.player_position.x
        target = f"[{landing[0]}, {landing[1]}]"
        makes = [c.candidate_id for c in candidates if kind != "walk" and ends.get(c.candidate_id) == landing]
        if makes:
            return {cid: f"route: makes the next move ({kind} to {target})" for cid in makes}
        # The walks that keep Dave on this platform: toward a walk-only target, or to a spot from
        # which one of the catalog's jumps lands where the next move does (simulated, since the
        # working take-off can be anywhere on the platform, e.g. its far end).
        jumps = [spec for spec in specs.values() if "jump" in spec.phases[0].buttons]
        out = {}
        for c in candidates:
            spec = specs[c.skill]
            if "jump" in spec.phases[0].buttons or not any(b in DIRECTIONS for b in spec.buttons):
                continue
            end_x, end_y = trace_skill(reach, x, obs.player_position.y, spec)[-1]
            ground = reach._ground(end_x, end_y)
            if ground is None or ground[1] != here[1] or ends.get(c.candidate_id) is None:
                continue  # walked off the platform
            if kind == "walk":
                if abs(end_x - landing[0] * TILE) < abs(x - landing[0] * TILE):
                    out[c.candidate_id] = f"route: toward {target}"
            elif any(estimate_end_at(reach, end_x, end_y, j) == landing for j in jumps):
                out[c.candidate_id] = f"route: walks to the take-off for the {kind} to {target}"
        return out

    def _follow_reach(self, obs: Observation, memory: WorkingMemory) -> None:
        assert self.goal is not None
        waypoint = self._reach_waypoint(obs, self.goal)
        if waypoint is not None:
            self._set_waypoint(waypoint, memory, obs)

    def _step_toward(self, obs: Observation, target: TilePos) -> TilePos | None:
        """The next landing on the estimated path from Dave to ``target`` (the target itself when
        only walking is left, or when no path is known); None when Dave is not standing on a
        known cell, or without a reach envelope (callers keep what they have)."""
        here = self._located(obs)
        reach = self._reach_map()
        if here is None or reach is None:
            return None
        cell = next_waypoint(reach, here, (target.col, target.row))
        return target if cell is None else TilePos(col=cell[0], row=cell[1])

    def _set_waypoint(self, waypoint: TilePos, memory: WorkingMemory, obs: Observation) -> None:
        """Point the goal at ``waypoint``; while planner waypoints are ahead, at the next landing
        toward the first of them instead (a far waypoint would send the tactical model straight
        at it, off ledges and into walls)."""
        assert self.goal is not None
        if self._waypoints:
            step = self._step_toward(obs, self._waypoints[0])
            if step is None and self.reach is not None and self.goal.next_waypoint is not None:
                return  # airborne: keep the current step until he stands again
            waypoint = step or self._waypoints[0]
        if waypoint != self.goal.next_waypoint:
            self.goal = self.goal.model_copy(update={"next_waypoint": waypoint})
            memory.set_goal(self.goal, restart_clock=False)

    # -- planner waypoints -------------------------------------------------------------------
    def _standable(self, col: int, row: int) -> bool:
        kind = self._cells.get((col, row))
        return kind is not None and kind not in BLOCKING_KINDS and self._cells.get((col, row + 1)) == "solid"

    def _check_waypoints(self, choice: PlanChoice, obs: Observation,
                         goal: GoalCandidate) -> tuple[list[TilePos], list[str]]:
        """(valid waypoints, problems). Tiles and platform ids are resolved to cells; each must be
        an explored standing cell and, with a reach envelope, reachable from the one before (from
        Dave for the first) with the game's physics; then the
        goal must still be reachable from the last one. The valid prefix is kept."""
        target = (goal.target.col, goal.target.row)
        reach = self._reach_map()
        prev = self._located(obs)
        valid: list[TilePos] = []
        for w in choice.waypoints:
            given: Any = w if isinstance(w, str) else list(w)
            if isinstance(w, str):
                cell = self.platforms.resolve(w, target) if self.platforms is not None else None
                if cell is None:
                    return valid, [f"waypoint {w!r} is not a platform id from `platforms`"]
            else:
                cell = (w[0], w[1])
            if not self._standable(*cell):
                return valid, [(f"waypoint {given} is not an empty explored cell directly above a '#' on the map; "
                                "give standing tiles [col, row] or platform ids")]
            if reach is not None and prev is not None and reach.path(prev, {cell}) is None:
                return valid, [self._unreachable(reach, prev, cell, f"waypoint {given}")]
            valid.append(TilePos(col=cell[0], row=cell[1]))
            prev = cell
        if reach is not None and valid and prev is not None and goal.goal_type != "explore":
            here = self._located(obs)
            ends = reach.targets_for(target)
            if reach.path(prev, ends) is None and here is not None and reach.path(here, ends) is not None:
                return valid[:-1], [self._unreachable(reach, prev, target, f"the goal {goal.candidate_id}",
                                                      to_goal=True)]
        return valid, []

    def _unreachable(self, reach: ReachMap, frm: Cell, to: Cell, what: str, to_goal: bool = False) -> str:
        """Feedback for a leg the physics estimate cannot make: where Dave can go from there
        instead."""
        ends = reach.targets_for(to) if to_goal else {to}
        text = f"{what} at [{to[0]}, {to[1]}] is not reachable from [{frm[0]}, {frm[1]}] (a wall, a gap or too high)"
        if self.platforms is not None:
            p = self.platforms.of(frm)
            if p is not None and p.exits:
                text += f"; from {p.pid} Dave can reach: " + ", ".join(
                    f"{pid} ({e['by']})" for pid, e in sorted(p.exits.items(), key=lambda kv: kv[1]["cost"])[:6])
            here = self.platforms.here
            if to_goal and here is not None:
                route = reach.path(here, ends)
                if route is not None:
                    text += f"; the goal is reachable this way: {self.platforms.chain(route, here)}"
        return text

    # -- attempts and failed moves (control/attempts.py) -------------------------------------
    def _reach_map(self) -> ReachMap | None:
        """The reach estimate over this level's cells; moves that failed here cost more."""
        if self.reach is None:
            return None
        return ReachMap(self._cells, self.reach, self.log.failures())

    def _located(self, obs: Observation) -> Cell | None:
        """Dave's standing cell on the estimate's map; None without a reach envelope or airborne."""
        if self.reach is None or obs.player_position is None or obs.player_state not in STANDING_STATES:
            return None
        return ReachMap(self._cells, self.reach).locate(obs.player_position.x, obs.player_position.y)

    def _note_stand(self, obs: Observation) -> None:
        here = self._located(obs)
        if here is None:
            return
        if self._stand is not None and self._stand != here:
            self.log.succeed(self._stand, here)  # a move that failed before has now worked
        self._stand = here
        wp = self.goal.next_waypoint if self.goal is not None else None
        self._heading = None if wp is None else (wp.col, wp.row)
        self.log.progress(here)

    def _first_move(self, frm: Cell, toward: Cell) -> tuple[Cell, Cell] | None:
        """(launch cell, landing cell) of the first jump or fall on the estimated path."""
        reach = self._reach_map()
        if reach is None:
            return None
        prev = frm
        for cell, kind in reach.path(frm, reach.targets_for(toward)) or []:
            if kind != "walk":
                return prev, cell
            prev = cell
        return None

    def _record_stuck(self, obs: Observation, how: str) -> None:
        here = self._located(obs) or self._stand
        wp = self.goal.next_waypoint if self.goal is not None else None
        if here is None or wp is None:
            return
        link = self._first_move(here, (wp.col, wp.row))
        if link is not None:  # only a real estimated move, never a line to a far target
            self.log.fail(*link, how)

    def _record_death(self, obs: Observation, run: ExecutionResult | None, launched: Cell | None,
                      heading: Cell | None) -> None:
        hit = obs
        if run is not None:
            hit = next((s.observation for s in run.steps if s.observation.player_state == "burning"), run.observation)
        cause, tile = contact_cause(hit)
        self.log.death(cause, tile)
        # Fire or water on the way is the move's fault; a shot or a monster is not.
        if cause not in ("plasma", "monster") and launched is not None and heading is not None:
            link = self._first_move(launched, heading)
            if link is not None:
                self.log.fail(*link, f"death: {cause}")

    def _planned_path(self, obs: Observation, waypoints: list[TilePos],
                      goal: GoalCandidate) -> list[list[Any]]:
        """The estimated moves Dave -> waypoints -> goal as [col, row, kind] landing cells (kind
        walk, fall or jump; "unknown" for a leg the estimate cannot make), for the viewer."""
        reach = self._reach_map()
        prev = self._located(obs)
        if reach is None or prev is None:
            return []
        out: list[list[Any]] = [[prev[0], prev[1], "start"]]
        legs = [((w.col, w.row), {(w.col, w.row)}) for w in waypoints]
        target = (goal.target.col, goal.target.row)
        legs.append((target, reach.targets_for(target) or {target}))
        for end, ends in legs:
            steps = reach.path(prev, ends)
            if steps is None:
                out.append([end[0], end[1], "unknown"])
                prev = end
                continue
            out.extend([c[0], c[1], kind] for c, kind in steps)
            if steps:
                prev = steps[-1][0]
        return out

    def _platforms(self, obs: Observation, candidates: tuple[GoalCandidate, ...]) -> Platforms | None:
        reach = self._reach_map()
        if reach is None:
            return None
        out = Platforms(reach, self._located(obs))
        out.note_items({c.candidate_id: (c.target.col, c.target.row) for c in candidates
                        if c.goal_type != "explore"})
        out.note_danger(self.log.deaths, "died here this episode")
        if self.graph is not None and self.level_id is not None:
            past = [i for _, d in self.level_graph.g.nodes(data=True) for i in d.get("incidents", [])]
            out.note_danger(past, "died here in past runs")
        out.note_failed(self.log.failed_links())
        return out

    def _advance_waypoints(self, obs: Observation, memory: WorkingMemory) -> None:
        """Drop the planner waypoints Dave has reached (standing on the row, within 1 column), then
        point the goal at the next one, or back at its target (the route or reach estimate
        refines that right after on arms that have one)."""
        assert self.goal is not None
        if not self._waypoints or obs.player_position is None or obs.player_state not in STANDING_STATES:
            return
        here = player_tile(obs.player_position)
        for i, w in enumerate(self._waypoints):
            if here.row == w.row and abs(here.col - w.col) <= 1:
                del self._waypoints[:i + 1]  # reached this one: those before it are behind too
                self.log.progress((here.col, here.row), reached=i + 1)
                self._set_waypoint(_goal_tile(self.goal), memory, obs)
                return

    # -- learned routes (graph-enabled arms only) -------------------------------------------
    def _target_node(self, obs: Observation, candidate: GoalCandidate, start: str | None) -> str | None:
        graph = self.level_graph
        if candidate.goal_type != "explore":
            return graph.node_at(obs.level_id, candidate.target.row, candidate.target.col)
        # Explore: the cheapest reachable segment with an unexplored side in that direction.
        side = "open_right" if candidate.target_name == "right" else "open_left"
        options = sorted(n for n, d in graph.g.nodes(data=True) if d["level_id"] == obs.level_id and d[side])
        if start is None or not options:
            return options[0] if options else None
        costed = []
        for node in options:
            route = find_route(graph, start, node, obs.inventory, self.graph_cfg)  # type: ignore[arg-type]
            if route.status in ("found", "at_target"):
                costed.append((route.cost or 0.0, node))
        return min(costed)[1] if costed else options[0]

    def _route(self, obs: Observation, candidate: GoalCandidate) -> Route:
        graph = self.level_graph
        assert self.graph_cfg is not None
        start = graph.locate(obs)
        target = self._target_node(obs, candidate, start)
        if start is None:
            return Route("unreachable", "", target or "", reason="unknown_start")
        if target is None:
            return Route("unreachable", start, "", reason="target_not_on_known_segment",
                         frontier=find_route(graph, start, "", obs.inventory, self.graph_cfg).frontier)
        return find_route(graph, start, target, obs.inventory, self.graph_cfg)

    def _waypoint(self, route: Route, goal: Goal) -> TilePos:
        """The next route node's cell nearest the goal's target, or the target tile itself."""
        final = _goal_tile(goal)
        step = self._tracker.next_step if self._tracker else None
        if route.status != "found" or step is None or self.graph is None:
            return final
        graph = self.level_graph
        data = graph.g.nodes[graph.resolve(step.target)]
        return TilePos(col=min(max(final.col, data["col_min"]), data["col_max"]), row=data["row"])

    def _follow_route(self, obs: Observation, memory: WorkingMemory, run: ExecutionResult | None) -> None:
        """Route bookkeeping in Python; only a route lost after being found asks the planner."""
        assert self.graph is not None and self.goal is not None and self._tracker is not None
        graph = self.level_graph
        node = graph.locate(obs)
        failed = run is not None and run.outcome in FAILED_OUTCOMES
        reason = self._tracker.update(graph, node, obs.inventory, step_failed=failed)
        if reason is None:
            waypoint = self._waypoint(self._tracker.route, self.goal)
        elif reason == "target_reached":
            waypoint = _goal_tile(self.goal)
        elif node is None and reason != "inventory_changed" and not reason.startswith("no_route"):
            return  # airborne: re-route once Dave is standing on a segment again
        else:
            had_route = self._tracker.route.status == "found"
            candidate = GoalCandidate(candidate_id=self.goal.target_ref, goal_type=self.goal.goal_type,
                                      target_kind="", target_name=self.goal.target_ref.split(":")[1],
                                      target=_goal_tile(self.goal), description="",
                                      success_predicate=self.goal.success_predicate)
            route = self._route(obs, candidate)
            self._tracker = RouteTracker(route, graph, obs.inventory)
            if had_route and route.status == "unreachable":
                self._pending.add("route_invalidated")
            waypoint = self._waypoint(route, self.goal)
        self._set_waypoint(waypoint, memory, obs)

    # -- request -----------------------------------------------------------------------------
    def _request(self, obs: Observation, memory: WorkingMemory, triggers: tuple[str, ...],
                 candidates: tuple[GoalCandidate, ...]) -> PlanningRequest:
        here = player_tile(obs.player_position) if obs.player_position else None
        nearby: list[dict[str, Any]] = []
        if here is not None:
            for t in obs.tiles:
                if t.kind == "hazard" and abs(t.pos.col - here.col) <= NEARBY_TILES \
                        and abs(t.pos.row - here.row) <= NEARBY_TILES:
                    nearby.append({"type": t.name, "kind": "hazard_tile", "tile": [t.pos.col, t.pos.row]})
            for e in obs.entities:
                if e.visible:
                    tile = player_tile(e.position)
                    nearby.append({"type": e.entity_type, "kind": "entity", "tile": [tile.col, tile.row]})
        recent = tuple(
            {"skill": e.skill, "outcome": e.outcome, "reason": e.reason, "events": list(e.events),
             "end_tile": [e.end_tile.col, e.end_tile.row] if e.end_tile else None,
             "moved_px": e.moved_px}
            for e in memory.history[-self.cfg.recent_events:]
        )
        return PlanningRequest(
            episode_id=obs.episode_id, level_id=obs.level_id, frame=obs.frame, observation_id=obs.observation_id,
            triggers=triggers,
            player={"tile": [here.col, here.row] if here else None, "state": obs.player_state,
                    "grounded": obs.grounded, "facing": obs.facing},
            lives=obs.lives, inventory=obs.inventory, score=obs.score,
            view_cols=(obs.region.min.col, obs.region.max.col), nearby=tuple(nearby[:MAX_NEARBY]),
            recent=recent, previous_goal=self.previous,
            current_goal=self.goal.target_ref if self.goal else None,
            candidates=candidates, graph_routes=self.graph is not None,
            map=render(self._cells, obs, tuple((w.col, w.row) for w in self._waypoints)),
            waypoints=tuple((w.col, w.row) for w in self._waypoints),
            platforms=tuple(self.platforms.views()) if self.platforms is not None else (),
            attempts=tuple(self.log.views()), failed_links=tuple(self.log.failed_links()),
        )
