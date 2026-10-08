"""Single-episode control loop: observe -> remember -> candidates -> decide -> revalidate -> execute.

Every arm shares this loop's observation, working-memory, candidate and executor path. Execution
is paused-step: the game advances only inside ``execute``, so it is frozen while a planner or
controller decides. The optional recorder is write-only, so episode logging never feeds back
into decisions. The optional learned graph (graph-enabled arms only) is updated from the same
observations. The optional goal manager runs at decision boundaries: after the episode starts
and after each skill it ends goals that are due and calls the planner on shared triggers; on
graph-enabled arms it also turns the goal into a route waypoint (control/goals.py).
Before each model decision the candidates get experience notes (control/experience.py): what
each skill did from this tile earlier in the episode (every arm), and on graph-enabled arms what
it did from this platform in past runs (the graph as it was at episode start).
Budgets: the simulation-frame budget (``max_frames``) and the wall-time budget
(``max_wall_seconds``) are checked before every decision; model-backed controllers raise
``BudgetExhausted`` when a call/token/cost budget runs out with ``on_budget_exhausted:
terminate``. Each ends the episode as ``truncated`` with the budget in the termination reason. Ctrl-C ends the episode as ``truncated`` / ``interrupted``,
writes what was recorded so far, and re-raises.
The optional ``on_event`` callback receives a JSON-ready summary of each step as it happens
(``episode``, ``plan``, ``goal``, ``deciding``, ``decision``, ``outcome``, and ``graph`` on
graph-enabled arms) for the live viewer
(runner/live.py). It is write-only: nothing it does feeds back into decisions.
Real-time mode (``realtime=True``, ``environment.execution_mode: real_time``; the live viewer's
"pause while thinking" off): planner and tactical calls run on a worker thread while this
thread keeps the game going, one tick at a time with no keys pressed (Dave stands still). After
the wait the choice is checked against the latest observation: it is dropped
(``decision_stale``) and decided again when Dave died, respawned or the episode ended during
the wait, or the chosen skill is no longer legal or now screened out; otherwise that skill runs
from the latest observation (``decision_latency`` records the wait). Real-time episodes depend
on model latency, so they cannot be replayed exactly.
"""

from __future__ import annotations

import copy
import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from dave_agent.adapters.base import GameAdapter
from dave_agent.config import ExecutorConfig, SkillSpec
from dave_agent.control.experience import annotate_experience
from dave_agent.control.goals import GoalManager, PlanningRecord, PlanningStep
from dave_agent.control.skills import ExecutionResult, execute, generate_candidates
from dave_agent.control.threats import forecast
from dave_agent.memory.detector import EventDetector
from dave_agent.memory.episodes import EpisodeRecorder
from dave_agent.memory.graph import GraphStore, edge_key, inventory_context
from dave_agent.memory.working import WorkingMemory, player_tile
from dave_agent.models.base import TacticalController
from dave_agent.models.tactical import BudgetExhausted
from dave_agent.schemas import Decision, Event, ModelCallRecord, validate_decision

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class CandidateRecord:
    """What every arm was offered at one decision point."""

    observation_id: int
    frame: int
    candidate_ids: tuple[str, ...]
    masked: dict[str, str]
    digest: str


@dataclass
class EpisodeResult:
    episode_id: str
    adapter: str
    outcome: str  # level_complete | game_over | secret_exit | truncated | error
    termination_reason: str
    frames: int
    score: int | None
    lives: int | None
    decisions: list[Decision] = field(default_factory=list)
    model_calls: list[ModelCallRecord] = field(default_factory=list)
    events: list[Event] = field(default_factory=list)
    observation_ids: list[int] = field(default_factory=list)
    candidate_sets: list[CandidateRecord] = field(default_factory=list)
    executions: list[ExecutionResult] = field(default_factory=list)
    planning: list[PlanningRecord] = field(default_factory=list)
    # Monotonic wall time of the whole episode (reset to the end, model waits included).
    wall_seconds: float = 0.0
    # Per model-chosen decision (not forced): summed latency of every call made for it, re-asks
    # included. None when a call reported no latency.
    decision_latency_ms: list[float | None] = field(default_factory=list)
    decision_calls: list[int] = field(default_factory=list)  # calls per model-chosen decision
    # Player tile (col, row) where each execution started; None when the position is unavailable.
    execution_starts: list[tuple[int, int] | None] = field(default_factory=list)


