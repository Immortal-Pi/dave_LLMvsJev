"""Load and validate the YAML configuration set.

``experiments.yaml`` is the entry point; ``environment.yaml``, ``models.yaml`` and
``skills.yaml`` are read from the same directory.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, NonNegativeInt, ValidationError, model_validator

PositiveInt = Annotated[int, Field(gt=0)]


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class EnvironmentConfig(Strict):
    adapter: Literal["fixture", "dave"]
    levels_dir: Path
    dave_dir: Path
    observation_policy: Literal["local_observed", "oracle"]
    execution_mode: Literal["paused_step", "real_time"]
    decision_frames: PositiveInt


ReasoningEffort = Literal["none", "minimal", "low", "medium", "high"]


class PriceConfig(Strict):
    """User-supplied token prices for a provider that reports no cost. Costs computed from it are
    labeled ``estimated`` and carry ``source`` and ``as_of``; nothing here is a measured charge."""

    input_per_mtok: Annotated[float, Field(ge=0)]   # USD per 1M prompt tokens
    output_per_mtok: Annotated[float, Field(ge=0)]  # USD per 1M completion tokens (reasoning included)
    source: str = Field(min_length=1)
    as_of: str = Field(min_length=1)


class AzureModelConfig(Strict):
    provider: Literal["azure_openai"]
    deployment_env: str
    max_completion_tokens: PositiveInt  # includes reasoning tokens
    reasoning_effort: ReasoningEffort | None  # None: omit the parameter
    price: PriceConfig | None = None  # None: cost unknown (never assumed)


class JevModelConfig(Strict):
    provider: Literal["openrouter_decisions"]
    endpoint: str
    model_id: str
    api_key_env: str


class ModelsConfig(Strict):
    planner: AzureModelConfig
    tactical_llm: AzureModelConfig
    jev: JevModelConfig
    timeout_seconds: Annotated[float, Field(gt=0, le=120)]
    max_retries: Annotated[int, Field(ge=0, le=5)]


MAX_SKILL_FRAMES = 600
InterruptReason = Literal["death", "terminal", "hazard_contact", "new_hazard_nearby", "threat_incoming"]


class SkillPhase(Strict):
    """Hold ``buttons`` for ``ticks`` frames, or until predicate ``until`` holds (at most ``max_ticks``).

    Buttons are released when the phase ends; the next phase states its own buttons.
    """

    buttons: tuple[str, ...] = ()
    ticks: Annotated[int, Field(ge=1, le=MAX_SKILL_FRAMES)] | None = None
    until: str | None = None
    max_ticks: Annotated[int, Field(ge=1, le=MAX_SKILL_FRAMES)] | None = None

    @model_validator(mode="after")
    def _one_mode(self) -> SkillPhase:
        fixed = self.ticks is not None and self.until is None and self.max_ticks is None
        conditional = self.ticks is None and self.until is not None and self.max_ticks is not None
        if not (fixed or conditional):
            raise ValueError("a phase needs either 'ticks' or both 'until' and 'max_ticks'")
        return self

    @property
    def budget(self) -> int:
        return self.ticks if self.ticks is not None else self.max_ticks  # type: ignore[return-value]


class SkillSpec(Strict):
    name: Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]*$")]
    kind: Literal["single", "macro"]
    description: str = ""
    phases: Annotated[tuple[SkillPhase, ...], Field(min_length=1)]
    preconditions: tuple[str, ...] = ()
    interrupt_on: tuple[InterruptReason, ...] = ("death", "terminal")

    @property
    def max_frames(self) -> int:
        """Hard cap: the sum of every phase's tick budget."""
        return sum(p.budget for p in self.phases)

    @property
    def buttons(self) -> frozenset[str]:
        return frozenset(b for p in self.phases for b in p.buttons)

    @model_validator(mode="after")
    def _bounded(self) -> SkillSpec:
        if self.max_frames > MAX_SKILL_FRAMES:
            raise ValueError(f"phases total {self.max_frames} frames; the cap is {MAX_SKILL_FRAMES}")
        if self.kind == "single" and len(self.phases) != 1:
            raise ValueError("a 'single' skill has exactly one phase; use kind 'macro'")
        return self


