"""The offline scoreboard (runner/sweep.py): overrides are validated, the grid expands, the rank
key orders summaries, a used output directory is refused, and a fixture sweep is reproducible."""

import json

import pytest

from dave_agent.adapters.fixture import FixtureAdapter
from dave_agent.config import ConfigError
from dave_agent.runner.sweep import grid_configs, override, rank_key, run_sweep

from ..conftest import LEVELS


def fixture_factory(name, env):
    return FixtureAdapter(LEVELS)


def test_override_sets_nested_value_and_validates(config):
    before = config.graph.weights.risk
    changed = override(config, "graph.weights.risk", before + 1)
    assert changed.graph.weights.risk == before + 1
    assert config.graph.weights.risk == before  # the original is untouched
    assert changed.planning == config.planning
    with pytest.raises(ConfigError, match="unknown config key"):
        override(config, "graph.weights.nope", 1)
    with pytest.raises(ConfigError, match="invalid value"):
        override(config, "graph.credit_bonus", 1.5)  # must be below 1
    with pytest.raises(ConfigError, match="invalid value"):
        override(config, "graph.p_min", 0.999)  # p_min must stay below p_max


def test_grid_expands_in_sorted_key_order():
    assert grid_configs({}) == [{}]
    combos = grid_configs({"b": [1, 2], "a": [3]})
    assert combos == [{"a": 3, "b": 1}, {"a": 3, "b": 2}]
    with pytest.raises(ConfigError):
        grid_configs({"a": []})


def test_rank_key_prefers_completions_then_deaths_then_frames():
    def s(c, d, f, col=None):
        return {"completions": c, "deaths_mean": d, "frames_to_complete_mean": f,
                "furthest_col_unfinished_mean": col}
    ranked = sorted([s(1, 0, 900), s(2, 3, 5000), s(1, 0, 500), s(1, 1, 100), s(0, 0, None, 40),
                     s(0, 0, None, 60)], key=rank_key)
    assert ranked == [s(2, 3, 5000), s(1, 0, 500), s(1, 0, 900), s(1, 1, 100), s(0, 0, None, 60),
                      s(0, 0, None, 40)]


def test_non_empty_out_is_refused(config, tmp_path):
    (tmp_path / "x").write_text("used")
    with pytest.raises(ConfigError, match="not empty"):
        run_sweep(config, {}, ("fixture_l1",), (0,), 50, "none", tmp_path, "fixture",
                  adapter_factory=fixture_factory)


@pytest.mark.parametrize("graph_mode", ["none", "cold", "warm"])
def test_fixture_sweep_is_reproducible(config, tmp_path, graph_mode):
    grid = {"planning.goal_timeout_frames": [100, 480]}
    outs = []
    for run in ("a", "b"):
        summary = run_sweep(config, grid, ("fixture_l1",), (0, 1), 60, graph_mode, tmp_path / run, "fixture",
                            train_episodes=1, adapter_factory=fixture_factory)
        outs.append((tmp_path / run / "episodes.jsonl").read_text(encoding="utf-8"))
    assert outs[0] == outs[1]
    rows = [json.loads(line) for line in outs[0].splitlines()]
    assert len(rows) == 4 and {r["graph"] for r in rows} == {graph_mode}
    assert {r["config_id"] for r in rows} == {"planning.goal_timeout_frames=100", "planning.goal_timeout_frames=480"}
    assert {s["scenario"] for s in summary} == {"fixture_l1", "*"}
    assert (tmp_path / "a" / "summary.csv").read_text(encoding="utf-8").startswith("config_id,")


def test_workers_do_not_change_the_result(config, tmp_path):
    grid = {"planning.no_progress_frames": [60, 180]}
    for workers, name in ((1, "serial"), (2, "pool")):
        run_sweep(config, grid, ("fixture_l1", "fixture_l1"), (0,), 60, "cold", tmp_path / name, "fixture",
                  adapter_factory=fixture_factory, workers=workers)
    serial, pool = ((tmp_path / n / "episodes.jsonl").read_text(encoding="utf-8") for n in ("serial", "pool"))
    assert serial == pool and len(serial.splitlines()) == 4
    assert not (tmp_path / "pool" / "episodes.partial.jsonl").exists()
