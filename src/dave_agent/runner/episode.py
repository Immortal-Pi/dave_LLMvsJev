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
"""

from __future__ import annotations

import copy
import logging
import time
from dataclasses import dataclass, field

from dave_agent.adapters.base import GameAdapter
from dave_agent.config import ExecutorConfig, SkillSpec
from dave_agent.control.experience import annotate_experience
from dave_agent.control.goals import GoalManager, PlanningRecord, PlanningStep
from dave_agent.control.skills import ExecutionResult, execute, generate_candidates
from dave_agent.memory.detector import EventDetector
from dave_agent.memory.episodes import EpisodeRecorder
from dave_agent.memory.graph import GraphStore
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
) -> EpisodeResult:
    started = time.monotonic()
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

    def planned(step: PlanningStep) -> None:
        result.model_calls.extend(step.calls)
        result.events.extend(step.events)
        if step.record is not None:
            result.planning.append(step.record)
        if recorder is not None and (step.calls or step.events):
            recorder.record_planning(step.calls, step.events)

    stop: tuple[str, dict] | None = None  # (termination reason, truncation payload) for budget stops
    try:
        if goals is not None:
            planned(goals.reset(observation, memory))
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
            events: list[Event] = []
            if len(candidates) == 1:
                # Only one legal action (e.g. waiting out a burn): no model call, same for every arm.
                decision = Decision(
                    candidate_id=candidates[0].candidate_id, observation_id=observation.observation_id, forced=True
                )
            else:
                if observation.player_position is not None:
                    candidates = annotate_experience(
                        candidates, memory.experience(player_tile(observation.player_position)),
                        None if past is None else past.skill_evidence(observation))
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
                try:
                    decision, calls = controller.decide(observation, memory.goal, candidates, memory.context())
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
            result.decisions.append(decision)
            run = execute(adapter, candidate, skills, observation, executor)
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
            if graph is not None:
                ref = f"{recorder.episode_key if recorder else observation.episode_id}#{observation.observation_id}"
                graph.record_execution(observation, run, ref)
            result.executions.append(run)
            tile = None if observation.player_position is None else player_tile(observation.player_position)
            result.execution_starts.append(None if tile is None else (tile.col, tile.row))
            result.events.extend(events)
            memory.record(decision, run)
            if recorder is not None:
                recorder.record(observation, offered, decision, calls, run, events)
            observation = run.observation
            result.observation_ids.append(observation.observation_id)
            if goals is not None:
                planned(goals.update(observation, memory, events, run))
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
    return result


def _failure_events(calls, observation) -> list[Event]:
    return [Event(event_type="model_failure", episode_id=observation.episode_id, frame=observation.frame,
                  payload={"provider": c.provider, "model": c.model, "purpose": c.purpose, "status": c.status})
            for c in calls if c.status != "ok"]