def run_episode(
    adapter: GameAdapter,
    controller: TacticalController,
    skills: tuple[SkillSpec, ...],
    executor: ExecutorConfig,
    memory: WorkingMemory,
    scenario_id: str,
    seed: int,
    max_frames: int,
    recorder: EpisodeRecorder | None = None,
    graph: GraphStore | None = None,
    goals: GoalManager | None = None,
    max_wall_seconds: float | None = None,
    evidence: GraphStore | None = None,
    on_event: Callable[[str, dict[str, Any]], None] | None = None,
    realtime: bool = False,
) -> EpisodeResult:
    started = time.monotonic()

    def emit(kind: str, payload: dict[str, Any]) -> None:
        if on_event is not None:
            try:
                on_event(kind, payload)
            except Exception:  # a viewer problem never stops the episode
                log.exception("on_event(%s) failed", kind)

    # ``evidence``: the graph-enabled arm's graph (learning or frozen). Its "past runs" notes come
    # from a copy taken before this episode, so they never count this episode's attempts (those
    # are in the working-memory notes).
    past = copy.deepcopy(evidence) if evidence is not None else None
    observation = adapter.reset(scenario_id, seed)
    memory.reset(observation)
    if graph is not None:
        graph.observe(observation)
    detector = EventDetector()
    buttons = adapter.capabilities().buttons
    result = EpisodeResult(
        episode_id=observation.episode_id,
        adapter=observation.adapter,
        outcome="truncated",
        termination_reason="",
        frames=0,
        score=observation.score,
        lives=observation.lives,
    )
    start_events = [Event(event_type="episode_start", episode_id=observation.episode_id, frame=observation.frame,
                          payload={"scenario_id": scenario_id, "seed": seed}),
                    *detector.reset(observation)]
    result.events.extend(start_events)
    result.observation_ids.append(observation.observation_id)
    if recorder is not None:
        recorder.start(observation, start_events)
    emit("episode", {"status": "started", "scenario_id": scenario_id, "seed": seed, **_obs_view(observation)})

    def planned(step: PlanningStep) -> None:
        if graph is not None:
            for goal in step.credit:  # what each ended goal's moves did toward it (control/credit.py)
                graph.record_credit(goal)
        result.model_calls.extend(step.calls)
        result.events.extend(step.events)
        if step.record is not None:
            result.planning.append(step.record)
        if recorder is not None and (step.calls or step.events):
            recorder.record_planning(step.calls, step.events)
        for e in step.events:
            if e.event_type in ("goal_achieved", "goal_failed"):
                emit("goal", {"frame": e.frame, **e.payload})
        if step.record is not None:
            emit("plan", _plan_view(step))

    def while_playing(fn: Callable[[], Any], start) -> tuple[Any, list]:
        """``fn()``; in real-time mode on a worker thread while the game runs on with no keys
        pressed. Returns its value and the steps taken meanwhile (none when paused)."""
        if not realtime:
            return fn(), []
        box: dict[str, Any] = {}

        def work() -> None:
            try:
                box["value"] = fn()
            except BaseException as exc:  # re-raised on this thread
                box["error"] = exc

        worker = threading.Thread(target=work, daemon=True, name="dave-decide")
        worker.start()
        steps, latest = [], start
        while worker.is_alive() and latest.terminal == "running":
            step = adapter.step(frozenset(), 1)
            steps.append(step)
            latest = step.observation
        worker.join()
        if "error" in box:
            raise box["error"]
        return box["value"], steps

    def absorb(steps) -> list[Event]:
        """Fold the steps taken during a wait into memory, the graph and the goal manager."""
        waited: list[Event] = []
        for step in steps:
            waited.extend(step.events)
            waited.extend(detector.observe(step.observation))
            if graph is not None:
                graph.observe(step.observation)
            if goals is not None:
                goals.observe(step.observation)
        if steps:
            memory.advance([s.observation for s in steps])
        return waited

    pending: list[Event] = []  # events of a real-time wait, carried into the next decision's record
    stop: tuple[str, dict] | None = None  # (termination reason, truncation payload) for budget stops
    try:
        if goals is not None:
            step, idle = while_playing(lambda: goals.reset(observation, memory), observation)
            planned(step)
            if idle:
                pending.extend(absorb(idle))
                observation = idle[-1].observation
                result.observation_ids.append(observation.observation_id)
        shown = graph if graph is not None else past  # the graph the viewer draws (learning or frozen)
        if shown is not None and on_event is not None:
            emit("graph", _graph_view(shown, observation.level_id, graph is not None))
        while observation.terminal == "running" and observation.frame < max_frames:
            if max_wall_seconds is not None and time.monotonic() - started >= max_wall_seconds:
                stop = (f"budget:wall_time:{max_wall_seconds:g}s", {"budget": "wall_time",
                                                                    "max_wall_seconds": max_wall_seconds})
                break
            offered = generate_candidates(skills, buttons, observation)
            if not offered.candidates:
                raise RuntimeError(f"no legal candidates at frame {observation.frame}: {offered.masked}")
            result.candidate_sets.append(
                CandidateRecord(observation.observation_id, observation.frame, offered.ids, offered.masked,
                                offered.digest())
            )
            candidates = list(offered.candidates)
            calls: tuple[ModelCallRecord, ...] = ()
            events: list[Event] = pending
            pending = []
            screened: dict[str, str] = {}
            idle: list = []
            start = observation  # where the skill runs from (later than the decision's in real time)
            wait: dict[str, Any] = {}
            if len(candidates) == 1:
                # Only one legal action (e.g. waiting out a burn): no model call, same for every arm.
                decision = Decision(
                    candidate_id=candidates[0].candidate_id, observation_id=observation.observation_id, forced=True
                )
            else:
                if observation.player_position is not None:
                    target = memory.goal.target_ref if memory.goal is not None else None
                    candidates = annotate_experience(
                        candidates, memory.experience(player_tile(observation.player_position)),
                        None if past is None else past.skill_evidence(observation),
                        None if past is None or target is None else past.credit_evidence(observation, target))
                if goals is not None:
                    # Estimated end tiles and predicted threat contacts in the descriptions; candidates
                    # predicted to touch a threat are masked (control/threats.py). The offered set and
                    # its digest above are unchanged; the screen depends only on the observation and
                    # the shared cell memory, so it is identical for every arm and replays exactly.
                    candidates, screened = goals.annotate(observation, candidates, skills)
                    if screened:
                        events.append(Event(event_type="candidates_screened", episode_id=observation.episode_id,
                                            frame=observation.frame, certainty="derived",
                                            payload={"masked": screened,
                                                     "kept": [c.candidate_id for c in candidates]}))
                emit("deciding", {"frame": observation.frame, "options": len(candidates), "screened": screened})
                try:
                    context = memory.context()
                    (decision, calls), idle = while_playing(
                        lambda: controller.decide(observation, memory.goal, candidates, context), observation)
                except BudgetExhausted as exc:
                    # Calls made for the interrupted decision are still logged.
                    result.model_calls.extend(exc.calls)
                    failures = _failure_events(exc.calls, observation)
                    result.events.extend(failures)
                    if recorder is not None and (exc.calls or failures):
                        recorder.record_planning(list(exc.calls), failures)
                    stop = (f"budget:{exc.budget}", {"budget": exc.budget})
                    break
                result.model_calls.extend(calls)
                result.decision_latency_ms.append(
                    None if any(c.latency_ms is None for c in calls) else sum(c.latency_ms for c in calls))
                result.decision_calls.append(len(calls))
                events.extend(_failure_events(calls, observation))
                if decision.fallback:
                    events.append(Event(event_type="decision_fallback", episode_id=observation.episode_id,
                                        frame=observation.frame, certainty="derived",
                                        payload={"reason": decision.fallback_reason,
                                                 "candidate_id": decision.candidate_id, "calls": len(calls)}))
            candidate = validate_decision(decision, candidates, observation)
            if idle:
                waited = absorb(idle)
                events.extend(waited)
                latest = idle[-1].observation
                fresh, stale = _still_valid(candidate, waited, latest, skills, buttons, goals)
                wait = {"wait_ticks": len(idle), "decided_frame": observation.frame, "start_frame": latest.frame}
                if stale is not None:
                    # Too late: record the calls and what happened, then decide again on the latest state.
                    events.append(Event(event_type="decision_stale", episode_id=observation.episode_id,
                                        frame=latest.frame, certainty="derived",
                                        payload={**wait, "candidate_id": decision.candidate_id, "reason": stale}))
                    result.events.extend(events)
                    if recorder is not None:
                        recorder.record_planning(list(calls), events)
                    emit("decision", {**_decision_view(observation, memory, candidates, screened, decision, calls,
                                                        goals.scores if goals is not None else {}),
                                      **wait, "stale": stale})
                    observation = latest
                    result.observation_ids.append(observation.observation_id)
                    continue
                wait["runs"] = fresh.candidate_id  # its id on the latest observation (ids are positional)
                events.append(Event(event_type="decision_latency", episode_id=observation.episode_id,
                                    frame=latest.frame, certainty="derived",
                                    payload={**wait, "candidate_id": decision.candidate_id}))
                candidate, start = fresh, latest
            result.decisions.append(decision)
            emit("decision", {**_decision_view(observation, memory, candidates, screened, decision, calls,
                                                        goals.scores if goals is not None else {}), **wait})
            run = execute(adapter, candidate, skills, start, executor)
            if run.outcome == "rejected":
                # Candidates were generated from this observation, so this indicates a bug.
                raise RuntimeError(f"candidate {candidate.candidate_id} rejected on its own observation: {run.reason}")
            events.extend(run.events)
            for step in run.steps:
                events.extend(detector.observe(step.observation))
                if graph is not None:
                    graph.observe(step.observation)
                if goals is not None:
                    goals.observe(step.observation)
            recorded = None
            if graph is not None:
                ref = f"{recorder.episode_key if recorder else observation.episode_id}#{observation.observation_id}"
                recorded = graph.record_execution(start, run, ref,
                                                  None if goals is None else goals.predicted_safe(run.candidate_id))
            result.executions.append(run)
            emit("outcome", _outcome_view(run, events))
            if graph is not None and on_event is not None:
                emit("graph", {**_graph_view(graph, observation.level_id, True),
                               "last": _graph_last(graph, start, run, recorded)})
            tile = None if start.player_position is None else player_tile(start.player_position)
            result.execution_starts.append(None if tile is None else (tile.col, tile.row))
            result.events.extend(events)
            memory.record(decision, run)
            if recorder is not None:
                recorder.record(observation, offered, decision, calls, run, events)
            observation = run.observation
            result.observation_ids.append(observation.observation_id)
            if goals is not None:
                step, idle = while_playing(lambda: goals.update(observation, memory, events, run), observation)
                planned(step)
                if idle:
                    pending.extend(absorb(idle))
                    observation = idle[-1].observation
                    result.observation_ids.append(observation.observation_id)
            log.debug("frame=%d skill=%s outcome=%s reason=%s", observation.frame, run.skill, run.outcome, run.reason)
    except (Exception, KeyboardInterrupt) as exc:
        if isinstance(exc, KeyboardInterrupt):
            result.outcome, result.termination_reason = "truncated", "interrupted"
        else:
            result.outcome, result.termination_reason = "error", f"error:{type(exc).__name__}: {exc}"
        result.wall_seconds = time.monotonic() - started
        result.frames, result.score, result.lives = observation.frame, observation.score, observation.lives
        if recorder is not None:
            # Writes the batched rows too, so an interrupted run keeps its decisions.
            recorder.finish(result.outcome, result.termination_reason, observation, [])
        exc.episode_result = result  # the partial evidence, for callers that record failed episodes
        raise

    end_events: list[Event] = []
    if stop is not None:
        result.termination_reason = stop[0]
        end_events.append(Event(event_type="episode_truncated", episode_id=observation.episode_id,
                                frame=observation.frame, payload=stop[1]))
    elif observation.terminal == "running":
        result.termination_reason = f"max_frames:{max_frames}"
        end_events.append(Event(event_type="episode_truncated", episode_id=observation.episode_id,
                                frame=observation.frame, payload={"max_frames": max_frames}))
    else:
        result.outcome = observation.terminal
        result.termination_reason = f"terminal:{observation.terminal}"
    result.events.extend(end_events)
    result.frames = observation.frame
    result.score = observation.score
    result.lives = observation.lives
    result.wall_seconds = time.monotonic() - started
    if recorder is not None:
        recorder.finish(result.outcome, result.termination_reason, observation, end_events)
    emit("episode", {"status": "finished", "outcome": result.outcome,
                     "termination_reason": result.termination_reason,
                     "deaths": sum(e.event_type == "death" for e in result.events), **_obs_view(observation)})
    return result


