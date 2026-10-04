"""Model-backed tactical control: the shared request, output validation, retry, fallback and budgets.

Every model-backed tactical arm (LLM and Jev) goes through ``ModelController``:
it builds one ``TacticalRequest`` from the observation, the active goal, the offered candidates
and the working-memory context; asks the model for one candidate id; validates it; retries
invalid or failed answers up to ``max_retries`` times; then falls back to a documented
deterministic legal choice. A fallback is never a model decision (``Decision.fallback``).
Per-episode call, token and cost budgets are enforced before every call.
"""

from __future__ import annotations

import hashlib
import json
import random
from dataclasses import dataclass
from typing import Any, Protocol

from pydantic import Field, ValidationError

from dave_agent.config import TacticalConfig
from dave_agent.memory.working import MemoryContext, player_tile
from dave_agent.schemas import Contract, Decision, Goal, Identifier, ModelCallRecord, NonNegInt, Observation, \
    SkillCandidate

# One character per observed tile kind; entities and the player are drawn over tiles.
TILE_CHARS = {"solid": "#", "hazard": "X", "collectible": "$", "required_item": "T", "exit": "D",
              "climbable": "|", "item": "I"}
PLAYER_CHAR, MONSTER_CHAR, SHOT_CHAR, EMPTY_CHAR = "@", "M", "*", "."
SHOT_TYPES = frozenset({"plasma", "bullet"})
GRID_LEGEND = ("# solid, X hazard (touching it sets Dave burning), $ loot, T trophy, D door, | climbable, "
               "I gun/jetpack, M monster, * plasma or bullet, @ Dave, . empty")

# Provider-neutral text shared by every model-backed arm (planner and tactical, LLM and Jev).
# Only rules verified in docs/feasibility.md section 3 (deadly-dave source) are stated here.
GAME_RULES = """Game rules (verified):
- Dave walks left/right, jumps, and can fire the gun only after collecting it. There is no ducking and no health bar: Dave has lives.
- The level is completed by touching the door while holding the trophy. Touching the door without the trophy does nothing.
- Fire, water and vines set Dave burning; touching a monster or its plasma does too. Burning ends in death, then Dave respawns at the level start. The game is over at 0 lives.
- Loot (gems etc.) only adds score. The gun lets Dave shoot monsters. Falling off the bottom of the screen wraps to the top; it is not a death.
- Coordinates are tile (col, row); row 0 is the top. Only the local view and what was seen earlier this episode are known."""
INPUT_GUIDE = f"""- `player`: Dave's tile, pixel position (16 px per tile), movement state, facing and velocity.
- `view.rows`: the visible tiles, one string per row starting at tile `view.origin` (col, row). Legend: {GRID_LEGEND}.
- `entities`: visible monsters and shots with their tiles and velocities (pixels per frame).
- `goal`: the current objective from the planner, with `waypoint` (the tile to head for now) and `waypoint_offset` (tiles from Dave: +col right, +row down).
- `progress` and `recent`: how the last skills went (outcomes, interruptions, deaths). `moved_px` is Dave's displacement during a skill in pixels; [0, 0] means he did not move (blocked by a wall or by the tile above).
- `candidates`: the only skills you may choose, each with a description and its frame limit."""
TACTICAL_TASK = ("Choose the skill that best moves Dave toward the waypoint without touching hazards, monsters or "
                 "plasma. A candidate whose description starts with `route:` carries out the next move of the "
                 "planned route (or walks to its take-off): prefer it unless it is dangerous. Avoid repeating a "
                 "skill that keeps failing.")


class TacticalRequest(Contract):
    """Everything a tactical model sees. Built by one function for every arm."""

    episode_id: Identifier
    level_id: Identifier
    frame: NonNegInt
    observation_id: NonNegInt
    player: dict[str, Any]
    lives: int | None
    inventory: dict[str, int] | None
    score: int | None
    view: dict[str, Any]  # origin tile and one string per row of the local view (GRID_LEGEND)
    entities: tuple[dict[str, Any], ...]
    goal: dict[str, Any] | None
    progress: dict[str, Any]
    recent: tuple[dict[str, Any], ...]  # recent executed skills, oldest first
    candidates: tuple[dict[str, Any], ...]

    @property
    def candidate_ids(self) -> tuple[str, ...]:
        return tuple(c["id"] for c in self.candidates)


