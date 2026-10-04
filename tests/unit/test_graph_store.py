"""One learned graph per level (memory/graph.py GraphStore, memory/persistence.py stores)."""

import json

import pytest

from dave_agent.memory.graph import GraphStore, WorldGraph
from dave_agent.memory.persistence import GraphCheckpointError, load_store, save_checkpoint, save_store, \
    store_exists
from dave_agent.memory.routes import find_route

from .test_graph import obs, run


def store():
    return GraphStore("fixture", "test", "local_observed")


def learned():
    s = store()
    s.record_execution(obs(player=(3, 3)), run("jump_right", obs(player=(6, 1), oid=2)), "r#1")
    s.record_execution(obs(player=(3, 3), level="L2"), run("jump_right", obs(player=(6, 1), oid=3, level="L2")), "r#2")
    return s


def test_each_level_has_its_own_graph():
    s = learned()
    assert sorted(s.levels) == ["L1", "L2"]
    for level, graph in s.levels.items():
        assert graph.level_id == level
        assert {d["level_id"] for _, d in graph.g.nodes(data=True)} == {level}
        assert graph.g.number_of_edges() == 1
    assert s.counts()["edges"] == 2 and s.counts()["levels"] == ["L1", "L2"]
    assert s.skill_evidence(obs(player=(3, 3), level="L3")) == {}  # an unseen level has no evidence


def test_a_level_graph_refuses_another_level():
    with pytest.raises(ValueError, match="cannot observe"):
        store().for_level("L1").observe(obs(level="L2"))


def test_a_skill_that_changes_level_is_never_an_edge():
    s = store()
    assert s.record_execution(obs(player=(3, 3)), run("jump_right", obs(player=(6, 1), oid=2, level="L2")),
                              "r#1") == "level_changed"
    assert s.counts()["edges"] == 0 and sorted(s.levels) == ["L1", "L2"]


def test_frontier_and_routes_stay_on_the_level(config):
    s = learned()
    l1 = s.levels["L1"]
    route = find_route(l1, "L1:r3:c3", "L2:r1:c6", {}, config.graph)
    assert route.status == "unreachable" and all(n.startswith("L1:") for n in route.frontier)


def test_store_round_trip_is_one_file_per_level(tmp_path):
    s = learned()
    s.add_lineage("run1", "run1/e", "C", "L1")
    directory = save_store(s, tmp_path / "arm-C" / "fixture.json")
    assert directory == tmp_path / "arm-C" / "fixture" and store_exists(tmp_path / "arm-C" / "fixture.json")
    assert sorted(p.name for p in directory.glob("*.json")) == ["L1.json", "L2.json"]
    loaded = load_store(directory, "fixture", "test", "local_observed")
    assert sorted(loaded.levels) == ["L1", "L2"] and loaded.counts() == s.counts()
    assert [e["run_id"] for e in loaded.lineage] == ["run1"]
    with pytest.raises(GraphCheckpointError, match="not found"):
        load_store(tmp_path / "missing.json")


def test_a_legacy_combined_checkpoint_is_split_by_level(tmp_path):
    legacy = WorldGraph("fixture", "test", "local_observed")  # one graph for every level, as before
    legacy.record_execution(obs(player=(3, 3)), run("jump_right", obs(player=(6, 1), oid=2)), "r#1")
    legacy.record_execution(obs(player=(3, 3), level="L2"),
                            run("jump_right", obs(player=(6, 1), oid=3, level="L2")), "r#2")
    legacy.add_lineage("old", "old/e", "C", "L1")
    save_checkpoint(legacy, tmp_path / "dave.json")
    s = load_store(tmp_path / "dave.json", "fixture", "test", "local_observed")
    assert sorted(s.levels) == ["L1", "L2"] and s.counts()["edges"] == 2
    assert s.levels["L2"].level_id == "L2" and [e["run_id"] for e in s.lineage] == ["old"]
    directory = save_store(s, tmp_path / "dave.json")  # written back as a directory
    assert json.loads((directory / "L2.json").read_text(encoding="utf-8"))["level_id"] == "L2"


def test_a_death_leaves_an_incident_with_its_cause():
    s = store()
    start = obs(player=(3, 3))
    end = obs(player=(2, 3), oid=2, state="burning")  # standing in the fire at (2, 3)
    s.record_execution(start, run("move_left_1", end, outcome="interrupted", reason="hazard_contact", death=True),
                       "r#1")
    (node,) = [d for _, d in s.levels["L1"].g.nodes(data=True) if d["incidents"]]
    assert node["incidents"] == [{"skill": "move_left_1", "cause": "fire", "tile": [2, 3], "ref": "r#1"}]
