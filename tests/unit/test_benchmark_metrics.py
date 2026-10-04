"""Phase 9: per-episode benchmark records and summaries: cost and latency aggregation, unknown
costs never becoming zero, every failure counted, the repeated-failure key and paired stats."""

import httpx
import pytest

from dave_agent.adapters.fixture import FixtureAdapter
from dave_agent.config import PriceConfig
from dave_agent.control.skills import ExecutionResult
from dave_agent.evaluation.metrics import episode_record, failure_key
from dave_agent.evaluation.summary import summarize
from dave_agent.models.azure import AzureChatClient, AzureSettings, AzureTacticalModel, estimated_cost
from dave_agent.runner.episode import EpisodeResult
from dave_agent.schemas import Decision, ModelCallRecord

from ..conftest import LEVELS

PRICE = PriceConfig(input_per_mtok=1.0, output_per_mtok=4.0, source="test table", as_of="2026-10-03")


def call(provider="openrouter_jev", purpose="tactical", status="ok", latency=100.0, cost=None, source=None,
         retries=0):
    return ModelCallRecord(provider=provider, model="m", purpose=purpose, status=status, latency_ms=latency,
                           cost_usd=cost, cost_source=source, retries=retries,
                           usage={"total_tokens": 10.0, "prompt_tokens": 8.0, "completion_tokens": 2.0})


def result(outcome="truncated", calls=(), decisions=(), frames=600, **extra):
    r = EpisodeResult(episode_id="e", adapter="fixture", outcome=outcome, termination_reason=f"x:{outcome}",
                      frames=frames, score=0, lives=3, model_calls=list(calls), decisions=list(decisions),
                      wall_seconds=2.0)
    for k, v in extra.items():
        setattr(r, k, v)
    return r


def ident(arm="A", trial=0, **extra):
    return {"environment": "fixture", "mode": "live", "regime": "cold", "checkpoint": None, "scenario": "s",
            "trial": trial, "seed": trial, "arm": arm, **extra}


def test_cost_reported_plus_estimated_and_unknown_is_never_zero():
    rec = episode_record(result(calls=[call(cost=0.01, source="provider_reported"),
                                       call(provider="azure_openai", cost=0.02, source="estimated")]), ident())
    assert rec["cost_usd"] == pytest.approx(0.03) and rec["cost_reported_usd"] == pytest.approx(0.01)
    assert rec["cost_estimated_usd"] == pytest.approx(0.02) and rec["cost_unknown_calls"] == 0
    unknown = episode_record(result(calls=[call(cost=0.01, source="provider_reported"),
                                           call(provider="azure_openai")]), ident())
    assert unknown["cost_usd"] is None and unknown["cost_unknown_calls"] == 1
    offline = episode_record(result(calls=[call(provider="mock")]), ident())
    assert offline["cost_usd"] == 0.0  # mocks make no paid call: known, free
    groups = summarize([rec, unknown], 0.95, 100)["groups"]
    assert groups[0]["cost_usd_total"] is None and groups[0]["cost_usd_per_attempt"] is None
    assert groups[0]["cost_unknown_calls"] == 1 and groups[0]["cost_reported_usd"] == pytest.approx(0.02)


def test_successful_latency_is_separate_from_decision_latency_with_reasks():
    calls = [call(status="invalid_output", latency=300.0), call(latency=100.0), call(latency=50.0, retries=2)]
    decisions = [Decision(candidate_id="c1", observation_id=1, context_digest="d"),
                 Decision(candidate_id="c1", observation_id=2, context_digest="d")]
    rec = episode_record(result(calls=calls, decisions=decisions, decision_latency_ms=[400.0, 50.0],
                                decision_calls=[2, 1]), ident())
    assert rec["ok_latencies_ms"] == [100.0, 50.0] and rec["decision_latencies_ms"] == [400.0, 50.0]
    assert rec["tactical_reasks"] == 1 and rec["invalid_outputs"] == 1 and rec["transport_retries"] == 2
    assert rec["model_wait_seconds"] == pytest.approx(0.45) and rec["overhead_seconds"] == pytest.approx(1.55)
    assert rec["call_status"] == {"tactical:invalid_output": 1}