STALE_EVENTS = frozenset({"death", "respawn"})


def _still_valid(candidate, waited: list[Event], latest, skills, buttons,
                 goals: GoalManager | None) -> tuple[Any, str | None]:
    """After a real-time wait: the candidate for the chosen skill on the latest observation, or
    why the choice is stale (Dave died or respawned, the episode ended, or the skill is no
    longer legal or is now screened out)."""
    happened = next((e.event_type for e in waited if e.event_type in STALE_EVENTS), None)
    if happened is not None:
        return None, f"event:{happened}"
    if latest.terminal != "running":
        return None, f"terminal:{latest.terminal}"
    offered = generate_candidates(skills, buttons, latest)
    fresh = next((c for c in offered.candidates if c.skill == candidate.skill), None)
    if fresh is None:
        return None, f"illegal:{offered.masked.get(candidate.skill, 'not offered')}"
    if goals is not None and len(offered.candidates) > 1:
        kept, screened = goals.annotate(latest, list(offered.candidates), skills)
        if fresh.candidate_id in screened:
            return None, f"screened:{screened[fresh.candidate_id]}"
        fresh = next(c for c in kept if c.candidate_id == fresh.candidate_id)
    return fresh, None


def _failure_events(calls, observation) -> list[Event]:
    return [Event(event_type="model_failure", episode_id=observation.episode_id, frame=observation.frame,
                  payload={"provider": c.provider, "model": c.model, "purpose": c.purpose, "status": c.status})
            for c in calls if c.status != "ok"]


