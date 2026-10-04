"""Phase 7: the shared tactical request, output validation, retry, deterministic fallback and
per-episode budgets (scripted models: no network), plus the Azure tactical model against a
mocked transport."""

import json

import httpx
import pytest

from dave_agent.adapters.fixture import FixtureAdapter
from dave_agent.control.skills import generate_candidates
from dave_agent.memory.episodes import EpisodeStore
from dave_agent.memory.working import WorkingMemory
from dave_agent.models.azure import AzureChatClient, AzureSettings, AzureTacticalModel
from dave_agent.models.tactical import (
    TIMEOUT,
    BudgetExhausted,
    ModelController,
    ScriptedTacticalModel,
    SeededMockModel,
    TacticalOutputError,
    parse_tactical,
    tactical_request,
    view_rows,
)
from dave_agent.runner.episode import run_episode
from dave_agent.schemas import Goal, TilePos

from ..conftest import LEVELS

SETTINGS = AzureSettings(endpoint="https://example.invalid/", api_key="test-key-0000",
                         api_version="2024-12-01-preview", deployment="dep")


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


def tactical(config, **update):
    return config.tactical.model_copy(update=update)


def play(config, model, cfg=None, recorder=None, max_frames=200, wall=None):
    adapter = FixtureAdapter(LEVELS)
    try:
        return run_episode(adapter, ModelController(model, cfg or config.tactical, config.models.max_retries),
                           config.skills.for_adapter("fixture"), config.skills.executor,
                           WorkingMemory.from_config(config), "fixture_l1", 0, max_frames=max_frames,
                           recorder=recorder, max_wall_seconds=wall)
    finally:
        adapter.close()


def test_request_holds_view_goal_waypoint_and_candidates(config):
    obs, candidates, memory = start(config)
    rows = view_rows(obs)
    assert len(rows) == obs.region.max.row - obs.region.min.row + 1
    assert sum(r.count("@") for r in rows) == 1 and any("#" in r for r in rows)
    goal = Goal(goal_id="g1", goal_type="collect", target_ref="collect:trophy:c6:r1",
                next_waypoint=TilePos(col=6, row=1), success_predicate="item_collected_at",
                constraints=("target:6,1", "hazard_near_target"), deadline_frame=100, source_observation_id=0)
    req = tactical_request(obs, goal, candidates, memory.context())
    here = req.player["tile"]
    assert req.goal["waypoint_offset"] == [6 - here[0], 1 - here[1]]
    assert req.goal["constraints"] == ["hazard_near_target"]  # the internal target tile is not repeated
    assert req.goal["frames_left"] == 100 - obs.frame
    assert req.candidate_ids == tuple(c.candidate_id for c in candidates)


@pytest.mark.parametrize(("text", "match"), [
    (None, "empty"),
    ("move_right", "not JSON"),
    ('{"candidate_id": "move_right", "why": "x"}', "exactly"),
    ('{"candidate_id": "teleport"}', "not offered"),
])
def test_parse_rejects_bad_or_unauthorized_output(text, match):
    with pytest.raises(TacticalOutputError, match=match):
        parse_tactical(text, ("move_right", "wait"))


def test_invalid_output_is_retried_with_feedback(config):
    obs, candidates, memory = start(config)
    model = ScriptedTacticalModel(['{"candidate_id": "teleport"}', json.dumps({"candidate_id": "c2_jump"})])
    decision, calls = ModelController(model, config.tactical, 1).decide(obs, None, candidates, memory.context())
    assert decision.candidate_id == "c2_jump" and not decision.fallback
    assert [c.status for c in calls] == ["invalid_output", "ok"]
    assert model.feedback[0] is None and "not offered" in model.feedback[1]


@pytest.mark.parametrize(("outputs", "reason", "statuses"), [
    (["nope", "{}"], "invalid_output", ["invalid_output", "invalid_output"]),
    ([TIMEOUT, TIMEOUT], "call_timeout", ["timeout", "timeout"]),
    ([None, None], "call_error", ["error", "error"]),
])
def test_retries_then_deterministic_legal_fallback(config, outputs, reason, statuses):
    obs, candidates, memory = start(config)
    controller = ModelController(ScriptedTacticalModel(outputs), config.tactical, 1)
    decision, calls = controller.decide(obs, None, candidates, memory.context())
    assert decision.fallback and decision.fallback_reason == reason
    assert decision.candidate_id == "c5_wait"  # first offered of tactical.fallback_skills
    assert [c.status for c in calls] == statuses


