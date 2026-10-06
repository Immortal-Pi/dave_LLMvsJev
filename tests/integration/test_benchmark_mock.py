"""Phase 9 acceptance: a small mock benchmark on the fixture reproduces its manifest, episode
records, summaries and paired metrics; arms stay isolated; warm checkpoints stay frozen; the
paid-run ceiling stops scheduling; failures are recorded, not dropped; unpriced live arms are
refused before any call."""

import json

import pytest

from dave_agent.adapters.fixture import FixtureAdapter
from dave_agent.cli import main
from dave_agent.config import ConfigError
from dave_agent.evaluation.metrics import VOLATILE_FIELDS
from dave_agent.memory.graph import GraphStore
from dave_agent.memory.persistence import GraphCheckpointError, save_store
from dave_agent.models.planner import RuleMockPlanner
from dave_agent.models.tactical import ModelController, ScriptedTacticalModel
from dave_agent.runner import session
from dave_agent.runner.benchmark import BenchmarkRunner, BenchmarkSpec, resummarize, sha256_file, train_memory
from dave_agent.runner.session import MODES, Models

from ..conftest import CONFIG, LEVELS


def strip(value):
    """Drop the machine- and clock-dependent fields (docs/benchmark.md, "Reproducibility")."""
    if isinstance(value, dict):
        return {k: strip(v) for k, v in value.items() if k not in VOLATILE_FIELDS}
    if isinstance(value, list):
        return [strip(v) for v in value]
    return value


def read(out):
    manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    episodes = [json.loads(line) for line in (out / "episodes.jsonl").read_text(encoding="utf-8").splitlines()]
    summary = json.loads((out / "summary.json").read_text(encoding="utf-8"))
    return manifest, episodes, summary


def spec(tmp_path, name="b", **update):
    base = dict(arms=("A", "B", "C"), trials=3, scenarios=("fixture_l1",), adapter="fixture",
                out_dir=tmp_path / name, benchmark_id="bench-test")
    return BenchmarkSpec(**{**base, **update})


def test_mock_benchmark_reproduces_manifest_records_and_summaries(config, tmp_path):
    first = BenchmarkRunner(config, spec(tmp_path, "one")).run()
    second = BenchmarkRunner(config, spec(tmp_path, "two")).run()
    assert first["status"] == second["status"] == "complete" and first["episodes_run"] == 9
    one, two = read(tmp_path / "one"), read(tmp_path / "two")
    for a, b in zip(one, two, strict=True):
        assert strip(a) == strip(b)
    manifest, episodes, summary = one
    assert manifest["mode"] == "mock" and manifest["environment_label"].startswith("fixture: synthetic")
    assert manifest["scenario_hashes"]["fixture_l1"].keys() == {"0", "1", "2"}
    assert set(manifest["prompt_hashes"]) >= {"game_rules", "input_guide", "tactical_task"}
    assert manifest["arms"]["B"]["settings"]["tactical"]["model"] == "mock-jev"
    assert manifest["memory"] == {"A": "none", "B": "none", "C": "in-episode"}
    # Every arm of a trial starts from the same scenario and seed; arm order is a reproducible shuffle.
    for entry in manifest["schedule"]:
        assert sorted(entry["order"]) == ["A", "B", "C"]
        same = [e for e in episodes if e["trial"] == entry["trial"]]
        assert {e["seed"] for e in same} == {entry["seed"]} and \
            [e["arm"] for e in sorted(same, key=lambda e: e["order"])] == entry["order"]
    assert any(e["order"] != ["A", "B", "C"] for e in manifest["schedule"])
    assert len(summary["groups"]) == 3 and len(summary["pairs"]) == 3
    assert all(p["n_pairs"] == 3 for p in summary["pairs"])
    # summaries rebuild identically from episodes.jsonl alone
    before = (tmp_path / "one" / "summary.json").read_text(encoding="utf-8")
    resummarize(tmp_path / "one")
    assert (tmp_path / "one" / "summary.json").read_text(encoding="utf-8") == before


