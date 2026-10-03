"""Phase 6: planner output validation, the offline rule planner, and the Azure client against the
recorded response fixture (mocked transport: no network)."""

import json
import os
from pathlib import Path

import httpx
import pytest

from dave_agent.config import ConfigError
from dave_agent.control.goals import TargetMemory, goal_candidates
from dave_agent.models import azure
from dave_agent.models.azure import AzureChatClient, AzurePlanner, AzureSettings
from dave_agent.models.planner import PlanOutputError, PlanningRequest, RuleMockPlanner, parse_plan

from .test_goals import o

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "azure" / "planner_response.json"
SETTINGS = AzureSettings(endpoint="https://example.invalid/", api_key="test-key-0000", api_version="2024-12-01-preview",
                         deployment="dep")


def request(config, **update):
    obs = o()
    targets = TargetMemory()
    targets.reset(obs)
    base = PlanningRequest(
        episode_id="e1", level_id="L1", frame=0, observation_id=1, triggers=("no_goal",), player={}, lives=3,
        inventory={"trophy": 0}, score=0, view_cols=(0, 9), nearby=(), recent=(), previous_goal=None,
        current_goal=None, candidates=goal_candidates(obs, targets, config.planning), graph_routes=False)
    return base.model_copy(update=update)


@pytest.mark.parametrize(("text", "match"), [
    (None, "empty"),
    ("{goal: x}", "not JSON"),
    ('{"goal": "explore:right", "why": "x"}', "does not match"),
    ('{"goal": "collect:key:c1:r1", "rationale": ""}', "not an offered candidate"),
])
def test_parse_plan_rejects_bad_output(text, match):
    with pytest.raises(PlanOutputError, match=match):
        parse_plan(text, ("explore:right",))


def test_rule_planner_avoids_a_goal_that_just_failed(config):
    req = request(config)
    first = json.loads(RuleMockPlanner().propose(req)[0])["goal"]
    assert first == "collect:trophy:c6:r1"
    failed = req.model_copy(update={"previous_goal": {"target_ref": first, "status": "failed"}})
    assert json.loads(RuleMockPlanner().propose(failed)[0])["goal"] != first


def client(handler, retries=1):
    return AzureChatClient(SETTINGS, 5.0, retries, transport=httpx.MockTransport(handler), sleep=lambda s: None)


def test_recorded_azure_response_parses_to_a_valid_choice(config):
    recorded = json.loads(FIXTURE.read_text(encoding="utf-8"))
    seen = []

    def handler(req):
        seen.append(req)
        return httpx.Response(200, json=recorded)

    planner = AzurePlanner(client(handler))
    req = request(config, candidates=request(config).candidates[1:])  # any candidate set works for the transport
    text, call = planner.propose(req)
    assert call.status == "ok" and call.purpose == "planner" and call.response_ref == recorded["id"]
    assert call.usage["prompt_tokens"] == recorded["usage"]["prompt_tokens"] and call.cost_usd is None
    assert parse_plan(text, tuple(recorded["_meta"]["candidate_ids"])).goal == "collect:gem:c2:r2"
    sent = seen[0]
    assert sent.url.path == "/openai/deployments/dep/chat/completions"
    assert sent.url.params["api-version"] == "2024-12-01-preview" and sent.headers["api-key"] == "test-key-0000"
    body = json.loads(sent.content)
    assert body["response_format"]["json_schema"]["strict"] is True
    assert body["response_format"]["json_schema"]["schema"]["properties"]["goal"]["enum"] == list(req.candidate_ids)


def test_feedback_is_sent_on_retry(config):
    bodies = []

    def handler(req):
        bodies.append(json.loads(req.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": "{}"}}]})

    AzurePlanner(client(handler)).propose(request(config), feedback="goal 'x' is not an offered candidate")
    assert "previous answer was rejected" in bodies[0]["messages"][-1]["content"]


def test_transient_errors_retry_then_succeed():
    calls = []

    def handler(req):
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(429, text="rate limited")
        return httpx.Response(200, json={"choices": [{"message": {"content": "{}"}}]})

    result = client(handler).complete({})
    assert result.status == "ok" and result.retries == 1


def test_timeout_and_auth_errors_are_bounded_and_redacted(monkeypatch):
    def timeout(req):
        raise httpx.ReadTimeout("slow", request=req)

    result = client(timeout, retries=2).complete({})
    assert (result.status, result.retries, result.text) == ("timeout", 2, None)

    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "test-key-0000")
    hits = []

    def unauthorized(req):
        hits.append(1)
        return httpx.Response(401, text="bad key test-key-0000")

    result = client(unauthorized, retries=3).complete({})
    assert result.status == "error" and len(hits) == 1  # not retried
    assert "test-key-0000" not in result.error


def test_missing_azure_settings_fail_before_any_call(monkeypatch):
    monkeypatch.setattr(azure, "load_dotenv", lambda: None)
    for name in ("AZURE_OPENAI_ENDPOINT", "AZURE_OPENAI_API_KEY", "AZURE_OPENAI_API_VERSION", "DEP_ENV"):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(ConfigError, match="AZURE_OPENAI_ENDPOINT.*DEP_ENV"):
        AzureSettings.from_env("DEP_ENV")


@pytest.mark.live
@pytest.mark.skipif(os.environ.get("RUN_LIVE") != "1", reason="paid Azure call; set RUN_LIVE=1")
def test_live_azure_planner_returns_valid_choice(config):
    settings = AzureSettings.from_env(config.models.planner.deployment_env)
    planner = AzurePlanner(AzureChatClient(settings, config.models.timeout_seconds, config.models.max_retries))
    req = request(config)
    try:
        text, call = planner.propose(req)
    finally:
        planner.client.close()
    assert call.status == "ok"
    assert parse_plan(text, req.candidate_ids).goal in req.candidate_ids