def _tile(pos) -> list[int] | None:
    if pos is None:
        return None
    t = player_tile(pos)
    return [t.col, t.row]


def view_rows(observation: Observation) -> tuple[str, ...]:
    region = observation.region
    width, height = region.max.col - region.min.col + 1, region.max.row - region.min.row + 1
    grid = [[EMPTY_CHAR] * width for _ in range(height)]

    def put(col: int, row: int, char: str) -> None:
        if region.min.col <= col <= region.max.col and region.min.row <= row <= region.max.row:
            grid[row - region.min.row][col - region.min.col] = char

    for t in observation.tiles:
        put(t.pos.col, t.pos.row, TILE_CHARS[t.kind])
    for e in observation.entities:
        if e.visible:
            col, row = _tile(e.position)
            put(col, row, SHOT_CHAR if e.entity_type in SHOT_TYPES else MONSTER_CHAR)
    if observation.player_position is not None:
        col, row = _tile(observation.player_position)
        put(col, row, PLAYER_CHAR)
    return tuple("".join(r) for r in grid)


def tactical_request(observation: Observation, goal: Goal | None, candidates: list[SkillCandidate],
                     memory: MemoryContext) -> TacticalRequest:
    obs = observation
    here = _tile(obs.player_position)
    goal_view = None
    if goal is not None:
        waypoint = goal.next_waypoint
        goal_view = {
            "goal_type": goal.goal_type, "target": goal.target_ref, "success": goal.success_predicate,
            "waypoint": [waypoint.col, waypoint.row] if waypoint else None,
            # Tiles from Dave to the waypoint: +col is right, +row is down.
            "waypoint_offset": [waypoint.col - here[0], waypoint.row - here[1]] if waypoint and here else None,
            "constraints": [c for c in goal.constraints if not c.startswith("target:")],
            "frames_left": None if goal.deadline_frame is None else goal.deadline_frame - obs.frame,
        }
    return TacticalRequest(
        episode_id=obs.episode_id, level_id=obs.level_id, frame=obs.frame, observation_id=obs.observation_id,
        player={"tile": here, "px": None if obs.player_position is None else [obs.player_position.x,
                                                                              obs.player_position.y],
                "state": obs.player_state, "grounded": obs.grounded, "facing": obs.facing,
                "velocity": None if memory.velocity is None else [memory.velocity.dx, memory.velocity.dy]},
        lives=obs.lives, inventory=obs.inventory, score=obs.score,
        view={"origin": [obs.region.min.col, obs.region.min.row], "rows": list(view_rows(obs))},
        entities=tuple({"type": e.entity_type, "tile": _tile(e.position),
                        "velocity": None if e.velocity is None else [e.velocity.dx, e.velocity.dy]}
                       for e in obs.entities if e.visible),
        goal=goal_view,
        progress=memory.progress.model_dump(),
        recent=tuple({"skill": e.skill, "outcome": e.outcome, "reason": e.reason, "events": list(e.events),
                      "end_tile": [e.end_tile.col, e.end_tile.row] if e.end_tile else None,
                      "moved_px": e.moved_px}
                     for e in memory.recent),
        candidates=tuple({"id": c.candidate_id, "description": c.description, "max_frames": c.max_frames}
                         for c in candidates),
    )


def context_digest(request: TacticalRequest) -> str:
    """Digest of everything the model is shown except the episode id. Arms that see the same
    state, goal, memory and candidates get the same digest (logged per decision)."""
    canonical = json.dumps(request.model_dump(mode="json", exclude={"episode_id"}), sort_keys=True,
                           separators=(",", ":"))
    return "sha256:" + hashlib.sha256(canonical.encode()).hexdigest()[:16]


