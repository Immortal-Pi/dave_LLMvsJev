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
from dave_agent.control.credit import GoalCredit, GoalSteps
from dave_agent.control.level_map import render
from dave_agent.control.platforms import Platforms
from dave_agent.control.reach import DIRECTIONS, TILE, Cell, ReachMap, estimate_end_at, flight, frontier, grabs, \
    next_landing, next_waypoint, trace_flying, trace_in_jump, trace_skill
from dave_agent.control.move_score import MoveScore, position_values, score_moves
from dave_agent.control.skills import ExecutionResult
from dave_agent.control.threats import Contact, assess_timing, contact_cause, firing_cells, safe_window, screen, \
    shot_hits, threat_key
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
from dave_agent.schemas import DESCRIPTION_MAX, Event, Goal, ModelCallRecord, Observation, SkillCandidate, TilePos

TARGET_KINDS = frozenset({"required_item", "item", "collectible", "exit"})
# Hard triggers always plan (within the call cap); soft triggers are debounced.
HARD_TRIGGERS = frozenset({"no_goal", "goal_achieved", "goal_failed", "goal_expired"})
NO_PLANNING_STATES = frozenset({"burning", "dead"})  # inputs are ignored; plan after respawn
NEARBY_TILES = 3  # hazards/monsters within this many tiles are summarized for the planner
MAX_NEARBY = 10
# A monster that a shooting option is predicted to hit guards the way when a move's predicted
# contact is with it (or its shots), or when it is within this many tiles of Dave or of the next
# waypoint: the shot then gets a ``route:`` note. Live runs on levels 5 and 6 offered ``shoot``
# in 82-122 decisions per level and the model chose it 0-4 times; the route note won every time.
KILL_NOTE_TILES = 3
TAKEOFF_WALKS = 3  # how many walks ahead the route notes look for a jump's take-off
FLY_LOOKAHEAD = 3  # flying: aim at the cell this many cells along the flight path
# Self-sacrifice: after this many frames with no route note on any option (no safe move on the
# way, no wait for one), and with at least 2 lives, a move into a monster's body is offered: the
# monster burns and stays dead (game.c: the level start after a death resets only Dave), Dave
# loses a life and restarts at the level start with what he has collected.
SACRIFICE_FRAMES = 1200
# Jetpack fuel kept for the goals that may need a flight to finish the level (the trophy, the
# door): other goals fly only on what is above it. Level 4: the scripted run spent fuel on loot
# and short hops; the person used 681 of 900 for the trophy at (5,2) and the door.
FUEL_RESERVE = 400
REQUIRED_GOALS = ("collect:trophy", "reach:door")
FULL_FUEL = 900  # one jetpack pickup (docs/state_mapping.md): what a flight-only goal needs is assumed
# The rule priority chooses unless one of these triggers fired (planning.llm_calls: escalate).
ESCALATE_TRIGGERS = frozenset({"stuck", "repeated_failures", "death"})
SACRIFICE_LIVES = 2


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
    planner: str = "llm"  # who chose: llm, rule (escalate mode, no call) or fallback