# -- live viewer summaries (on_event) ------------------------------------------------------------
def _tile(pos) -> list[int] | None:
    if pos is None:
        return None
    t = player_tile(pos)
    return [t.col, t.row]


def _obs_view(obs) -> dict[str, Any]:
    return {"frame": obs.frame, "level_id": obs.level_id, "tile": _tile(obs.player_position),
            "state": obs.player_state, "score": obs.score, "lives": obs.lives, "inventory": obs.inventory}


def _call_view(call: ModelCallRecord) -> dict[str, Any]:
    """One model call for the viewer; input and output tokens under one name for Azure
    (prompt/completion, reasoning included in completion) and Jev (input/output)."""
    usage = call.usage or {}
    return {"provider": call.provider, "model": call.model, "status": call.status, "latency_ms": call.latency_ms,
            "cost_usd": call.cost_usd, "tokens": usage.get("total_tokens"),
            "input_tokens": usage.get("prompt_tokens", usage.get("input_tokens")),
            "output_tokens": usage.get("completion_tokens", usage.get("output_tokens"))}


def _graph_view(store: GraphStore, level_id: str, learning: bool) -> dict[str, Any]:
    level = store.get(level_id)
    return {"source": "learning" if learning else "frozen", "level_id": level_id,
            "graph": None if level is None else level.view()}