def test_graph_files_are_per_arm_and_trial_and_only_for_graph_arms(config, tmp_path):
    BenchmarkRunner(config, spec(tmp_path)).run()
    graphs = tmp_path / "b" / "graphs"
    # One store directory per arm and trial, one checkpoint per level inside it.
    assert sorted(p.relative_to(graphs).as_posix() for p in graphs.rglob("*.json")) == \
        [f"C/fixture_l1-t00{k}/fixture_l1.json" for k in range(3)]
    level = graphs / "C" / "fixture_l1-t001" / "fixture_l1.json"
    lineage = json.loads(level.read_text(encoding="utf-8"))["lineage"]
    assert [(e["arm"], e["run_id"]) for e in lineage] == [("C", "bench-test-fixture_l1-t001-C")]


def test_warm_regime_reads_a_frozen_checkpoint(config, tmp_path):
    ckpt = tmp_path / "c.json"
    report = train_memory(config, "C", 2, ("fixture_l1",), "fixture", ckpt, tmp_path / "train.sqlite")
    assert len(report["episodes"]) == 2 and report["sha256"] == sha256_file(ckpt)
    manifest = BenchmarkRunner(config, spec(tmp_path, regime="warm", checkpoints={"C": ckpt})).run()
    assert manifest["status"] == "complete" and manifest["checkpoints_changed"] == {}
    assert sha256_file(ckpt) == report["sha256"]
    info = manifest["checkpoints"]["C"]
    assert info["sha256"] == report["sha256"] and info["training_episodes"] == 2
    assert info["same_level_learning"] == ["fixture_l1"]  # trained on the evaluation level: labeled
    episodes = read(tmp_path / "b")[1]
    assert {e["memory"] for e in episodes if e["arm"] == "C"} == {"frozen-checkpoint"}
    assert len({e["checkpoint"] for e in episodes}) == 1  # one warm group, so arms still pair
    assert not (tmp_path / "b" / "graphs").exists()


def test_warm_regime_refusals(config, tmp_path):
    with pytest.raises(ConfigError, match="--checkpoint C=PATH"):
        BenchmarkRunner(config, spec(tmp_path, regime="warm")).run()
    with pytest.raises(ConfigError, match="needs --memory-regime warm"):
        BenchmarkRunner(config, spec(tmp_path, checkpoints={"C": tmp_path / "x.json"})).run()
    with pytest.raises(ConfigError, match="not graph-enabled"):
        BenchmarkRunner(config, spec(tmp_path, regime="warm", checkpoints={"A": tmp_path / "x.json"})).run()
    other = GraphStore("fixture", FixtureAdapter(LEVELS).capabilities().build_id, "local_observed")
    other.for_level("fixture_l1")
    other.add_lineage("r", "r/e", "X", "fixture_l1")  # trained by another arm
    save_store(other, tmp_path / "other.json")
    with pytest.raises(GraphCheckpointError, match="trained by arm"):
        BenchmarkRunner(config, spec(tmp_path, "w", regime="warm", checkpoints={"C": tmp_path / "other.json"})).run()


def _scripted_factory(cost=None, explode=False):
    def factory(config, arm, live_planner, live_tactical, seed):
        def boom(request):
            raise RuntimeError("provider exploded")
        model = ScriptedTacticalModel([boom] if explode else [], usage={"total_tokens": 10.0}, cost_usd=cost)
        return Models(RuleMockPlanner(), model, ModelController(model, config.tactical, config.models.max_retries),
                      MODES[(live_planner, live_tactical)])
    return factory


def test_paid_ceiling_stops_scheduling_and_lists_unrun_episodes(config, tmp_path):
    cfg = config.model_copy(update={"benchmark": config.benchmark.model_copy(update={"paid_run_budget_usd": 1.0})})
    messages = []
    runner = BenchmarkRunner(cfg, spec(tmp_path, arms=("B", "C"), live_tactical=True),
                             models_factory=_scripted_factory(cost=0.1), notify=messages.append)
    manifest = runner.run()
    assert manifest["status"] == "budget_stopped" and manifest["paid"] is True
    assert manifest["episodes_run"] == 1 and len(manifest["not_run"]) == 5
    assert manifest["spent_usd"]["reported"] >= 1.0
    assert messages[0]["paid_run"] == "live-tactical" and messages[0]["paid_run_budget_usd"] == 1.0
    summary = read(tmp_path / "b")[2]
    # Only the first arm ran: it is summarized, and no pair exists to compare.
    assert [g["n"] for g in summary["groups"]] == [1] and summary["pairs"] == []