class ThreatConfig(Strict):
    """Threat prediction (control/threats.py): plasma flies straight at 2 px/tick until a brick;
    monsters are extrapolated linearly, so their box grows with the look-ahead."""

    horizon_ticks: PositiveInt = 48  # look-ahead for monsters (plasma: the whole skill)
    margin_px: NonNegativeInt = 2  # added on every side of every box
    monster_growth_ticks: PositiveInt = 8  # a monster's box grows 1 px per this many ticks ahead
    interrupt_ticks: PositiveInt = 16  # threat_incoming: predicted contact within this many ticks
    mask: bool = True  # drop candidates predicted to touch a threat or hazard (unless all do)


class ExecutorConfig(Strict):
    # new_hazard_nearby: a monster or plasma that was not visible when the skill started
    # comes within this many pixels (centre distance per axis) of Dave.
    hazard_radius_px: PositiveInt
    threats: ThreatConfig = Field(default_factory=ThreatConfig)


class ReachConfig(Strict):
    """Measured movement for estimated reachability (control/reach.py)."""

    arc_px: tuple[NonNegativeInt, ...] = Field(min_length=2)  # rise above the start by tick of a jump
    air_px_per_tick: PositiveInt  # sideways speed while a direction is held in the air
    walk_px_per_3_ticks: PositiveInt  # walking speed
    fall_px_per_tick: PositiveInt  # free fall after the arc or off an edge
    short_hold_ticks: PositiveInt  # how long the *_short jumps hold the direction
    body_px: tuple[NonNegativeInt, NonNegativeInt]  # x offsets of the wall-collision box's left/right edge
    foot_px: tuple[NonNegativeInt, NonNegativeInt]  # x offsets of the two points that need ground under them
    head_px: tuple[NonNegativeInt, NonNegativeInt] = (4, 9)  # x offsets of the ceiling test points (dave.c)


class SkillsConfig(Strict):
    executor: ExecutorConfig
    catalogs: dict[Literal["fixture", "dave"], tuple[SkillSpec, ...]]
    # Per adapter; an adapter without an entry gets no reachability waypoints.
    reach: dict[Literal["fixture", "dave"], ReachConfig] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _unique(self) -> SkillsConfig:
        for adapter, skills in self.catalogs.items():
            names = [s.name for s in skills]
            dupes = sorted({n for n in names if names.count(n) > 1})
            if dupes:
                raise ValueError(f"duplicate skill names in catalogs.{adapter}: {dupes}")
        return self

    def for_adapter(self, adapter: str) -> tuple[SkillSpec, ...]:
        if adapter not in self.catalogs:
            raise ValueError(f"no skill catalog for adapter {adapter!r}; have {sorted(self.catalogs)}")
        return self.catalogs[adapter]  # type: ignore[index]


class MemoryConfig(Strict):
    # Working memory: the recent-history deque covers this many simulation frames and never
    # holds more than max_history_entries; controllers see at most context_entries of it.
    recent_history_frames: PositiveInt
    max_history_entries: PositiveInt
    context_entries: PositiveInt
    episode_store: Path
    store_batch_size: PositiveInt  # rows buffered before one SQLite transaction
    graph_enabled: bool
    graph_checkpoint: Path | None
    graph_updates: bool

    @model_validator(mode="after")
    def _context_fits(self) -> MemoryConfig:
        if self.context_entries > self.max_history_entries:
            raise ValueError("context_entries must not exceed max_history_entries")
        return self


class RouteWeights(Strict):
    time: Annotated[float, Field(ge=0)]
    risk: Annotated[float, Field(ge=0)]
    uncertainty: Annotated[float, Field(ge=0)]


class GraphConfig(Strict):
    """Learned-graph route cost (docs/graph.md). Starting points, not calibrated values."""

    weights: RouteWeights
    reference_frames: PositiveInt
    p_min: Annotated[float, Field(gt=0, lt=1)]
    p_max: Annotated[float, Field(gt=0, lt=1)]
    evidence_per_item: PositiveInt

    @model_validator(mode="after")
    def _clip_ordered(self) -> GraphConfig:
        if self.p_min >= self.p_max:
            raise ValueError("p_min must be below p_max")
        return self


class PlanningConfig(Strict):
    """Goal manager and planner triggers (docs/planner.md). Starting points, not calibrated values."""

    no_progress_frames: PositiveInt
    repeated_skill_failures: PositiveInt
    max_calls_per_episode: PositiveInt
    # Soft triggers (death, stuck, repeated failures, inventory, invalid target or route) wait at
    # least this many frames after the previous planner call. Goal-ended triggers do not.
    min_frames_between_calls: Annotated[int, Field(ge=0)]
    goal_timeout_frames: PositiveInt  # a goal not achieved by then expires
    recent_events: PositiveInt  # recent history entries shown to the planner
    nearest_collectibles: Annotated[int, Field(ge=0)]  # score items offered as collect goals


