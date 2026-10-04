"""Phase 8: the Jev tactical model against a mocked transport (no network), the sanitized live
response fixture, and parity of what Jev and the LLM are shown."""

import json
import logging
from pathlib import Path

import httpx
import pytest

from dave_agent.adapters.fixture import FixtureAdapter
from dave_agent.config import ConfigError
from dave_agent.control.skills import generate_candidates
from dave_agent.memory.working import WorkingMemory
from dave_agent.models import jev
from dave_agent.models.azure import AzureChatClient, AzureSettings, AzureTacticalModel
from dave_agent.models.jev import SCORE_MEANING, JevClient, JevSettings, JevTacticalModel
from dave_agent.models.tactical import ModelController, context_digest, tactical_request

from ..conftest import LEVELS

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "jev"
KEY = "sk-or-test-0000"
SETTINGS = JevSettings(endpoint="https://example.invalid/api/alpha/decisions", model_id="typesafe/jev-1.13",
                       api_key=KEY)


def start(config):
    adapter = FixtureAdapter(LEVELS)
    try:
        obs = adapter.reset("fixture_l1", 0)
        candidates = list(generate_candidates(config.skills.for_adapter("fixture"), adapter.capabilities().buttons,
                                              obs).candidates)
    finally:
        adapter.close()
    memory = WorkingMemory.from_config(config)
    memory.reset(obs)
    return obs, candidates, memory


def jev_model(handler, retries=1):
    return JevTacticalModel(JevClient(SETTINGS, 5.0, retries, transport=httpx.MockTransport(handler),
                                      sleep=lambda s: None))


def answer(choice, probabilities=None, usage=None, **extra):
    body = {"model": "typesafe/jev-1.13-20260917", "id": "gen-dec-test", "provider": "TypeSafe",
            "answers": {"skill": {"type": "choice", "choice": choice, **({"probabilities": probabilities}
                                                                         if probabilities is not None else {})}}}
    if usage is not None:
        body["usage"] = usage
    return {**body, **extra}


def test_body_is_one_choice_over_the_offered_ids_with_the_llm_payload_as_state(config):
    obs, candidates, memory = start(config)
    request = tactical_request(obs, None, candidates, memory.context())
    body = jev_model(lambda r: httpx.Response(500)).body(request)
    assert body["model"] == "typesafe/jev-1.13" and set(body["questions"]) == {"skill"}
    question = body["questions"]["skill"]
    assert question["type"] == "choice" and list(question["criteria"]) == list(request.candidate_ids)
    # State = exactly the LLM arm's user message, plus the LLM system prompt's rules and guide.
    azure = AzureTacticalModel(AzureChatClient(AzureSettings("https://x.invalid/", "k", "v", "d"), 5.0, 0))
    llm_user = json.loads(azure.body(request)["messages"][1]["content"])
    state = dict(body["state"])
    assert state.pop("rules") in azure.body(request)["messages"][0]["content"]
    assert state.pop("input_guide") in azure.body(request)["messages"][0]["content"]
    assert state == llm_user
    assert KEY not in json.dumps(body)


def test_live_fixture_contract(config):
    """The sanitized real response (tests/fixtures/jev/choice_response.json) through the model."""
    captured = json.loads((FIXTURES / "choice_response.json").read_text(encoding="utf-8"))
    response = {**captured["response"],
                "answers": {"skill": captured["response"]["answers"]["action"]}}  # our question id
    obs, candidates, memory = start(config)
    request = tactical_request(obs, None, candidates, memory.context()).model_copy(update={"candidates": tuple(
        {"id": cid, "description": d, "max_frames": 10}
        for cid, d in captured["request"]["questions"]["action"]["criteria"].items())})
    model = jev_model(lambda r: httpx.Response(200, json=response))
    text, call = model.propose(request)
    choice = model.parse(text, request.candidate_ids)
    assert choice.candidate_id == "c0_move_right" and choice.provider_score == 0.94
    assert choice.provider_score_meaning == SCORE_MEANING
    assert call.status == "ok" and call.model == "typesafe/jev-1.13-20260917"
    assert call.cost_usd == pytest.approx(1.8564e-05) and call.cost_source == "provider_reported"
    assert call.usage == {"input_tokens": 442.0, "output_tokens": 59.0, "total_tokens": 501.0}
    assert call.output["confidence"] == 0.92 and call.output["probabilities"]["c1_jump_right"] == 0.06
    assert call.response_ref == "gen-dec-1791052280-OJkB0gjyQkdg0Yaxzyxq"