class TacticalChoice(Contract):
    candidate_id: str = Field(min_length=1, max_length=128)
    provider_score: float | None = None
    provider_score_meaning: str | None = None


class TacticalOutputError(ValueError):
    """The model's answer is not exactly one offered candidate id."""


def parse_tactical(text: str | None, allowed: tuple[str, ...]) -> TacticalChoice:
    if not text:
        raise TacticalOutputError("empty output")
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise TacticalOutputError(f"output is not JSON: {exc.msg}") from exc
    if not isinstance(data, dict) or set(data) != {"candidate_id"}:
        raise TacticalOutputError('output must be exactly {"candidate_id": "<offered id>"}')
    try:
        choice = TacticalChoice.model_validate(data)
    except ValidationError as exc:
        raise TacticalOutputError(f"invalid candidate_id: {exc.errors()[0]['msg']}") from exc
    if choice.candidate_id not in allowed:
        raise TacticalOutputError(f"candidate_id {choice.candidate_id!r} is not offered; "
                                  f"choose one of {list(allowed)}")
    return choice


class TacticalModel(Protocol):
    provider: str
    model: str

    def propose(self, request: TacticalRequest, feedback: str | None = None) -> tuple[str | None, ModelCallRecord]:
        """Raw output (None when the call itself failed) and the call record. ``feedback`` says
        why the previous attempt was rejected."""
        ...

    def parse(self, text: str | None, allowed: tuple[str, ...]) -> TacticalChoice:
        """Validate raw output; raise TacticalOutputError when it is not one allowed id."""
        ...


def _record(provider: str, model: str, status: str = "ok") -> ModelCallRecord:
    return ModelCallRecord(provider=provider, model=model, purpose="tactical", latency_ms=0.0, status=status)


class SeededMockModel:
    """Offline mock LLM: seeded uniform choice over the offered candidates. Same choices, in
    the same order, as ``SeededMockController`` with the same seed."""

    provider = "mock"

    def __init__(self, seed: int, label: str) -> None:
        self.model = label
        self._rng = random.Random(seed)

    def propose(self, request: TacticalRequest, feedback: str | None = None) -> tuple[str | None, ModelCallRecord]:
        return json.dumps({"candidate_id": self._rng.choice(request.candidate_ids)}), _record(self.provider,
                                                                                              self.model)

    def parse(self, text: str | None, allowed: tuple[str, ...]) -> TacticalChoice:
        return parse_tactical(text, allowed)


TIMEOUT = object()  # ScriptedTacticalModel output: a timed-out call


class ScriptedTacticalModel:
    """Test model: returns the given outputs in order (a str, None for a failed call, TIMEOUT,
    or a callable receiving the request), then the first offered candidate."""

    provider, model = "scripted", "scripted-tactical"

    def __init__(self, outputs: list, usage: dict[str, float] | None = None,
                 cost_usd: float | None = None) -> None:
        self.outputs, self.usage, self.cost_usd = list(outputs), usage, cost_usd
        self.requests: list[TacticalRequest] = []
        self.feedback: list[str | None] = []

    def propose(self, request: TacticalRequest, feedback: str | None = None) -> tuple[str | None, ModelCallRecord]:
        self.requests.append(request)
        self.feedback.append(feedback)
        out = self.outputs.pop(0) if self.outputs else json.dumps({"candidate_id": request.candidate_ids[0]})
        if callable(out):
            out = out(request)
        status = "timeout" if out is TIMEOUT else "error" if out is None else "ok"
        record = ModelCallRecord(provider=self.provider, model=self.model, purpose="tactical", latency_ms=0.0,
                                 status=status, usage=self.usage, cost_usd=self.cost_usd,
                                 cost_source=None if self.cost_usd is None else "provider_reported")
        return (out if status == "ok" else None), record

    def parse(self, text: str | None, allowed: tuple[str, ...]) -> TacticalChoice:
        return parse_tactical(text, allowed)