class TacticalConfig(Strict):
    """Shared policy for model-backed tactical controllers (docs/tactical.md)."""

    # Per-episode budgets, checked before every tactical call. Tokens and cost count only
    # when the provider reports them; null disables that budget.
    max_calls_per_episode: PositiveInt
    max_tokens_per_episode: PositiveInt | None
    max_cost_usd_per_episode: Annotated[float, Field(gt=0)] | None
    # terminate: end the episode as truncated (budget:<name>); fallback: keep playing with
    # deterministic fallback decisions and no further calls.
    on_budget_exhausted: Literal["terminate", "fallback"]
    # Deterministic legal fallback: the first offered skill in this list, else the first offered candidate.
    fallback_skills: tuple[str, ...]


class BenchmarkConfig(Strict):
    pilot_trials: PositiveInt
    initial_trials: PositiveInt
    max_episode_frames: PositiveInt
    max_episode_wall_seconds: PositiveInt
    paid_run_budget_usd: Annotated[float, Field(ge=0)]
    randomize_arm_order: bool
    seed: NonNegativeInt = 0  # base seed: trial k uses seed + k; also seeds arm order and the bootstrap
    confidence: Annotated[float, Field(gt=0, lt=1)] = 0.95
    bootstrap_samples: PositiveInt = 2000


class ArmConfig(Strict):
    planner: Literal["llm", "fixed"]
    tactical: Literal["llm", "jev", "deterministic"]
    graph_enabled: bool


class AppConfig(Strict):
    schema_version: Literal[1]
    scenario: str
    environment: EnvironmentConfig
    models: ModelsConfig
    skills: SkillsConfig
    memory: MemoryConfig
    graph: GraphConfig
    planning: PlanningConfig
    tactical: TacticalConfig
    benchmark: BenchmarkConfig
    arms: dict[str, ArmConfig]

    @model_validator(mode="after")
    def _known_predicates(self) -> AppConfig:
        from dave_agent.control.predicates import PREDICATES  # local: control imports config

        for adapter, skills in self.skills.catalogs.items():
            for spec in skills:
                names = set(spec.preconditions) | {p.until for p in spec.phases if p.until}
                unknown = sorted(names - PREDICATES.keys())
                if unknown:
                    raise ValueError(
                        f"skills.catalogs.{adapter}.{spec.name}: unknown predicates {unknown}; "
                        f"known: {sorted(PREDICATES)}"
                    )
        return self


class ConfigError(ValueError):
    """Configuration is missing or invalid; the message says what to fix."""


def _read_yaml(path: Path) -> dict:
    if not path.exists():
        raise ConfigError(f"config file not found: {path}")
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path}: invalid YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"{path}: top level must be a mapping")
    return data


def load_config(experiments_path: Path | str) -> AppConfig:
    experiments_path = Path(experiments_path)
    root = experiments_path.parent
    merged: dict = {}
    for name in ("environment.yaml", "models.yaml", "skills.yaml"):
        part = _read_yaml(root / name)
        overlap = merged.keys() & part.keys()
        if overlap:
            raise ConfigError(f"{root / name}: keys {sorted(overlap)} already defined elsewhere")
        merged.update(part)
    experiments = _read_yaml(experiments_path)
    overlap = merged.keys() & experiments.keys()
    if overlap:
        raise ConfigError(f"{experiments_path}: keys {sorted(overlap)} belong in another config file")
    merged.update(experiments)
    try:
        config = AppConfig.model_validate(merged)
    except ValidationError as exc:
        lines = [f"invalid configuration under {root}:"]
        for err in exc.errors():
            location = ".".join(str(p) for p in err["loc"])
            lines.append(f"  - {location}: {err['msg']}")
        raise ConfigError("\n".join(lines)) from exc
    # Resolve relative paths against the project root (parent of configs/).
    project_root = root.parent
    env = config.environment
    env = env.model_copy(
        update={
            name: project_root / getattr(env, name)
            for name in ("levels_dir", "dave_dir")
            if not getattr(env, name).is_absolute()
        }
    )
    memory = config.memory
    memory = memory.model_copy(
        update={
            name: project_root / getattr(memory, name)
            for name in ("episode_store", "graph_checkpoint")
            if getattr(memory, name) is not None and not getattr(memory, name).is_absolute()
        }
    )
    return config.model_copy(update={"environment": env, "memory": memory})