def test_fallback_without_a_listed_skill_takes_the_first_candidate(config):
    obs, candidates, memory = start(config)
    controller = ModelController(ScriptedTacticalModel([None, None]), tactical(config, fallback_skills=()), 1)
    decision, _ = controller.decide(obs, None, candidates, memory.context())
    assert decision.candidate_id == candidates[0].candidate_id


def test_call_budget_terminates_or_falls_back(config):
    obs, candidates, memory = start(config)
    stop = ModelController(SeededMockModel(0, "m"), tactical(config, max_calls_per_episode=2), 1)
    stop.decide(obs, None, candidates, memory.context())
    stop.decide(obs, None, candidates, memory.context())
    with pytest.raises(BudgetExhausted) as exc:
        stop.decide(obs, None, candidates, memory.context())
    assert exc.value.budget == "tactical_calls"

    keep = ModelController(SeededMockModel(0, "m"),
                           tactical(config, max_calls_per_episode=1, on_budget_exhausted="fallback"), 1)
    keep.decide(obs, None, candidates, memory.context())
    decision, calls = keep.decide(obs, None, candidates, memory.context())
    assert decision.fallback and decision.fallback_reason == "budget:tactical_calls" and calls == ()


def test_budget_hit_mid_retry_keeps_the_calls_made(config):
    obs, candidates, memory = start(config)
    controller = ModelController(ScriptedTacticalModel(["bad"]), tactical(config, max_calls_per_episode=1), 1)
    with pytest.raises(BudgetExhausted) as exc:
        controller.decide(obs, None, candidates, memory.context())
    assert [c.status for c in exc.value.calls] == ["invalid_output"]


def test_token_and_cost_budgets_count_reported_usage(config):
    obs, candidates, memory = start(config)
    tokens = ModelController(ScriptedTacticalModel([], usage={"total_tokens": 600.0}),
                             tactical(config, max_tokens_per_episode=1000), 1)
    tokens.decide(obs, None, candidates, memory.context())
    tokens.decide(obs, None, candidates, memory.context())
    assert tokens.exhausted() == "tactical_tokens"
    cost = ModelController(ScriptedTacticalModel([], cost_usd=0.02), tactical(config, max_cost_usd_per_episode=0.03), 1)
    cost.decide(obs, None, candidates, memory.context())
    assert cost.exhausted() is None
    cost.decide(obs, None, candidates, memory.context())
    assert cost.exhausted() == "tactical_cost"
    unknown = ModelController(SeededMockModel(0, "m"), tactical(config, max_cost_usd_per_episode=0.01), 1)
    unknown.decide(obs, None, candidates, memory.context())
    assert unknown.exhausted() is None  # no reported cost: the cost budget cannot trigger


def test_budgets_reset_per_episode(config):
    obs, candidates, memory = start(config)
    controller = ModelController(SeededMockModel(0, "m"), tactical(config, max_calls_per_episode=1), 1)
    controller.decide(obs, None, candidates, memory.context())
    nxt = obs.model_copy(update={"episode_id": "fixture_l1-s0-e2"})
    controller.decide(nxt, None, candidates, memory.context())  # new episode: no BudgetExhausted
    assert controller.use.calls == 1


def test_seeded_mock_model_matches_the_mock_controller(config):
    from dave_agent.models.mock import SeededMockController

    old, new = play_old(config, SeededMockController(3, "m")), play(config, SeededMockModel(3, "m"))
    assert [d.candidate_id for d in old.decisions] == [d.candidate_id for d in new.decisions]


def play_old(config, controller):
    adapter = FixtureAdapter(LEVELS)
    try:
        return run_episode(adapter, controller, config.skills.for_adapter("fixture"), config.skills.executor,
                           WorkingMemory.from_config(config), "fixture_l1", 3, max_frames=200)
    finally:
        adapter.close()