@dataclass
class PlanningStep:
    events: list[Event] = field(default_factory=list)
    calls: list[ModelCallRecord] = field(default_factory=list)
    record: PlanningRecord | None = None
    # Goals that ended (achieved, failed, expired or replaced) with the steps taken for them, for
    # the learned graph's credit on graph-enabled arms (control/credit.py).
    credit: list[GoalSteps] = field(default_factory=list)


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
        self._fuel = 0  # jetpack fuel (ticks): routes may fly while there is some
        self._stalled_since: int | None = None  # first frame with no route note on any option
        # Forecast misses: what the threat screen predicted for the options of the last decision,
        # and (tile, skill) -> causes of deaths that came although the chosen move was predicted safe.
        self._predicted: dict[str, Contact | None] = {}
        self._predicted_at: Cell | None = None
        self._misses: dict[tuple[Cell, str], list[str]] = {}
        self._waypoints: list[TilePos] = []  # the planner's waypoints still ahead, in order
        self.log = AttemptLog()  # what was tried on this level (control/attempts.py)
        self._stand: Cell | None = None  # Dave's last standing cell, and the waypoint he was heading for
        self._heading: Cell | None = None
        self.platforms: Platforms | None = None  # as last shown to the planner
        self._risky: set[Cell] = set()  # cells in a monster's predicted line of fire, now
        self.credit = GoalCredit()  # what each skill did toward each target this episode
        self._credit_done: list[GoalSteps] = []
        self.scores: dict[str, MoveScore] = {}  # the last decision's live move scores (graph arms)
        self._hits: frozenset[str] = frozenset()  # options whose shot is predicted to hit a monster

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
        planned = self._maybe_plan(obs, memory)
        planned.credit = self._drain_credit()
        return planned

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
            miss = self._forecast_miss(obs, run)
            if miss is not None:
                step.events.append(miss)
        if self.goal is not None:
            ended = evaluate(self.goal, self.level_id or obs.level_id, obs, events, self.targets)
            self._credit_step(obs, memory, events, achieved=ended is not None and ended[0] == "achieved")
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
        planned.credit = self._drain_credit()
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
        self._finish_credit(status == "achieved")
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
            # Loot is score only: with no known path it is noise for both planners.
            candidates = tuple(c for c in candidates
                               if not (c.target_kind == "collectible" and _no_path(c))) or candidates
            candidates = self._requires(obs, candidates)
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
        avoid = self.previous["target_ref"] if self.previous and self.previous["status"] != "achieved" else None
        source = "llm"
        if self.cfg.llm_calls == "escalate" and not self._escalate(triggers, rule_choice(candidates, avoid)):
            pick = rule_choice(candidates, avoid)
            chosen, source = (pick, f"rule priority ({pick.goal_type})", []), "rule"
        elif self.calls >= self.cfg.max_calls_per_episode:
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
            pick = rule_choice(candidates, avoid)
            chosen, source = (pick, f"deterministic fallback ({fallback_reason})", []), "fallback"

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
                                model_ms=round(model_ms, 1), planner=source)
        step.record = record
        step.events.append(Event(
            event_type="goal_set", episode_id=obs.episode_id, frame=obs.frame, location=candidate.target,
            entity_refs=(goal.goal_id,), certainty="derived",
            payload={"goal_id": goal.goal_id, "target_ref": goal.target_ref, "goal_type": goal.goal_type,
                     "triggers": list(record.triggers), "fallback": record.fallback,
                     "fallback_reason": fallback_reason, "attempts": attempts, "planner": source,
                     "rationale": goal.rationale,
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
        self._finish_credit(False)  # a goal replaced before it ended
        self.goal, self.status, self._goal_start = goal, "active", obs.frame
        self.credit.start(goal.target_ref, obs.level_id, self._located(obs))
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
            self._misses, self._predicted, self._predicted_at = {}, {}, None
            self.log.reset(obs.level_id)
            self._stand = self._heading = None
        region = obs.region
        for col in range(region.min.col, region.max.col + 1):
            for row in range(region.min.row, region.max.row + 1):
                self._cells[(col, row)] = "empty"
        for t in obs.tiles:
            self._cells[(t.pos.col, t.pos.row)] = t.kind
        self._risky = firing_cells(obs) if self.threats is not None else set()
        self._fuel = (obs.inventory or {}).get("jetpack_fuel", 0)

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
        contact are then masked unless all have one. Timing notes say when to go (a wait is
        safe when a move is safe after it), and a path past the screen edge is noted, not
        masked. End tiles need Dave standing on a known cell; contacts are also predicted while
        he free-falls. Mid-jump, each option gets its landing tile and the best landing toward the
        goal a route note (``_air_notes``). Unchanged without a reach envelope."""
        self.scores = {}
        self._hits = frozenset()
        if self.reach is None or obs.player_position is None:
            return candidates, {}
        self._fuel = (obs.inventory or {}).get("jetpack_fuel", 0)
        reach = ReachMap(self._cells, self.reach, fuel=self._flight_fuel())
        here = (reach.locate(obs.player_position.x, obs.player_position.y)
                if obs.player_state in STANDING_STATES else None)
        specs = {s.name: s for s in skills}
        contacts, timing = (assess_timing(obs, reach, candidates, specs, self.threats)
                            if self.threats is not None else ({}, {}))
        self._predicted, self._predicted_at = dict(contacts), here
        # Skills the threat screen will remove are never marked: the next safe way is.
        safe = [c for c in candidates if not _blocks(contacts.get(c.candidate_id))]
        grab = self._grab_notes(obs, reach, safe, specs)
        if here is None and not contacts and not grab:
            return candidates, {}  # mid-jump, or not on a known cell: nothing to estimate
        ends = {c.candidate_id: estimate_end_at(reach, obs.player_position.x, obs.player_position.y, specs[c.skill])
                for c in candidates} if here is not None else {}
        route = self._route_notes(obs, here, safe, specs, ends) if here is not None else {}
        air = self._air_notes(obs, reach, safe, specs)
        route = {**route, **self._fly_notes(obs, reach, safe, specs)}
        route = {**self._reveal_notes(obs, reach, here, safe, contacts, specs), **route,
                 **{cid: note for cid, (note, best) in air.items() if best}, **grab}
        kill = self._kill_notes(obs, reach, safe, contacts, specs) if self.threats is not None else {}
        # A kill clears the way: no wait for a gap between the shots is needed.
        if here is not None and not route and not kill and self.threats is not None:
            route, timing = self._wait_notes(obs, reach, here, candidates, safe, contacts, specs, ends, timing)
        route = {**route, **kill}
        sacrifice = self._sacrifice_notes(obs, here, route, candidates, contacts) if self.threats is not None else {}
        route = {**route, **sacrifice}
        if here is not None:
            self.scores = self._move_scores(obs, candidates, ends)
        out = []
        for c in candidates:
            notes = []
            if here is not None:
                if self._fuel and specs[c.skill].buttons == {"jetpack"} and c.candidate_id not in route:
                    notes.append(f"uses fuel ({self._fuel} left): not needed for the planned route")
                if c.candidate_id in self.scores:
                    notes.append(self.scores[c.candidate_id].note())
                end = ends[c.candidate_id]
                edge = contacts.get(c.candidate_id) is not None and not _blocks(contacts[c.candidate_id])
                notes.append("estimated end: past the screen edge, unseen" if edge else
                             "estimated: no safe landing" if end is None else
                             f"estimated end tile [{end[0]}, {end[1]}]" + (" (no movement)" if end == here else ""))
                credit = None if self.goal is None else self.credit.note(self.goal.target_ref, here, c.skill)
                if credit:
                    notes.append(credit)
                missed = self._misses.get((here, c.skill))
                if missed:
                    notes.append(f"forecast missed here before: hit {len(missed)}x ({', '.join(sorted(set(missed)))}) "
                                 f"though predicted safe")
            elif c.candidate_id in air and not air[c.candidate_id][1]:
                notes.append(air[c.candidate_id][0])  # the best landing leads as a route note
            text = "; ".join([c.description, *notes])
            contact = contacts.get(c.candidate_id)
            if contact is not None and not contact.blocks:
                text = f"{contact.note()}; {text}"  # past the screen edge: after the route note
            if c.candidate_id in timing:
                text = f"{timing[c.candidate_id]}; {text}"
            if c.candidate_id in route:
                text = f"{route[c.candidate_id]}; {text}"  # leads, after a danger note
            if c.candidate_id in contacts and c.candidate_id not in sacrifice:
                # A danger note leads, so the length cap (DESCRIPTION_MAX) never cuts it.
                if contact is None:
                    text = f"{text}; no threat predicted"
                elif contact.blocks:
                    text = f"{contact.note()}; {text}"
            out.append(c.model_copy(update={"description": text[:DESCRIPTION_MAX]}))
        if self.threats is None or not self.threats.mask:
            return out, {}
        return screen(out, {cid: None if cid in sacrifice else c for cid, c in contacts.items()})

    def _kill_notes(self, obs: Observation, reach: ReachMap, candidates: list[SkillCandidate],
                    contacts: dict[str, Contact | None], specs: dict[str, SkillSpec]) -> dict[str, str]:
        """``route:`` notes on the shooting options predicted to hit a monster that guards the way
        (``KILL_NOTE_TILES``): a monster hit by a bullet burns and stays dead for the rest of the
        level, its plasma stops (monster.c, game.c), so the moves past it become safe. Sets
        ``_hits`` (the options predicted to hit any monster) for the move scores."""
        hits = {cid: hit for cid, (_, hit) in shot_hits(obs, reach.cells, candidates, specs).items() if hit}
        self._hits = frozenset(hits)
        if not hits:
            return {}
        guards: set[str] = set()
        for contact in contacts.values():
            if contact is not None and contact.blocks and contact.kind != "hazard":
                what = threat_key(contact.what)  # a monster's shot, flying or due: plasma<n>
                guards.add("monster" + what[len("plasma"):] if what.startswith("plasma") else what)
        assert obs.player_position is not None
        near = [(obs.player_position.x + 8) // TILE, (obs.player_position.y + 8) // TILE]
        spots = [tuple(near)] + ([(wp.col, wp.row)] if self.goal is not None and (wp := self.goal.next_waypoint)
                                 else [])
        kinds = {}
        for e in obs.entities:
            if not e.visible or e.entity_type in ("plasma", "bullet"):
                continue
            kinds[e.entity_id] = e.entity_type
            col, row = (e.position.x + 12) // TILE, (e.position.y + 10) // TILE
            if any(max(abs(col - c), abs(row - r)) <= KILL_NOTE_TILES for c, r in spots):
                guards.add(e.entity_id)
        return {cid: f"route: shoot — hits the {kinds.get(m, 'monster')} in {tick} ticks; it stays dead and the "
                     f"way on is clear" for cid, (m, tick) in hits.items() if m in guards}

    def _sacrifice_notes(self, obs: Observation, here: Cell | None, route: dict[str, str],
                         candidates: list[SkillCandidate], contacts: dict[str, Contact | None]) -> dict[str, str]:
        """The last resort when Dave has stood without a way on for ``SACRIFICE_FRAMES``: the moves
        whose first contact is a monster's body (its plasma kills only Dave), unmasked, with a
        route note saying what it costs. Only with ``SACRIFICE_LIVES`` or more lives."""
        if here is None:
            return {}
        if route:
            self._stalled_since = None
            return {}
        if self._stalled_since is None or obs.frame < self._stalled_since:
            self._stalled_since = obs.frame
        if obs.frame - self._stalled_since < SACRIFICE_FRAMES or (obs.lives or 0) < SACRIFICE_LIVES:
            return {}
        bodies = {c.candidate_id: contacts[c.candidate_id] for c in candidates
                  if (hit := contacts.get(c.candidate_id)) is not None and hit.blocks
                  and hit.what.startswith("monster") and hit.kind not in ("plasma", "hazard")}
        if not bodies:
            return {}
        first = min(h.tick for h in bodies.values())
        left = (obs.lives or 0) - 1
        return {cid: f"route: last resort (stuck {obs.frame - self._stalled_since} frames) — collides with the "
                     f"{h.kind} in {h.tick} ticks: it is destroyed for good; Dave loses a life ({left} left) and "
                     f"restarts at the level start, keeping what he collected"
                for cid, h in bodies.items() if h.tick == first}

    def _wait_notes(self, obs: Observation, reach: ReachMap, here: Cell, candidates: list[SkillCandidate],
                    safe: list[SkillCandidate], contacts: dict[str, Contact | None], specs: dict[str, SkillSpec],
                    ends: dict[str, Cell | None], timing: dict[str, str]) -> tuple[dict[str, str], dict[str, str]]:
        """(route notes, timing notes) when the move the route wants is blocked now: the first
        moment it is safe (``threats.safe_window``), as a ``route: wait`` note on the longest safe
        wait that does not pass it, and a ``timing: safe from`` note on the move itself."""
        blocked = [c for c in candidates if _blocks(contacts.get(c.candidate_id))
                   and specs[c.skill].buttons & {"left", "right", "jump"} and "fire" not in specs[c.skill].buttons]
        wanted = self._route_notes(obs, here, blocked, specs, ends)
        if not wanted:
            return {}, timing
        waits = [c for c in safe if not specs[c.skill].buttons]
        timing = dict(timing)
        for c in blocked:
            if c.candidate_id not in wanted:
                continue
            assert self.threats is not None
            window = safe_window(obs, reach, specs[c.skill], self.threats)
            if window is None:
                continue
            delay, length = window
            timing[c.candidate_id] = f"timing: safe from {delay} ticks (for {length})"
            fit = [w for w in waits if specs[w.skill].max_frames <= delay]
            if fit:
                wait = max(fit, key=lambda w: specs[w.skill].max_frames)
                return {wait.candidate_id: f"route: wait — {c.skill} is safe in {delay} ticks (for {length} ticks)"}, timing
        return {}, timing

    def _fly_notes(self, obs: Observation, reach: ReachMap, candidates: list[SkillCandidate],
                   specs: dict[str, SkillSpec]) -> dict[str, str]:
        """With the jetpack on: ``route:`` notes on the moves that get Dave furthest along the
        shortest flight to the goal's waypoint (over known open cells, ``ReachMap.fly_path``),
        judged by where each one really ends (``trace_flying``: walls and ceilings stop him, so a
        2 px nudge that lines him up with a gap can be the move that gets him on); at a standable
        end, turning the jetpack off when that lands him there."""
        pos, goal = obs.player_position, self.goal
        if pos is None or goal is None or obs.player_state != "jetpacking" or not self._fuel:
            return {}
        wp = goal.next_waypoint or _goal_tile(goal)
        start = ((pos.x + TILE // 2) // TILE, (pos.y + TILE // 2) // TILE)
        dist, _ = reach.fly_cells(start)
        ends = [t for t in reach.targets_for((wp.col, wp.row)) if t in dist]
        if not ends:
            return {}
        end = min(ends, key=lambda c: (dist[c], c))
        path = reach.fly_path(start, end)
        aim = path[min(len(path) - 1, FLY_LOOKAHEAD)]
        target = f"[{end[0]}, {end[1]}]"

        def gap(px: int, py: int) -> int:
            return abs(px - aim[0] * TILE) + abs(py - aim[1] * TILE)

        now = gap(pos.x, pos.y)
        scored: dict[str, int] = {}
        out: dict[str, str] = {}
        for c in candidates:
            spec = specs[c.skill]
            if not spec.buttons & {"left", "right", "jump", "down", "jetpack"}:
                continue  # hovering gets him nowhere
            flown = trace_flying(reach, pos.x, pos.y, spec, self._fuel)
            if reach.burns(flown):
                continue
            if "jetpack" in spec.buttons:
                if reach.standable(end) and reach.locate(*flown[-1]) == end:
                    out[c.candidate_id] = f"route: turns the jetpack off and lands on {target}"
                continue
            if gap(*flown[-1]) < now:
                scored[c.candidate_id] = gap(*flown[-1])
        if scored:
            best = min(scored.values())
            out.update({cid: f"route: flies toward {target}" for cid, s in scored.items() if s == best})
        return out

    def _air_notes(self, obs: Observation, reach: ReachMap, candidates: list[SkillCandidate],
                   specs: dict[str, SkillSpec]) -> dict[str, tuple[str, bool]]:
        """Mid-jump (a jump stopped by a threat coming into view): {id: (landing note, best)} for
        the candidates that land somewhere safe. ``best`` marks those whose landing is the
        cheapest way on toward the goal, so the tactical model can keep the plan after shooting or
        steering. Empty when Dave is not mid-jump."""
        pos = obs.player_position
        if pos is None or obs.player_state != "jumping" or obs.jump_tick is None:
            return {}
        opened = reach.opened()
        targets = self._goal_targets(reach, self.goal) if self.goal is not None else set()
        lands: dict[str, Cell] = {}
        for c in candidates:
            path = trace_in_jump(opened, pos.x, pos.y, obs.jump_tick, specs[c.skill])
            cell = reach.locate(*path[-1])
            if cell is not None and not reach.burns(path):
                lands[c.candidate_id] = cell
        costs = {cid: reach.cost(cell, targets) for cid, cell in lands.items()} if targets else {}
        known = [v for v in costs.values() if v is not None]
        best = min(known) if known else None
        return {cid: (f"route: lands on [{cell[0]}, {cell[1]}], the best landing toward the goal"
                      if best is not None and costs.get(cid) == best else
                      f"estimated landing tile [{cell[0]}, {cell[1]}]", best is not None and costs.get(cid) == best)
                for cid, cell in lands.items()}

    def _reveal_notes(self, obs: Observation, reach: ReachMap, here: Cell | None, candidates: list[SkillCandidate],
                      contacts: dict[str, Contact | None], specs: dict[str, SkillSpec]) -> dict[str, str]:
        """On an explore goal with no known way to the explored map's edge that way, the moves
        whose path passes the screen edge that way: the screen scrolls, and only that shows what
        is beyond (level 4: Dave can stand no further than x 507 on the ledge at (31,3), and the
        screen scrolls only past x 520)."""
        goal = self.goal
        if goal is None or goal.goal_type != "explore" or here is None or obs.player_position is None:
            return {}
        side = DIRECTIONS.get(goal.target_ref.split(":")[-1], 0)
        if not side or reach.path(here, frontier(reach, side)):
            return {}
        x0 = obs.player_position.x
        out = {}
        for c in candidates:
            contact = contacts.get(c.candidate_id)
            if contact is None or contact.blocks:
                continue
            path = trace_skill(reach.opened(), x0, obs.player_position.y, specs[c.skill], 0, obs.jump_cooldown)
            x = path[min(contact.tick, len(path)) - 1][0]
            if (x - x0) * side > 0:
                out[c.candidate_id] = f"route: reveals the map to the {'right' if side > 0 else 'left'} (the screen scrolls)"
        return out

    # -- goal credit (control/credit.py) ---------------------------------------------------
    def _goal_targets(self, reach: ReachMap, goal: Goal) -> set[Cell]:
        """Where the goal is done from: the target's standing and take-off cells; for an explore
        goal, the explored map's edge that way."""
        if goal.goal_type == "explore":
            side = DIRECTIONS.get(goal.target_ref.split(":")[-1], 0)
            return frontier(reach, side) if side else set()
        final = _goal_tile(goal)
        return reach.targets_for((final.col, final.row))

    def _credit_step(self, obs: Observation, memory: WorkingMemory, events: list[Event], achieved: bool) -> None:
        """Credit the skill just run (working memory's newest entry) for the active goal: whether
        it brought Dave closer to the target by the reach estimate, back where he had been, or
        killed him. Skills started in the air, and level changes, are not credited."""
        goal, reach = self.goal, self._reach_map()
        entry = memory.history[-1] if memory.history else None
        if goal is None or reach is None or entry is None or entry.end_frame != obs.frame \
                or entry.start_position is None or obs.level_id != self.level_id:
            return
        start = reach.locate(entry.start_position.x, entry.start_position.y)
        if start is None:
            return
        end = (reach.locate(obs.player_position.x, obs.player_position.y)
               if obs.player_position is not None and obs.player_state in STANDING_STATES else None)
        targets = self._goal_targets(reach, goal)
        before = reach.cost(start, targets) if targets else None
        after = 0.0 if achieved else (reach.cost(end, targets) if end is not None and targets else None)
        died = obs.player_state == "burning" or any(e.event_type == "death" for e in events)
        self.credit.record(start, entry.skill, before, after, end, died)

    def _finish_credit(self, achieved: bool) -> None:
        done = self.credit.finish(achieved)
        if done is not None:
            self._credit_done.append(done)

    def _drain_credit(self) -> list[GoalSteps]:
        done, self._credit_done = self._credit_done, []
        return done

    def _grab_notes(self, obs: Observation, reach: ReachMap, candidates: list[SkillCandidate],
                    specs: dict[str, SkillSpec]) -> dict[str, str]:
        """``route:`` notes on the candidates whose simulated path touches the goal's target and
        no hazard: they take it on the way, mid-jump included (level 3's gun floats over a vine
        and is taken only in flight). Same for every arm."""
        goal = self.goal
        if goal is None or goal.goal_type not in ("collect", "reach") or obs.player_position is None:
            return {}
        target = _goal_tile(goal)
        item = (target.col, target.row)
        pos = obs.player_position
        jumping = obs.player_state == "jumping" and obs.jump_tick is not None
        if not jumping and obs.player_state not in STANDING_STATES:
            return {}
        parts = goal.target_ref.split(":")
        verb = "picks up" if goal.goal_type == "collect" else "reaches"
        what = f"{verb} the {parts[1] if len(parts) > 1 else 'target'}"
        out = {}
        for c in candidates:
            spec = specs[c.skill]
            path = (trace_in_jump(reach, pos.x, pos.y, obs.jump_tick, spec) if jumping and obs.jump_tick is not None
                    else trace_skill(reach, pos.x, pos.y, spec))
            if grabs(path, item) and not reach.burns(path):
                out[c.candidate_id] = f"route: {what} on the way"
        return out

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
        if kind == "fly":
            return {c.candidate_id: f"route: turns the jetpack on for the flight to {target}"
                    for c in candidates if specs[c.skill].buttons == {"jetpack"}}
        # A landing on the same platform makes the same move: Dave's x is rarely a multiple of 16,
        # so a jump from where he really stands often lands a cell short or long (level 3: from x 94
        # the long jump lands on (11,6), the plan's take-off at x 96 on (12,6)).
        makes = [c.candidate_id for c in candidates
                 if kind != "walk" and reach.same_platform(ends.get(c.candidate_id), landing)]
        if makes:
            off = {cid: abs(ends[cid][0] - landing[0]) for cid in makes if ends.get(cid) is not None}
            makes = [cid for cid in makes if off.get(cid) == min(off.values())]
            return {cid: f"route: makes the next move ({kind} to {target})" for cid in makes}
        # The walks that keep Dave on this platform: toward a walk-only target, or to a spot from
        # which one of the catalog's jumps lands where the next move does (simulated, since the
        # working take-off can be anywhere on the platform, e.g. its far end).
        # The take-off can be several walks away (level 3: between two vine clumps, the jump over
        # the next one only clears it from the far end of the platform), so the walks that lead
        # there are searched up to TAKEOFF_WALKS deep and the first walk of the shortest is marked.
        y = obs.player_position.y
        # On-ground skills only: the jetpack's flights press the same keys (up is the jump key).
        ground = [spec for spec in specs.values() if "jetpacking" not in spec.preconditions]
        jumps = [spec for spec in ground if "jump" in spec.phases[0].buttons]
        walks = [spec for spec in ground if "jump" not in spec.phases[0].buttons and "fire" not in spec.buttons
                 and any(b in DIRECTIONS for b in spec.buttons)]

        def walk(px: int, spec: SkillSpec) -> int | None:
            """Dave's x after ``spec`` from x ``px``, if he is still standing on this row and
            touched no hazard."""
            path = trace_skill(reach, px, y, spec)
            if reach.burns(path):
                return None
            end_x, end_y = path[-1]
            ground = reach._ground(end_x, end_y)
            return end_x if ground is not None and ground[1] == here[1] and end_y == y else None

        # A fall is taken off by a walk that drops off the platform's end onto the landing (level
        # 4: from (64,5) one walk stops 4 px short of the edge and three walk on past (65,6) into
        # the pit below, so it takes two walks).
        movers = walks if kind == "fall" else jumps

        def takes_off(px: int) -> bool:
            return any(reach.same_platform(estimate_end_at(reach, px, y, j), landing)
                       and not reach.burns(trace_skill(reach, px, y, j)) for j in movers)

        depth: dict[str, int] = {}
        for c in candidates:
            spec = specs[c.skill]
            if spec not in walks or ends.get(c.candidate_id) is None:
                continue
            end_x = walk(x, spec)
            if end_x is None:
                continue  # walked off the platform or into a hazard
            if kind == "walk":
                if abs(end_x - landing[0] * TILE) < abs(x - landing[0] * TILE):
                    depth[c.candidate_id] = 1
                continue
            frontier, seen = {end_x}, {x, end_x}
            for d in range(1, TAKEOFF_WALKS + 1):
                if any(takes_off(px) for px in frontier):
                    depth[c.candidate_id] = d
                    break
                frontier = {nx for px in frontier for s in walks if (nx := walk(px, s)) is not None} - seen
                seen |= frontier
        if not depth:
            return {}
        best = min(depth.values())
        if kind == "walk":
            return {cid: f"route: toward {target}" for cid in depth}
        how = "" if best == 1 else f" ({best} walks)"
        where = "the edge" if kind == "fall" else "the take-off"
        return {cid: f"route: walks to {where}{how} for the {kind} to {target}"
                for cid, d in depth.items() if d == best}

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
                                "give platform ids from `platforms`")]
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
        missed = {cell for cell, _ in self._misses}
        return ReachMap(self._cells, self.reach, self.log.failures(), self._risky | missed, fuel=self._flight_fuel())

    def _flight_fuel(self) -> int:
        """The fuel routes may fly on: all of it for the trophy and the door, what is above
        ``FUEL_RESERVE`` for anything else."""
        ref = self.goal.target_ref if self.goal is not None else ""
        return self._fuel if ref.startswith(REQUIRED_GOALS) else max(0, self._fuel - FUEL_RESERVE)

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

    def predicted_safe(self, candidate_id: str) -> bool | None:
        """Whether the threat screen predicted no contact for this option of the last decision
        (None when it was not assessed)."""
        if candidate_id not in self._predicted:
            return None
        return not _blocks(self._predicted[candidate_id])

    def _forecast_miss(self, obs: Observation, run: ExecutionResult | None) -> Event | None:
        """A death by plasma or a monster after a move the screen predicted safe: remembered for
        the tile and skill (a note on that option, and a route cost on the tile), and returned as a
        ``forecast_miss`` event, so the forecast can be fixed (scripts/audit_threats.py)."""
        if run is None or self.predicted_safe(run.candidate_id) is not True or self._predicted_at is None:
            return None
        hit = next((s.observation for s in run.steps if s.observation.player_state == "burning"), obs)
        cause, tile = contact_cause(hit)
        if cause in ("hazard", "unknown"):
            return None  # fire or water, or nothing seen: not the monsters' forecast
        self._misses.setdefault((self._predicted_at, run.skill), []).append(cause)
        return Event(event_type="forecast_miss", episode_id=obs.episode_id, frame=obs.frame, certainty="derived",
                     payload={"skill": run.skill, "from": list(self._predicted_at), "cause": cause, "tile": tile})

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
            for c, kind in steps:
                # A jump carries its simulated flight in tile units (every 3rd tick), for drawing.
                pts = [[round(px / TILE, 2), round(py / TILE, 2)] for px, py in flight(reach, prev, c)[::3]]                     if kind == "jump" else None
                out.append([c[0], c[1], kind, pts] if pts else [c[0], c[1], kind])
                prev = c
        return out

    def _escalate(self, triggers: set[str], rule: GoalCandidate) -> bool:
        """Escalate mode: whether the planner model decides this time. It does on a trigger that
        needs reasoning (stuck, repeated failures, a death), and when the rule's choice already
        ended without success ``rule_repeat_limit`` times on this level (a rule loop)."""
        if triggers & ESCALATE_TRIGGERS:
            return True
        failed = sum(a["goal"] == rule.candidate_id and a["outcome"] != "achieved" for a in self.log.attempts)
        return failed >= self.cfg.rule_repeat_limit

    def _requires(self, obs: Observation,
                  candidates: tuple[GoalCandidate, ...]) -> tuple[GoalCandidate, ...]:
        """Mark the item goals that only a flight reaches while Dave has no fuel: ``requires``
        the jetpack, and the path says so (with where the jetpack is, when known)."""
        here = self._located(obs)
        if self._fuel or here is None or self.reach is None:
            return candidates
        jetpack = next((c for c in candidates if c.goal_type == "collect" and c.target_name == "jetpack"), None)
        flying = ReachMap(self._cells, self.reach, self.log.failures(), self._risky, fuel=FULL_FUEL)
        out = []
        for c in candidates:
            if c.goal_type in ("collect", "reach") and c is not jetpack and _no_path(c) and                     flying.path(here, flying.targets_for((c.target.col, c.target.row))) is not None:
                where = f"; jetpack at ({jetpack.target.col},{jetpack.target.row})" if jetpack else ""
                c = c.model_copy(update={"requires": ("jetpack",),
                                         "path": f"{c.path}; reachable with the jetpack (not held{where})"})
            out.append(c)
        return tuple(out)

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
        out.note_fire(self._risky)
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
    def _move_scores(self, obs: Observation, candidates: list[SkillCandidate],
                     ends: dict[str, Cell | None]) -> dict[str, MoveScore]:
        """Each option's cost to the goal from the live graph, and its regret against the best
        option from here (control/move_score.py). Empty off a mapped platform or without a graph."""
        if self.graph is None or self.graph_cfg is None:
            return {}
        graph = self.graph.get(obs.level_id)
        here = None if graph is None else graph.locate(obs)
        if here is None:
            return {}
        target = ((self._tracker.route.target or None)
                  if self._tracker is not None and self.goal is not None else None)
        target_ref = self.goal.target_ref if self.goal is not None else None
        values, mode = position_values(graph, target, obs.inventory, self.graph_cfg, target_ref)
        goal = None if target is None or self.goal is None else (target, _goal_tile(self.goal).col)
        return score_moves(graph, here, values, mode, candidates, ends, obs.inventory, self.graph_cfg, target_ref,
                           goal, player_tile(obs.player_position).col if obs.player_position else None, self._hits)

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
            route = find_route(graph, start, node, obs.inventory, self.graph_cfg,  # type: ignore[arg-type]
                               candidate.candidate_id)
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
        return find_route(graph, start, target, obs.inventory, self.graph_cfg, candidate.candidate_id)

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
                    "grounded": obs.grounded, "facing": obs.facing,
                    "fuel": {"left": self._fuel, "reserve": FUEL_RESERVE}},
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


def _no_path(candidate: GoalCandidate) -> bool:
    return bool(candidate.path and candidate.path.startswith("no known path"))


def _blocks(contact: Contact | None) -> bool:
    """A predicted touch that masks; an edge contact (the path passes the screen edge) does not."""
    return contact is not None and contact.blocks