def _graph_last(store: GraphStore, start, run: ExecutionResult, recorded: str | None) -> dict[str, Any]:
    """What the skill just added to the graph, and the edge it touched (None when there is none)."""
    edge = None
    level = store.get(start.level_id)
    if level is not None and recorded in ("success", "failure_edge"):
        source = level.locate(start)
        key = edge_key(run.skill, inventory_context(start))
        if recorded == "success":
            target = level.locate(run.observation)
        else:
            target = next((v for _, v, k in level.g.out_edges(source, keys=True) if k == key), None)
        if source is not None and target is not None:
            edge = [source, target, key]
    return {"recorded": recorded, "skill": run.skill, "edge": edge}


def _plan_view(step: PlanningStep) -> dict[str, Any]:
    r = step.record
    assert r is not None
    goal = next((e.payload for e in step.events if e.event_type == "goal_set"), {})
    return {"frame": r.frame, "triggers": list(r.triggers), "chosen": r.chosen, "goal_id": r.goal_id,
            "rationale": goal.get("rationale"), "waypoint": goal.get("waypoint"),
            "waypoints": goal.get("waypoints") or [], "route": r.route, "fallback": r.fallback,
            "fallback_reason": r.fallback_reason, "attempts": r.attempts, "errors": r.errors,
            "model_ms": r.model_ms, "calls": [_call_view(c) for c in step.calls],
            "candidates": [{"id": c.candidate_id, "description": c.description, "route": c.route, "path": c.path}
                           for c in r.request.candidates],
            "map": r.request.map, "platforms": list(r.request.platforms), "tried": list(r.request.attempts),
            "failed_links": list(r.request.failed_links), "path": goal.get("path") or [],
            "deaths": goal.get("deaths") or []}


