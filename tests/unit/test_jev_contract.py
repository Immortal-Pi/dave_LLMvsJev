"""Contract-shape tests against the sanitized live response captured by scripts/probe_jev.py."""

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from dave_agent.models.jev import JevResponse, parse_choice

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "jev" / "choice_response.json"


@pytest.fixture
def captured():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def test_fixture_has_no_credentials(captured):
    text = json.dumps(captured).lower()
    assert "authorization" not in text and "bearer" not in text and "sk-or-" not in text


def test_live_fixture_parses(captured):
    allowed = set(captured["request"]["questions"]["action"]["criteria"])
    answer = parse_choice(captured["response"], "action", allowed)
    assert answer.choice in allowed
    assert set(answer.probabilities) == allowed
    assert 0.0 <= answer.confidence <= 1.0


def test_usage_and_cost_preserved(captured):
    parsed = JevResponse.model_validate(captured["response"])
    assert parsed.usage is not None and parsed.usage.cost is not None
    assert parsed.model.startswith("typesafe/jev-1.13")


def test_choice_outside_candidates_rejected(captured):
    with pytest.raises(ValueError, match="not an offered candidate"):
        parse_choice(captured["response"], "action", {"c3_wait"})


def test_missing_question_rejected(captured):
    with pytest.raises(ValueError, match="no answer"):
        parse_choice(captured["response"], "other", {"x"})


def test_absent_optional_fields_stay_none():
    parsed = JevResponse.model_validate({"model": "m", "answers": {"a": {"type": "choice", "choice": "x"}}})
    assert parsed.usage is None
    assert parsed.answers["a"].probabilities is None and parsed.answers["a"].confidence is None


def test_malformed_response_rejected():
    with pytest.raises(ValidationError):
        JevResponse.model_validate({"answers": {}})
