"""Phase 5: graph checkpoints (round trips, compatibility, atomic writes) and a learned route surviving restart."""

import json
import os

import pytest
import yaml

from dave_agent.adapters.fixture import FixtureAdapter
from dave_agent.memory.graph import WorldGraph
from dave_agent.memory.persistence import (
    GraphCheckpointError,
    export_yaml,
    from_checkpoint,
    load_checkpoint,
    save_checkpoint,
    to_checkpoint,
)
from dave_agent.memory.routes import find_route
from dave_agent.memory.working import WorkingMemory
from dave_agent.runner.episode import run_episode
from dave_agent.schemas import Decision, ModelCallRecord

from ..conftest import LEVELS
from .test_graph import graph, obs, run


def learned():
    g = graph()
    start = obs(player=(5, 3))
    g.record_execution(start, run("jump_right", obs(player=(6, 1), oid=2)), "r#1")
    g.record_execution(start, run("jump_left", obs(player=(1, 3), oid=3), "interrupted", "death", death=True), "r#2")
    g.suggest("L1", 40, 2, source="planner")
    g.add_lineage("run1", "run1/e1", "C", "fixture_l1")
    return g


def test_json_round_trip_is_exact_and_yaml_export_loads(tmp_path):
    g = learned()
    assert to_checkpoint(from_checkpoint(to_checkpoint(g))) == to_checkpoint(g)
    path = save_checkpoint(g, tmp_path / "g.json")
    loaded = load_checkpoint(path, "fixture", "test", "local_observed")
    again = to_checkpoint(loaded)
    assert again.pop("parent_sha256") is not None  # lineage points at the file it was loaded from
    expected = to_checkpoint(g)
    expected.pop("parent_sha256")
    assert again == expected
    exported = yaml.safe_load(export_yaml(loaded, tmp_path / "g.yaml").read_text(encoding="utf-8"))
    assert exported["counts"] == g.counts() and len(exported["nodes"]) == g.g.number_of_nodes()


@pytest.mark.parametrize(("field", "value", "match"), [
    ("build_id", "other-build", "build_id"),
    ("observation_policy", "oracle", "observation_policy"),
    ("adapter", "dave", "adapter"),
    ("graph_schema_version", 99, "schema version 99"),
])
def test_incompatible_checkpoints_rejected(tmp_path, field, value, match):
    path = save_checkpoint(learned(), tmp_path / "g.json")
    data = json.loads(path.read_text(encoding="utf-8"))
    data[field] = value
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(GraphCheckpointError, match=match):
        load_checkpoint(path, "fixture", "test", "local_observed")


def test_corrupt_or_missing_checkpoint_is_actionable(tmp_path):
    with pytest.raises(GraphCheckpointError, match="not found"):
        load_checkpoint(tmp_path / "none.json")
    (tmp_path / "bad.json").write_text("{trunc", encoding="utf-8")
    with pytest.raises(GraphCheckpointError, match="bad.json.bak"):
        load_checkpoint(tmp_path / "bad.json")


def test_interrupted_save_keeps_previous_checkpoint(tmp_path, monkeypatch):
    path = save_checkpoint(learned(), tmp_path / "g.json")
    before = path.read_bytes()
    bigger = learned()
    bigger.record_execution(obs(player=(6, 1)), run("move_left_1", obs(player=(5, 3), oid=4)), "r#3")

    def crash(fd):
        raise OSError("disk full")

    monkeypatch.setattr(os, "fsync", crash)
    with pytest.raises(OSError):
        save_checkpoint(bigger, path)
    monkeypatch.undo()
    assert path.read_bytes() == before
    assert load_checkpoint(path).counts() == learned().counts()
    save_checkpoint(bigger, path)
    assert (tmp_path / "g.json.bak").read_bytes() == before
    assert load_checkpoint(path).counts()["edges"] == 2


class Scripted:
    """Chooses catalog skills by name, in order (deterministic route learning)."""

    provider, model = "scripted", "scripted"

    def __init__(self, skills):
        self.skills = list(skills)

    def decide(self, observation, goal, candidates, memory):
        name = self.skills.pop(0) if self.skills else "wait"
        chosen = next(c for c in candidates if c.skill == name)
        return (Decision(candidate_id=chosen.candidate_id, observation_id=observation.observation_id),
                ModelCallRecord(provider="scripted", model="scripted", purpose="tactical", latency_ms=0.0,
                                status="ok"))


def test_route_learned_in_fixture_episode_survives_restart(config, tmp_path):
    adapter = FixtureAdapter(LEVELS)
    g = WorldGraph("fixture", "fixture-platformer-v1", "local_observed")
    try:
        # Floor (1,4) -> (3,4), then jump over the fire onto the ledge at row 2.
        run_episode(adapter, Scripted(["move_right", "move_right", "jump_right"]),
                    config.skills.for_adapter("fixture"), config.skills.executor, WorkingMemory.from_config(config),
                    "fixture_l1", 0, max_frames=5, graph=g)
    finally:
        adapter.close()
    g.add_lineage("r1", "r1/fixture_l1-s0-e1", "C", "fixture_l1")
    path = save_checkpoint(g, tmp_path / "arm-C" / "fixture.json")

    restarted = load_checkpoint(path, "fixture", "fixture-platformer-v1", "local_observed")
    route = find_route(restarted, "fixture_l1:r4:c1", "fixture_l1:r2:c6", {"trophy": 0}, config.graph)
    assert route.status == "found" and [s.skill for s in route.steps] == ["jump_right"]
    assert route == find_route(g, "fixture_l1:r4:c1", "fixture_l1:r2:c6", {"trophy": 0}, config.graph)
    assert restarted.lineage == [{"run_id": "r1", "episode_key": "r1/fixture_l1-s0-e1", "arm": "C",
                                  "scenario_id": "fixture_l1"}]
    # Nothing was learned about the way back.
    assert find_route(restarted, "fixture_l1:r2:c6", "fixture_l1:r4:c1", {}, config.graph).status == "unreachable"