THREAT_VIEW_TICKS = 96  # how far ahead the viewer draws each visible threat's predicted path
THREAT_VIEW_STEP = 4


def _threat_view(obs) -> list[dict[str, Any]]:
    """Each visible threat's predicted path with Dave standing still (centres in tile units, every
    few ticks), as the threat screen predicts it (control/threats.py ``forecast``): monsters on
    their route, flying plasma, and each shot still to be fired with the tick it is fired
    (``spawn``). For the viewer only."""
    out = []
    for f in forecast(obs, THREAT_VIEW_TICKS):
        w, h = (20, 3) if f["kind"] == "plasma" else (24, 21)
        pts = [[round((x + w / 2) / 16, 2), round((y + h / 2) / 16, 2)]
               for i, (x, y) in enumerate(f["path"]) if i % THREAT_VIEW_STEP == 0 or i == len(f["path"]) - 1]
        if len(pts) > 1:
            out.append({"id": f["id"], "kind": f["kind"], "spawn": f["spawn"], "path": pts})
    return out


def _decision_view(obs, memory: WorkingMemory, candidates, screened: dict[str, str], decision,
                   calls, scores: dict | None = None) -> dict[str, Any]:
    """``scores``: the goal manager's live move scores (graph arms; none for a forced decision,
    where no options were annotated)."""
    scores = {} if decision.forced or scores is None else scores
    tactical = [c for c in calls if c.purpose == "tactical"]
    probabilities = next((c.output.get("probabilities") for c in reversed(tactical)
                          if c.output and c.output.get("probabilities")), None)
    goal = memory.goal
    return {**_obs_view(obs), "observation_id": obs.observation_id,
            "goal": None if goal is None else {
                "target": goal.target_ref, "type": goal.goal_type,
                "waypoint": None if goal.next_waypoint is None else [goal.next_waypoint.col,
                                                                     goal.next_waypoint.row]},
            "candidates": [{"id": c.candidate_id, "skill": c.skill, "description": c.description,
                            "score": scores[c.candidate_id].view() if c.candidate_id in scores else None}
                           for c in candidates],
            "screened": screened, "chosen": decision.candidate_id, "forced": decision.forced,
            "fallback": decision.fallback, "fallback_reason": decision.fallback_reason,
            "probabilities": probabilities, "calls": [_call_view(c) for c in tactical],
            "threats": _threat_view(obs)}


def _outcome_view(run: ExecutionResult, events: list[Event]) -> dict[str, Any]:
    end = run.observation
    return {**_obs_view(end), "candidate_id": run.candidate_id, "skill": run.skill, "outcome": run.outcome,
            "reason": run.reason, "frames": run.frames,
            "events": [e.event_type for e in events if e.event_type in (
                "death", "item_collected", "respawn", "level_complete", "game_over", "inventory_changed")]}