def test_an_episode_error_is_recorded_as_a_failure_and_the_run_goes_on(config, tmp_path):
    manifest = BenchmarkRunner(config, spec(tmp_path, arms=("A", "B"), trials=2),
                               models_factory=_scripted_factory(explode=True)).run()
    assert manifest["status"] == "complete" and manifest["episodes_run"] == 4
    episodes = read(tmp_path / "b")[1]
    assert {e["outcome"] for e in episodes} == {"error"}
    assert all(e["error"] == "RuntimeError: provider exploded" for e in episodes)
    group = read(tmp_path / "b")[2]["groups"][0]
    assert group["n"] == 2 and group["errors"] == 2 and group["completions"] == 0


def test_live_unpriced_azure_is_refused_before_any_call(config, tmp_path):
    models = config.models
    unpriced = {role: getattr(models, role).model_copy(update={"price": None}) for role in ("planner", "tactical_llm")}
    config = config.model_copy(update={"models": models.model_copy(update=unpriced)})
    built = []

    def factory(*args):
        built.append(args)
        raise AssertionError("no model may be built")
    with pytest.raises(ConfigError, match="models.planner.price"):
        BenchmarkRunner(config, spec(tmp_path, arms=("A",), live_planner=True), models_factory=factory).run()
    with pytest.raises(ConfigError, match="models.tactical_llm.price"):
        BenchmarkRunner(config, spec(tmp_path, arms=("A", "B"), live_tactical=True), models_factory=factory).run()
    assert not built and not (tmp_path / "b").exists()


def test_cli_benchmark_and_summarize(tmp_path, capsys):
    out = tmp_path / "cli"
    assert main(["benchmark", "--config", str(CONFIG), "--arms", "A,B", "--trials", "2", "--id", "cli",
                 "--out", str(out)]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "complete" and report["episodes_run"] == 4 and report["mode"] == "mock"
    assert report["pairs"][0]["arms"] == ["A", "B"] and report["pairs"][0]["n_pairs"] == 2
    assert main(["summarize", "--benchmark", str(out)]) == 0
    assert main(["benchmark", "--config", str(CONFIG), "--arms", "A", "--trials", "1", "--out", str(out)]) == 2
    assert "not empty" in capsys.readouterr().err


def test_reach_hints_switch_is_applied_to_every_arm(config, tmp_path, monkeypatch):
    seen = []
    real = session.GoalManager

    def spy(*args, **kwargs):
        seen.append(kwargs["reach"])
        return real(*args, **kwargs)
    monkeypatch.setattr(session, "GoalManager", spy)
    # Give the fixture a reach envelope so that "on" and "off" differ (normally only dave has one).
    reach = {**config.skills.reach, "fixture": config.skills.reach["dave"]}
    cfg = config.model_copy(update={"skills": config.skills.model_copy(update={"reach": reach})})
    BenchmarkRunner(cfg, spec(tmp_path, "on", trials=1)).run()
    assert seen == [reach["fixture"]] * 3
    seen.clear()
    manifest = BenchmarkRunner(cfg, spec(tmp_path, "off", trials=1, reach_hints=False)).run()
    assert manifest["reach_hints"] is False and seen == [None] * 3
    assert {e["reach_hints"] for e in read(tmp_path / "off")[1]} == {False}


def test_interrupt_keeps_finished_episodes_and_lists_the_rest(config, tmp_path):
    built = []

    def factory(*args):
        built.append(args)
        if len(built) > 5:  # validate() and the manifest build A and B twice (4), then one episode runs
            raise KeyboardInterrupt
        return session.build_models(*args)
    with pytest.raises(KeyboardInterrupt):
        BenchmarkRunner(config, spec(tmp_path, arms=("A", "B"), trials=2), models_factory=factory).run()
    manifest, episodes, summary = read(tmp_path / "b")
    assert manifest["status"] == "interrupted" and manifest["episodes_run"] == 1 and len(episodes) == 1
    assert [n["reason"] for n in manifest["not_run"]] == ["interrupted"] * 3
    assert summary["groups"][0]["n"] == 1