def test_fixture_episode_with_failures_is_fully_traceable(config, tmp_path):
    outputs = ['{"candidate_id": "teleport"}', json.dumps({"candidate_id": "c1_move_right"}), TIMEOUT, TIMEOUT, "x"]
    with EpisodeStore(tmp_path / "e.sqlite") as store:
        store.create_run("r1", mode="mock", command="test", config_json="{}")
        recorder = store.recorder("r1", "A", "scripted", "fixture_l1", 0, 50)
        result = play(config, ScriptedTacticalModel(outputs), recorder=recorder)
        decisions = store._conn.execute("SELECT seq, fallback, model_call_seq FROM decisions ORDER BY seq").fetchall()
        executions = store._conn.execute("SELECT COUNT(*) FROM skill_executions").fetchone()[0]
        calls = store._conn.execute("SELECT status FROM model_calls WHERE purpose='tactical' ORDER BY seq").fetchall()
    assert result.outcome != "error"
    assert len(result.decisions) == len(result.executions) == len(decisions) == executions  # one action per decision
    assert [d.fallback for d in result.decisions[:3]] == [False, True, False]
    assert result.decisions[1].fallback_reason == "call_timeout"
    assert [c[0] for c in calls[:5]] == ["invalid_output", "ok", "timeout", "timeout", "invalid_output"]
    assert decisions[0][2] == 1 and decisions[1][2] == 3  # each decision links to its last call
    types = [e.event_type for e in result.events]
    assert types.count("model_failure") == 4 and types.count("decision_fallback") == 1


def test_budget_exhaustion_terminates_the_episode_as_truncated(config):
    result = play(config, SeededMockModel(0, "m"), tactical(config, max_calls_per_episode=3))
    assert result.outcome == "truncated" and result.termination_reason == "budget:tactical_calls"
    assert len(result.decisions) == 3 and result.events[-1].payload == {"budget": "tactical_calls"}


def test_budget_fallback_mode_finishes_the_episode_without_calls(config):
    result = play(config, SeededMockModel(0, "m"),
                  tactical(config, max_calls_per_episode=3, on_budget_exhausted="fallback"))
    assert result.termination_reason.startswith("terminal:") or result.termination_reason.startswith("max_frames")
    assert len(result.model_calls) == 3 and all(d.fallback for d in result.decisions[3:])


def test_wall_time_budget_truncates(config):
    result = play(config, SeededMockModel(0, "m"), wall=0)
    assert result.outcome == "truncated" and result.termination_reason == "budget:wall_time:0s"
    assert result.decisions == []


def azure_model(handler):
    client = AzureChatClient(SETTINGS, 5.0, 1, transport=httpx.MockTransport(handler), sleep=lambda s: None)
    return AzureTacticalModel(client, 500, "low")


def test_azure_tactical_body_is_a_strict_choice_without_credentials(config):
    obs, candidates, memory = start(config)
    req = tactical_request(obs, None, candidates, memory.context())
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={"id": "resp-1", "choices": [{"message": {"content": '{"candidate_id": "c2_jump"}'},
                                                                     "finish_reason": "stop"}],
                                         "usage": {"prompt_tokens": 900, "completion_tokens": 20,
                                                   "total_tokens": 920}})

    model = azure_model(handler)
    text, call = model.propose(req)
    body = json.loads(seen[0].content)
    schema = body["response_format"]["json_schema"]
    assert schema["strict"] and schema["schema"]["properties"]["candidate_id"]["enum"] == list(req.candidate_ids)
    assert body["reasoning_effort"] == "low" and body["max_completion_tokens"] == 500
    assert "test-key-0000" not in seen[0].content.decode() and "episode_id" not in body["messages"][1]["content"]
    assert model.parse(text, req.candidate_ids).candidate_id == "c2_jump"
    assert call.purpose == "tactical" and call.usage["total_tokens"] == 920 and call.response_ref == "resp-1"
    assert "api_key" not in json.dumps(model.settings())


def test_azure_tactical_timeout_becomes_a_fallback(config):
    obs, candidates, memory = start(config)

    def handler(request):
        raise httpx.ReadTimeout("slow", request=request)

    controller = ModelController(azure_model(handler), config.tactical, 1)
    decision, calls = controller.decide(obs, None, candidates, memory.context())
    assert decision.fallback and decision.fallback_reason == "call_timeout"
    assert [c.status for c in calls] == ["timeout", "timeout"] and calls[0].retries == 1