class BudgetExhausted(Exception):
    """A per-episode tactical budget ran out and ``on_budget_exhausted`` is ``terminate``.
    ``calls`` are the calls already made for the interrupted decision, so they can be logged."""

    def __init__(self, budget: str, calls: tuple[ModelCallRecord, ...] = ()) -> None:
        super().__init__(f"tactical budget exhausted: {budget}")
        self.budget, self.calls = budget, calls


@dataclass
class BudgetUse:
    calls: int = 0
    tokens: float = 0.0
    cost_usd: float = 0.0
    cost_known: bool = False


def fallback_candidate(candidates: list[SkillCandidate], preferred: tuple[str, ...]) -> SkillCandidate:
    """The documented deterministic legal fallback: the first offered skill named in
    ``tactical.fallback_skills``, else the first offered candidate (catalog order)."""
    by_skill = {c.skill: c for c in candidates}
    return next((by_skill[name] for name in preferred if name in by_skill), candidates[0])


class ModelController:
    """Wraps a ``TacticalModel`` as a ``TacticalController`` with the shared retry, fallback and
    budget policy. Counters reset when a new episode id is seen."""

    def __init__(self, model: TacticalModel, cfg: TacticalConfig, max_retries: int) -> None:
        self.inner, self.cfg, self.max_retries = model, cfg, max_retries
        self.provider, self.model = model.provider, model.model
        self.use = BudgetUse()
        self._episode: str | None = None

    def exhausted(self) -> str | None:
        """The name of the first budget that is used up, or None."""
        cfg, use = self.cfg, self.use
        if use.calls >= cfg.max_calls_per_episode:
            return "tactical_calls"
        if cfg.max_tokens_per_episode is not None and use.tokens >= cfg.max_tokens_per_episode:
            return "tactical_tokens"
        if cfg.max_cost_usd_per_episode is not None and use.cost_known \
                and use.cost_usd >= cfg.max_cost_usd_per_episode:
            return "tactical_cost"
        return None

    def _count(self, call: ModelCallRecord) -> None:
        self.use.calls += 1
        if call.usage:
            self.use.tokens += call.usage.get("total_tokens", 0.0)
        if call.cost_usd is not None:
            self.use.cost_usd += call.cost_usd
            self.use.cost_known = True

    def decide(self, observation: Observation, goal: Goal | None, candidates: list[SkillCandidate],
               memory: MemoryContext) -> tuple[Decision, tuple[ModelCallRecord, ...]]:
        if observation.episode_id != self._episode:
            self._episode, self.use = observation.episode_id, BudgetUse()
        request = tactical_request(observation, goal, candidates, memory)
        allowed = request.candidate_ids
        goal_id = goal.goal_id if goal else None
        digest = context_digest(request)
        calls: list[ModelCallRecord] = []
        feedback, reason = None, None
        for _ in range(self.max_retries + 1):
            budget = self.exhausted()
            if budget is not None:
                if self.cfg.on_budget_exhausted == "terminate":
                    raise BudgetExhausted(budget, tuple(calls))
                reason = f"budget:{budget}"
                break
            text, call = self.inner.propose(request, feedback)
            self._count(call)
            if call.status == "ok":
                try:
                    choice = self.inner.parse(text, allowed)
                except TacticalOutputError as exc:
                    call = call.model_copy(update={"status": "invalid_output"})
                    feedback, reason = str(exc), "invalid_output"
                else:
                    calls.append(call)
                    return Decision(candidate_id=choice.candidate_id, observation_id=observation.observation_id,
                                    goal_id=goal_id, provider_score=choice.provider_score,
                                    provider_score_meaning=choice.provider_score_meaning,
                                    context_digest=digest), tuple(calls)
            else:
                feedback, reason = None, f"call_{call.status}"
            calls.append(call)
        chosen = fallback_candidate(candidates, self.cfg.fallback_skills)
        return Decision(candidate_id=chosen.candidate_id, observation_id=observation.observation_id,
                        goal_id=goal_id, fallback=True, fallback_reason=reason, context_digest=digest), tuple(calls)
