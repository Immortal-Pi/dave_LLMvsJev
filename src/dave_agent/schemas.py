"""Shared data contracts between adapters, memory, controllers and the runner.

Unavailable values are ``None`` and listed in ``unavailable_fields``; they are never
silently replaced with zero. Pixel and tile coordinates use distinct types.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

SCHEMA_VERSION = 1

Identifier = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")]
NonNegInt = Annotated[int, Field(ge=0)]


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class PixelPos(Contract):
    """Position in game pixels (deadly-dave: 16 px per tile)."""

    x: int
    y: int


class TilePos(Contract):
    """Position in tile-grid cells: column and row."""

    col: int
    row: int


class PixelVelocity(Contract):
    """Pixels per simulation frame."""

    dx: int
    dy: int


class Region(Contract):
    """Inclusive tile rectangle."""

    min: TilePos
    max: TilePos

    @model_validator(mode="after")
    def _ordered(self) -> Region:
        if self.min.col > self.max.col or self.min.row > self.max.row:
            raise ValueError(f"region min {self.min} must not exceed max {self.max}")
        return self

    def contains(self, pos: TilePos) -> bool:
        return self.min.col <= pos.col <= self.max.col and self.min.row <= pos.row <= self.max.row


TileKind = Literal["solid", "hazard", "collectible", "required_item", "exit", "climbable", "item"]


class ObservedTile(Contract):
    pos: TilePos
    kind: TileKind
    name: str


class Entity(Contract):
    entity_id: Identifier
    entity_type: str
    position: PixelPos
    velocity: PixelVelocity | None = None
    velocity_source: Literal["measured", "derived"] | None = None
    last_observed_frame: NonNegInt
    visible: bool
    source: Literal["adapter", "memory"]

    @model_validator(mode="after")
    def _velocity_source(self) -> Entity:
        if (self.velocity is None) != (self.velocity_source is None):
            raise ValueError("velocity and velocity_source must both be set or both be None")
        return self


# secret_exit: deadly-dave level 5 warp-down entrance, distinct from completing the level.
TerminalStatus = Literal["running", "level_complete", "game_over", "secret_exit"]
AdapterKind = Literal["fixture", "simulator", "dave"]

# Player movement state as the game reports it (deadly-dave dave.h DAVE_STATE_*).
# burning: Dave touched a hazard; inputs are ignored until death and respawn.
PlayerState = Literal[
    "standing", "walking", "jumping", "climbing", "freefalling", "jetpacking", "burning", "dead", "blinking"
]
# front: facing the camera (deadly-dave DAVE_DIRECTION_FRONT); the gun cannot fire.
Facing = Literal["left", "right", "front"]

# Observation fields that an adapter may be unable to provide.
OPTIONAL_OBSERVATION_FIELDS = frozenset(
    {"player_position", "player_velocity", "grounded", "player_state", "facing", "lives", "inventory", "score"}
)


class Observation(Contract):
    schema_version: Literal[1] = SCHEMA_VERSION
    adapter: AdapterKind
    build_id: str
    episode_id: Identifier
    level_id: Identifier
    frame: NonNegInt
    observation_id: NonNegInt
    player_position: PixelPos | None
    player_velocity: PixelVelocity | None
    player_velocity_source: Literal["measured", "derived"] | None = None
    grounded: bool | None
    player_state: PlayerState | None
    facing: Facing | None
    lives: NonNegInt | None
    inventory: dict[str, NonNegInt] | None
    score: NonNegInt | None
    tiles: tuple[ObservedTile, ...]
    entities: tuple[Entity, ...]
    region: Region
    terminal: TerminalStatus
    unavailable_fields: frozenset[str] = frozenset()

    @model_validator(mode="after")
    def _availability(self) -> Observation:
        unknown = self.unavailable_fields - OPTIONAL_OBSERVATION_FIELDS
        if unknown:
            raise ValueError(f"unavailable_fields contains non-optional fields: {sorted(unknown)}")
        for name in OPTIONAL_OBSERVATION_FIELDS:
            value = getattr(self, name)
            if name in self.unavailable_fields and value is not None:
                raise ValueError(f"{name} is marked unavailable but has a value")
            if name not in self.unavailable_fields and value is None:
                raise ValueError(f"{name} is None; list it in unavailable_fields instead of omitting it")
        if (self.player_velocity is None) != (self.player_velocity_source is None):
            raise ValueError("player_velocity and player_velocity_source must both be set or both be None")
        outside = [t.pos for t in self.tiles if not self.region.contains(t.pos)]
        if outside:
            raise ValueError(f"observed tiles outside the local region: {outside[:3]}")
        return self


GoalType = Literal["collect", "reach", "explore", "recover"]


class Goal(Contract):
    goal_id: Identifier
    goal_type: GoalType
    target_ref: str = Field(min_length=1, max_length=128)
    next_waypoint: TilePos | None = None
    success_predicate: str = Field(min_length=1, max_length=256)
    constraints: tuple[str, ...] = ()
    deadline_frame: NonNegInt | None = None
    rationale: str = Field(default="", max_length=500)
    source_observation_id: NonNegInt


ParamValue = int | str | bool


class SkillCandidate(Contract):
    """A bounded skill offered to a controller. Generated by trusted code only."""

    candidate_id: Identifier
    skill: Identifier
    description: str = Field(default="", max_length=200)
    parameters: dict[str, ParamValue] = Field(default_factory=dict)
    preconditions: tuple[str, ...] = ()
    max_frames: Annotated[int, Field(ge=1, le=600)]


class Decision(Contract):
    candidate_id: Identifier
    observation_id: NonNegInt
    goal_id: Identifier | None = None
    provider_score: float | None = None
    provider_score_meaning: str | None = None
    # fallback: no valid model answer (or a budget ran out), so the deterministic legal fallback
    # chose; never counted as a model decision. fallback_reason says why.
    fallback: bool = False
    fallback_reason: str | None = Field(default=None, max_length=128)
    # forced: exactly one legal candidate, so the runner chose it without a model call.
    forced: bool = False
    # sha256 prefix of the shared tactical request (models/tactical.py::context_digest): equal
    # digests mean two arms were shown identical context. None for forced decisions.
    context_digest: str | None = Field(default=None, max_length=64)

    @model_validator(mode="after")
    def _score_documented(self) -> Decision:
        if self.provider_score is not None and not self.provider_score_meaning:
            raise ValueError("provider_score requires provider_score_meaning describing its semantics")
        if self.fallback_reason is not None and not self.fallback:
            raise ValueError("fallback_reason is only set on fallback decisions")
        return self


EventType = Literal[
    "episode_start",
    "skill_started",
    "skill_finished",
    "moved",
    "item_collected",
    "death",
    "respawn",
    "level_complete",
    "game_over",
    "episode_truncated",
    "model_failure",
    # A tactical decision made by the deterministic fallback (payload: reason, candidate_id).
    "decision_fallback",
    # Derived by memory/detector.py from consecutive observations.
    "inventory_changed",
    "area_discovered",
    # Emitted by the goal manager (Phase 6).
    "goal_set",
    "goal_achieved",
    "goal_failed",
]


class Event(Contract):
    event_type: EventType
    episode_id: Identifier
    frame: NonNegInt
    entity_refs: tuple[str, ...] = ()
    location: TilePos | None = None
    evidence: tuple[str, ...] = ()
    certainty: Literal["observed", "derived", "unknown"] = "observed"
    payload: dict[str, Any] = Field(default_factory=dict)


class StepResult(Contract):
    observation: Observation
    frames_advanced: NonNegInt
    applied_buttons: frozenset[str]
    events: tuple[Event, ...] = ()


class ModelCallRecord(Contract):
    provider: str
    model: str
    purpose: Literal["planner", "tactical", "probe"]
    latency_ms: float | None
    retries: NonNegInt = 0
    status: Literal["ok", "error", "timeout", "invalid_output"]
    usage: dict[str, float] | None = None
    cost_usd: float | None = None
    cost_source: Literal["provider_reported", "estimated"] | None = None
    request_ref: str | None = None
    response_ref: str | None = None
    # Provider answer details kept as reported (e.g. Jev probabilities, confidence, dated model);
    # None when the provider returns nothing beyond the answer text.
    output: dict[str, Any] | None = None

    @model_validator(mode="after")
    def _cost_source(self) -> ModelCallRecord:
        if (self.cost_usd is None) != (self.cost_source is None):
            raise ValueError("cost_usd and cost_source must both be set or both be None")
        return self


Support = Literal["supported", "unsupported", "planned"]


class AdapterCapabilities(Contract):
    adapter: AdapterKind
    build_id: str
    reset: Support
    observe: Support
    step_exact_frames: Support
    snapshots: Support
    headless: Support
    buttons: frozenset[str]
    frames_per_second: float | None
    notes: tuple[str, ...] = ()


class SnapshotRef(Contract):
    snapshot_id: Identifier
    adapter: AdapterKind
    frame: NonNegInt
    observation_id: NonNegInt


class InvalidDecisionError(ValueError):
    """A controller decision cannot be executed."""


def validate_decision(
    decision: Decision, candidates: list[SkillCandidate], observation: Observation
) -> SkillCandidate:
    """Return the candidate a decision refers to, or raise an actionable error."""
    if decision.observation_id != observation.observation_id:
        raise InvalidDecisionError(
            f"decision targets observation {decision.observation_id} but the latest is "
            f"{observation.observation_id}; re-decide on the current observation"
        )
    by_id = {c.candidate_id: c for c in candidates}
    if decision.candidate_id not in by_id:
        raise InvalidDecisionError(
            f"candidate_id {decision.candidate_id!r} is not offered; allowed: {sorted(by_id)}"
        )
    return by_id[decision.candidate_id]


def check_buttons(buttons: frozenset[str], supported: frozenset[str]) -> None:
    """Reject any button the adapter does not support."""
    bad = sorted(buttons - supported)
    if bad:
        raise ValueError(f"unsupported buttons {bad}; supported: {sorted(supported)}")
