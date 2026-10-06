"""Legal candidate generation and bounded, frame-stepped skill execution.

Candidates are built by trusted code from the catalog and the latest observation; controllers
only choose among them, and every arm receives the identical list. Execution advances the
game one simulation frame at a time (never wall-clock sleeps), polls the observation after
each frame, and stops on interruption, phase timeout or the skill's hard frame cap. There
is no reflex behaviour: the executor never chooses inputs the catalog did not specify.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field

from dave_agent.adapters.base import GameAdapter
from dave_agent.config import ExecutorConfig, SkillSpec
from dave_agent.control.predicates import check, screen_still
from dave_agent.control.threats import STANDING_STATES, imminent, scripted_shots, threat_key, threats, time_to_contact
from dave_agent.schemas import Event, Observation, SkillCandidate, StepResult

MAX_FROZEN_TICKS = 64  # a skill waits out at most four screen scrolls (16 ticks each)

# Entity types that never threaten Dave (his own bullet).
HARMLESS_ENTITIES = frozenset({"bullet"})


@dataclass(frozen=True)
class CandidateSet:
    candidates: tuple[SkillCandidate, ...]
    masked: dict[str, str]  # skill name -> first failed precondition
    observation_id: int

    @property
    def ids(self) -> tuple[str, ...]:
        return tuple(c.candidate_id for c in self.candidates)

    def digest(self) -> str:
        """Stable hash of the offered list and mask, for cross-arm parity checks."""
        payload = json.dumps(
            {"candidates": [c.model_dump(mode="json") for c in self.candidates], "masked": self.masked},
            sort_keys=True,
        )
        return hashlib.sha256(payload.encode()).hexdigest()[:16]


@dataclass
class ExecutionResult:
    candidate_id: str
    skill: str
    outcome: str  # completed | interrupted | failed | rejected
    reason: str | None
    frames: int  # simulation frames advanced, including any respawn auto ticks
    input_ticks: int  # frames on which the skill applied input (bounded by max_frames)
    observation: Observation
    events: list[Event] = field(default_factory=list)
    steps: list[StepResult] = field(default_factory=list)
    frozen_ticks: int = 0  # frames the game ignored input while the screen scrolled (not in input_ticks)


def generate_candidates(
    skills: tuple[SkillSpec, ...], supported_buttons: frozenset[str], observation: Observation
) -> CandidateSet:
    """Offer every catalog skill whose buttons the adapter supports and whose
    preconditions hold, in catalog order. Masked skills are reported with the reason."""
    candidates: list[SkillCandidate] = []
    masked: dict[str, str] = {}
    if observation.terminal != "running":
        return CandidateSet((), {s.name: "terminal" for s in skills}, observation.observation_id)
    for spec in skills:
        missing = sorted(spec.buttons - supported_buttons)
        if missing:
            masked[spec.name] = f"unsupported_buttons:{','.join(missing)}"
            continue
        failed = next((p for p in spec.preconditions if not check(p, observation)), None)
        if failed is not None:
            masked[spec.name] = failed
            continue
        candidates.append(
            SkillCandidate(
                candidate_id=f"c{len(candidates)}_{spec.name}",
                skill=spec.name,
                description=spec.description,
                parameters={"kind": spec.kind},
                preconditions=spec.preconditions,
                max_frames=spec.max_frames,
            )
        )
    return CandidateSet(tuple(candidates), masked, observation.observation_id)


def find_spec(skills: tuple[SkillSpec, ...], name: str) -> SkillSpec:
    spec = next((s for s in skills if s.name == name), None)
    if spec is None:
        raise ValueError(f"unknown skill {name!r}")
    return spec


def revalidate(candidate: SkillCandidate, skills: tuple[SkillSpec, ...], observation: Observation) -> str | None:
    """Return why ``candidate`` may not run on ``observation`` (the latest), or None if it may."""
    if observation.terminal != "running":
        return f"terminal:{observation.terminal}"
    try:
        spec = find_spec(skills, candidate.skill)
    except ValueError as exc:
        return str(exc)
    if candidate.max_frames != spec.max_frames:
        return "max_frames_mismatch"
    failed = next((p for p in spec.preconditions if not check(p, observation)), None)
    return f"precondition:{failed}" if failed else None


def stale_fallback(candidates: tuple[SkillCandidate, ...], skills: tuple[SkillSpec, ...]) -> SkillCandidate | None:
    """Shared fallback when a decision arrives too late (real-time mode, Phase 11):
    the first offered candidate that presses no buttons, i.e. a wait."""
    for c in candidates:
        if not find_spec(skills, c.skill).buttons:
            return c
    return None


def _center(x: int, y: int) -> tuple[int, int]:
    return x + 8, y + 8


def _new_hazard(obs: Observation, seen: frozenset[str], radius: int) -> str | None:
    if obs.player_position is None:
        return None
    px, py = _center(obs.player_position.x, obs.player_position.y)
    for e in obs.entities:
        if e.entity_type in HARMLESS_ENTITIES or e.entity_id in seen:
            continue
        ex, ey = _center(e.position.x, e.position.y)
        if abs(ex - px) <= radius and abs(ey - py) <= radius:
            return e.entity_id
    return None


def _sighted(obs: Observation, seen: frozenset[str]) -> str | None:
    """A monster or plasma that was not in view when the skill started, anywhere on screen: the
    screen scrolled mid-move, or it came in from the side (level 3: a jump across the screen edge
    met the spider's plasma, unseen at take-off)."""
    return next((e.entity_id for e in obs.entities
                 if e.visible and e.entity_type not in HARMLESS_ENTITIES and e.entity_id not in seen), None)


def _incoming(obs: Observation, expected: frozenset[str], cfg: ExecutorConfig) -> str | None:
    """A threat predicted to touch Dave within ``interrupt_ticks`` if he stays put, other than
    one already predicted when the skill started (the choice took those into account). Only
    while he stands: a jump in the air cannot be changed."""
    if obs.player_state not in STANDING_STATES or not obs.grounded:
        return None
    for threat in threats(obs):
        if threat.entity_id not in expected:
            contact = time_to_contact(obs, cfg.threats, threat.entity_id)
            if contact is not None and threat_key(contact.what) not in expected:
                return f"{contact.what}@{contact.tick}"
    return None


def _interrupt(spec: SkillSpec, step: StepResult, seen: frozenset[str], cfg: ExecutorConfig,
               expected: frozenset[str] = frozenset()) -> str | None:
    obs = step.observation
    rules = spec.interrupt_on
    if "death" in rules and any(e.event_type == "death" for e in step.events):
        return "death"
    if "terminal" in rules and obs.terminal != "running":
        return f"terminal:{obs.terminal}"
    if "hazard_contact" in rules and obs.player_state == "burning":
        return "hazard_contact"
    if "new_hazard_nearby" in rules:
        entity = _new_hazard(obs, seen, cfg.hazard_radius_px)
        if entity is not None:
            return f"new_hazard_nearby:{entity}"
    if "threat_sighted" in rules:
        entity = _sighted(obs, seen)
        if entity is not None:
            return f"threat_sighted:{entity}"
    if "threat_incoming" in rules:
        threat = _incoming(obs, expected, cfg)
        if threat is not None:
            return f"threat_incoming:{threat}"
    return None


def execute(
    adapter: GameAdapter,
    candidate: SkillCandidate,
    skills: tuple[SkillSpec, ...],
    observation: Observation,
    cfg: ExecutorConfig,
) -> ExecutionResult:
    """Run ``candidate`` from ``observation`` (which must be the adapter's latest).

    Inputs are applied one frame per ``adapter.step`` call, so the adapter releases every
    key between frames and a skill can never leave a button held after it ends.
    """
    base = {"episode_id": observation.episode_id}
    result = ExecutionResult(
        candidate_id=candidate.candidate_id,
        skill=candidate.skill,
        outcome="completed",
        reason=None,
        frames=0,
        input_ticks=0,
        observation=observation,
    )
    rejected = revalidate(candidate, skills, observation)
    if rejected is not None:
        result.outcome, result.reason = "rejected", rejected
        result.events.append(_finished(result, base, observation.frame))
        return result

    spec = find_spec(skills, candidate.skill)
    result.events.append(
        Event(event_type="skill_started", frame=observation.frame,
              payload={"candidate_id": candidate.candidate_id, "skill": spec.name}, **base)
    )
    # Entities already visible, and the shots of monsters whose motion is known: those were
    # predicted when the skill was chosen, so their appearing does not stop it (level 4: a jump
    # chosen to dodge the swirl's next shot was stopped before take-off when the shot appeared).
    seen = frozenset(e.entity_id for e in observation.entities) | scripted_shots(observation)
    # Threats that would already reach a standing Dave soon: the choice took these into account.
    expected = imminent(observation, cfg.threats) if "threat_incoming" in spec.interrupt_on else frozenset()
    for index, phase in enumerate(spec.phases):
        buttons = frozenset(phase.buttons)
        satisfied = False
        ticks = 0
        while ticks < phase.budget:
            # While the screen scrolls the game moves nothing and ignores the keys: those ticks do
            # not count, or a fixed hold ends early (level 3: jump_right_5 across the screen edge
            # let go 16 ticks early and dropped into the fire short of the pillar).
            frozen = not screen_still(result.observation) and result.frozen_ticks < MAX_FROZEN_TICKS
            step = adapter.step(buttons, 1)
            if step.applied_buttons != buttons:
                raise RuntimeError(f"adapter applied {sorted(step.applied_buttons)}, expected {sorted(buttons)}")
            result.steps.append(step)
            result.events.extend(step.events)
            result.frames += step.frames_advanced
            if frozen:
                result.frozen_ticks += 1
            else:
                ticks += 1
                result.input_ticks += 1
            result.observation = step.observation
            reason = _interrupt(spec, step, seen, cfg, expected)
            if reason is not None:
                result.outcome, result.reason = "interrupted", reason
                result.events.append(_finished(result, base, step.observation.frame))
                return result
            if phase.until is not None and check(phase.until, step.observation):
                satisfied = True
                break
        if phase.until is not None and not satisfied:
            result.outcome, result.reason = "failed", f"phase{index}_timeout:{phase.until}"
            break
    assert result.input_ticks <= spec.max_frames
    result.events.append(_finished(result, base, result.observation.frame))
    return result


def _finished(result: ExecutionResult, base: dict, frame: int) -> Event:
    return Event(
        event_type="skill_finished",
        frame=frame,
        payload={
            "candidate_id": result.candidate_id,
            "skill": result.skill,
            "outcome": result.outcome,
            "reason": result.reason,
            "frames": result.frames,
            "input_ticks": result.input_ticks,
        },
        **base,
    )
