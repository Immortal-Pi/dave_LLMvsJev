import pytest
from pydantic import ValidationError

from dave_agent.schemas import (
    Decision,
    Entity,
    InvalidDecisionError,
    ModelCallRecord,
    PixelPos,
    Region,
    SkillCandidate,
    TilePos,
    validate_decision,
)


def _candidates():
    return [
        SkillCandidate(candidate_id="c0_move_right", skill="move_right", max_frames=1),
        SkillCandidate(candidate_id="c1_wait", skill="wait", max_frames=1),
    ]


def test_validate_decision_returns_offered_candidate(adapter):
    obs = adapter.reset("fixture_l1", 0)
    decision = Decision(candidate_id="c1_wait", observation_id=obs.observation_id)
    assert validate_decision(decision, _candidates(), obs).skill == "wait"


def test_unoffered_candidate_rejected_with_allowed_list(adapter):
    obs = adapter.reset("fixture_l1", 0)
    decision = Decision(candidate_id="c9_fly", observation_id=obs.observation_id)
    with pytest.raises(InvalidDecisionError, match="allowed: \\['c0_move_right', 'c1_wait'\\]"):
        validate_decision(decision, _candidates(), obs)


def test_stale_decision_rejected(adapter):
    obs = adapter.reset("fixture_l1", 0)
    newer = adapter.observe()
    decision = Decision(candidate_id="c1_wait", observation_id=obs.observation_id)
    with pytest.raises(InvalidDecisionError, match="re-decide"):
        validate_decision(decision, _candidates(), newer)


def test_unknown_fields_rejected():
    with pytest.raises(ValidationError, match="extra"):
        Decision(candidate_id="c0", observation_id=1, confidence=0.9)


def test_provider_score_requires_documented_meaning():
    with pytest.raises(ValidationError, match="provider_score_meaning"):
        Decision(candidate_id="c0", observation_id=1, provider_score=0.9)


def test_bad_identifier_rejected():
    with pytest.raises(ValidationError):
        SkillCandidate(candidate_id="rm -rf /", skill="wait", max_frames=1)


def test_skill_duration_bounded():
    with pytest.raises(ValidationError):
        SkillCandidate(candidate_id="c0", skill="wait", max_frames=10_000)


def test_unavailable_field_must_be_null_and_listed(adapter):
    obs = adapter.reset("fixture_l1", 0)
    data = obs.model_dump()
    data["lives"] = None
    with pytest.raises(ValidationError, match="list it in unavailable_fields"):
        type(obs).model_validate(data)
    data["unavailable_fields"] = {"lives"}
    assert type(obs).model_validate(data).lives is None
    data["lives"] = 0
    with pytest.raises(ValidationError, match="marked unavailable"):
        type(obs).model_validate(data)


def test_pixel_and_tile_types_distinct():
    with pytest.raises(ValidationError):
        Region(min=PixelPos(x=0, y=0), max=TilePos(col=1, row=1))


def test_region_order_validated():
    with pytest.raises(ValidationError, match="must not exceed"):
        Region(min=TilePos(col=5, row=0), max=TilePos(col=1, row=0))


def test_entity_velocity_needs_source():
    with pytest.raises(ValidationError, match="velocity_source"):
        Entity(
            entity_id="m1", entity_type="spider", position=PixelPos(x=0, y=0),
            velocity={"dx": 1, "dy": 0}, last_observed_frame=0, visible=True, source="adapter",
        )


def test_cost_requires_source():
    with pytest.raises(ValidationError, match="cost_source"):
        ModelCallRecord(provider="x", model="y", purpose="tactical", latency_ms=1.0, status="ok", cost_usd=0.01)