def test_tactical_fixture_if_captured_parses():
    path = FIXTURES / "tactical_response.json"
    if not path.exists():
        pytest.skip("live tactical fixture not captured yet (probe-provider --provider jev --save-fixture)")
    captured = json.loads(path.read_text(encoding="utf-8"))
    assert KEY not in json.dumps(captured) and "bearer" not in json.dumps(captured).lower()
    allowed = set(captured["request"]["questions"]["skill"]["criteria"])
    assert jev.parse_choice(captured["response"], "skill", allowed).choice in allowed


def test_absent_distribution_and_usage_stay_none(config):
    obs, candidates, memory = start(config)
    request = tactical_request(obs, None, candidates, memory.context())
    model = jev_model(lambda r: httpx.Response(200, json=answer(request.candidate_ids[0])))
    text, call = model.propose(request)
    choice = model.parse(text, request.candidate_ids)
    assert choice.provider_score is None and choice.provider_score_meaning is None
    assert call.usage is None and call.cost_usd is None and call.cost_source is None
    assert call.output["probabilities"] is None and call.output["confidence"] is None


def test_off_list_choice_is_invalid_then_retried_then_falls_back(config):
    obs, candidates, memory = start(config)
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json=answer("not_offered", {"not_offered": 1.0}))

    controller = ModelController(jev_model(handler), config.tactical, config.models.max_retries)
    decision, calls = controller.decide(obs, None, candidates, memory.context())
    assert [c.status for c in calls] == ["invalid_output", "invalid_output"] and len(seen) == 2
    assert decision.fallback and decision.fallback_reason == "invalid_output"
    assert decision.context_digest == context_digest(tactical_request(obs, None, candidates, memory.context()))


@pytest.mark.parametrize("status", [429, 529, 503])
def test_retryable_statuses_are_retried(config, status):
    obs, candidates, memory = start(config)
    responses = [httpx.Response(status, text="busy"), httpx.Response(200, json=answer(
        candidates[1].candidate_id, {candidates[1].candidate_id: 0.7, candidates[0].candidate_id: 0.3},
        usage={"input_tokens": 10, "output_tokens": 2, "cost": 0.001}))]
    model = jev_model(lambda r: responses.pop(0))
    text, call = model.propose(tactical_request(obs, None, candidates, memory.context()))
    assert call.status == "ok" and call.retries == 1 and call.cost_usd == 0.001


def test_timeout_and_auth_error_become_fallbacks_without_leaking_the_key(config, caplog):
    obs, candidates, memory = start(config)

    def timeout(request):
        raise httpx.ReadTimeout("slow", request=request)

    controller = ModelController(jev_model(timeout), config.tactical, config.models.max_retries)
    decision, calls = controller.decide(obs, None, candidates, memory.context())
    assert decision.fallback and decision.fallback_reason == "call_timeout"
    assert {c.status for c in calls} == {"timeout"}

    seen = []

    def unauthorized(request):
        seen.append(request.headers["authorization"])
        return httpx.Response(401, text=f"bad key {KEY}")

    with caplog.at_level(logging.WARNING):
        controller = ModelController(jev_model(unauthorized), config.tactical, config.models.max_retries)
        decision, calls = controller.decide(obs, None, candidates, memory.context())
    assert seen[0] == f"Bearer {KEY}"
    assert decision.fallback_reason == "call_error" and all(c.retries == 0 for c in calls)  # 401: no transport retry
    assert KEY not in caplog.text and all(KEY not in c.model_dump_json() for c in calls)


def test_missing_key_is_a_config_error(config, monkeypatch):
    monkeypatch.setattr(jev, "load_dotenv", lambda: None)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    with pytest.raises(ConfigError, match="OPENROUTER_API_KEY"):
        JevSettings.from_env(config.models.jev)


def test_settings_have_no_credentials():
    model = jev_model(lambda r: httpx.Response(500))
    assert KEY not in json.dumps(model.settings()) and model.settings()["model_id"] == "typesafe/jev-1.13"
    assert KEY not in repr(SETTINGS)
