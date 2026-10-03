"""Single-episode control loop: observe -> candidates -> decide -> revalidate -> execute.

Phase 1 runs without a planner or memory; those attach in Phases 4-6 without changing
this loop's observation/candidate/executor path, which every arm shares. Execution is
paused-step: the game advances only inside ``execute``, so it is frozen while a
controller decides.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from dave_agent.adapters.base import GameAdapter
from dave_agent.config import ExecutorConfig, SkillSpec
from dave_agent.control.skills import ExecutionResult, execute, generate_candidates
from dave_agent.models.base import TacticalController
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
    outcome: str  # level_complete | game_over | secret_exit | truncated
    frames: int
    score: int | None
    lives: int | None
    decisions: list[Decision] = field(default_factory=list)
    model_calls: list[ModelCallRecord] = field(default_factory=list)
    events: list[Event] = field(default_factory=list)
    observation_ids: list[int] = field(default_factory=list)
    candidate_sets: list[CandidateRecord] = field(default_factory=list)
    executions: list[ExecutionResult] = field(default_factory=list)


def run_episode(
    adapter: GameAdapter,
    controller: TacticalController,
    skills: tuple[SkillSpec, ...],
    executor: ExecutorConfig,
    scenario_id: str,
    seed: int,
    max_frames: int,
) -> EpisodeResult:
    observation = adapter.reset(scenario_id, seed)
    buttons = adapter.capabilities().buttons
    result = EpisodeResult(
        episode_id=observation.episode_id,
        adapter=observation.adapter,
        outcome="truncated",
        frames=0,
        score=observation.score,
        lives=observation.lives,
    )
    result.events.append(
        Event(event_type="episode_start", episode_id=observation.episode_id, frame=observation.frame)
    )
    result.observation_ids.append(observation.observation_id)

    while observation.terminal == "running" and observation.frame < max_frames:
        offered = generate_candidates(skills, buttons, observation)
        if not offered.candidates:
            raise RuntimeError(f"no legal candidates at frame {observation.frame}: {offered.masked}")
        result.candidate_sets.append(
            CandidateRecord(observation.observation_id, observation.frame, offered.ids, offered.masked, offered.digest())
        )
        candidates = list(offered.candidates)
        if len(candidates) == 1:
            # Only one legal action (e.g. waiting out a burn): no model call, same for every arm.
            decision = Decision(
                candidate_id=candidates[0].candidate_id, observation_id=observation.observation_id, forced=True
            )
        else:
            decision, call = controller.decide(observation, None, candidates)
            result.model_calls.append(call)
        candidate = validate_decision(decision, candidates, observation)
        result.decisions.append(decision)
        run = execute(adapter, candidate, skills, observation, executor)
        result.executions.append(run)
        result.events.extend(run.events)
        if run.outcome == "rejected":
            # Candidates were generated from this observation, so this indicates a bug.
            raise RuntimeError(f"candidate {candidate.candidate_id} rejected on its own observation: {run.reason}")
        observation = run.observation
        result.observation_ids.append(observation.observation_id)
        log.debug("frame=%d skill=%s outcome=%s reason=%s", observation.frame, run.skill, run.outcome, run.reason)

    if observation.terminal == "running":
        result.events.append(
            Event(event_type="episode_truncated", episode_id=observation.episode_id, frame=observation.frame)
        )
    else:
        result.outcome = observation.terminal
    result.frames = observation.frame
    result.score = observation.score
    result.lives = observation.lives
    return result