def test_every_failure_counts_in_the_summary():
    records = [episode_record(result("level_complete", frames=470), ident(trial=0)),
               episode_record(result("error"), ident(trial=1), error="RuntimeError: boom"),
               episode_record(result("truncated"), ident(trial=2)),
               episode_record(result("game_over"), ident(trial=3))]
    g = summarize(records, 0.95, 100)["groups"][0]
    assert g["n"] == 4 and g["completions"] == 1 and g["completion_rate"] == 0.25 and g["errors"] == 1
    assert g["censored"] == 3 and g["frames_to_completion_median"] == 470
    assert g["outcomes"] == {"error": 1, "game_over": 1, "level_complete": 1, "truncated": 1}
    assert records[0]["frames_to_completion"] == 470 and records[2]["frames_to_completion"] is None


def test_repeated_failure_key_is_tile_skill_and_failure_type():
    adapter = FixtureAdapter(LEVELS)
    try:
        obs = adapter.reset("fixture_l1", 0)
    finally:
        adapter.close()
    run = ExecutionResult("c1", "jump_right", "interrupted", "hazard_contact:fire", 5, 5, obs)
    ok = ExecutionResult("c2", "wait", "completed", None, 5, 5, obs)
    rec = episode_record(result(executions=[run, ok, run, run], execution_starts=[(3, 9), (3, 9), (3, 9), (4, 9)]),
                         ident())
    assert failure_key((3, 9), "jump_right", "interrupted", "hazard_contact:fire") == \
        "c3r9|jump_right|interrupted:hazard_contact"
    assert rec["failures"] == 3 and rec["repeated_failures"] == 1
    assert rec["repeated_failure_keys"] == ["c3r9|jump_right|interrupted:hazard_contact"]


def test_pairs_match_on_trial_and_seed_and_count_unpaired():
    def rec(arm, trial, done):
        return episode_record(result("level_complete" if done else "truncated"), ident(arm, trial))
    records = [rec("A", 0, True), rec("B", 0, False), rec("A", 1, True), rec("B", 1, True),
               rec("A", 2, False), rec("B", 2, False), rec("A", 3, True)]  # B trial 3 never ran
    pair = summarize(records, 0.95, 200, seed=1)["pairs"][0]
    assert pair["arms"] == ["A", "B"] and pair["n_pairs"] == 3 and pair["unpaired"] == 1
    assert (pair["both_completed"], pair["only_first_completed"], pair["only_second_completed"],
            pair["neither_completed"]) == (1, 1, 0, 1)
    assert pair["completion_diff"] == 0.3333 and pair["mcnemar_p"] == 1.0
    assert pair == summarize(records, 0.95, 200, seed=1)["pairs"][0]  # deterministic


def test_regimes_and_modes_are_never_pooled():
    records = [episode_record(result(), ident(regime="cold")), episode_record(result(), ident(regime="warm")),
               episode_record(result(), ident(mode="mock"))]
    groups = summarize(records)["groups"]
    assert len(groups) == 3 and all(g["n"] == 1 for g in groups)


def test_azure_price_table_gives_an_estimated_cost():
    assert estimated_cost({"prompt_tokens": 1000.0, "completion_tokens": 500.0}, PRICE) == {
        "cost_usd": pytest.approx(0.003), "cost_source": "estimated",
        "output": {"price_source": "test table", "price_as_of": "2026-10-03"}}
    assert estimated_cost({"prompt_tokens": 1000.0, "completion_tokens": 500.0}, None) == {}
    assert estimated_cost(None, PRICE) == {}


def test_azure_tactical_records_carry_the_estimate(config):
    from .test_tactical import start

    obs, candidates, memory = start(config)
    from dave_agent.models.tactical import tactical_request

    request = tactical_request(obs, None, candidates, memory.context())
    reply = {"id": "r1", "choices": [{"message": {"content": '{"candidate_id": "%s"}' % candidates[0].candidate_id},
                                      "finish_reason": "stop"}],
             "usage": {"prompt_tokens": 2000, "completion_tokens": 250, "total_tokens": 2250}}
    settings = AzureSettings(endpoint="https://example.invalid/", api_key="k", api_version="v", deployment="dep")
    client = AzureChatClient(settings, 5.0, 0, transport=httpx.MockTransport(lambda r: httpx.Response(200, json=reply)),
                             sleep=lambda s: None)
    model = AzureTacticalModel(client, price=PRICE)
    _, record = model.propose(request)
    assert record.cost_source == "estimated" and record.cost_usd == pytest.approx(0.003)
    assert record.output["price_source"] == "test table" and model.settings()["price"]["as_of"] == "2026-10-03"
    _, unpriced = AzureTacticalModel(client).propose(request)
    assert unpriced.cost_usd is None and unpriced.cost_source is None
