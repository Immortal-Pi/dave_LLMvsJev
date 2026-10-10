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


def test_rekey_ties_a_store_to_the_current_build_and_keeps_what_it_learned(tmp_path, capsys):
    from dave_agent.adapters.fixture import BUILD_ID
    from dave_agent.cli import main

    path = save_store(learned(), tmp_path / "arm-C")
    before = {f.name: json.loads(f.read_text()) for f in path.glob("*.json")}
    assert main(["graph", "--checkpoint", str(path), "--rekey", "fixture"]) == 0
    assert json.loads(capsys.readouterr().out)["build_id"] == BUILD_ID
    for name, old in before.items():
        new = json.loads((path / name).read_text())
        assert new["build_id"] == BUILD_ID and old["build_id"] == "test"
        same = lambda c: {k: v for k, v in c.items() if k not in ("build_id", "parent_sha256")}  # noqa: E731
        assert same(new) == same(old)  # parent_sha256: the pre-rekey file, as on every save
        assert json.loads((path / f"{name}.prekey").read_text()) == old
    load_store(path, "fixture", BUILD_ID, "local_observed")


def test_rekey_refuses_another_adapter(tmp_path):
    from dave_agent.cli import main

    s = GraphStore("dave", "test", "local_observed")
    s.for_level("L1")
    path = save_store(s, tmp_path / "arm-C")
    assert main(["graph", "--checkpoint", str(path), "--rekey", "fixture"]) == 2
    assert json.loads((path / "L1.json").read_text())["build_id"] == "test"


def test_view_is_json_ready_and_reads_only():
    import copy

    from dave_agent.memory.graph import success_probability
    s = learned()
    graph = s.levels["L1"]
    before = copy.deepcopy((dict(graph.g.nodes(data=True)), list(graph.g.edges(keys=True, data=True))))
    view = json.loads(json.dumps(graph.view()))
    assert (dict(graph.g.nodes(data=True)), list(graph.g.edges(keys=True, data=True))) == before
    assert view["level_id"] == "L1" and view["counts"] == graph.counts()
    assert len(view["nodes"]) == graph.g.number_of_nodes()
    (edge,) = view["edges"]
    (_, _, data), = graph.g.edges(data=True)
    assert edge["skill"] == "jump_right" and edge["attempts"] == 1
    assert edge["p"] == round(success_probability(data), 3)


# -- paused and real-time play learn separately -------------------------------------------------
def test_a_store_keeps_its_execution_mode(tmp_path):
    realtime = GraphStore("fixture", "test", "local_observed", execution_mode="real_time")
    realtime.record_execution(obs(player=(3, 3)), run("jump_right", obs(player=(6, 1), oid=2)), "r#1")
    assert realtime.for_level("L1").execution_mode == "real_time"
    directory = save_store(realtime, tmp_path / "rt.json")
    assert json.loads((directory / "L1.json").read_text(encoding="utf-8"))["execution_mode"] == "real_time"
    assert load_store(directory, execution_mode="real_time").execution_mode == "real_time"
    with pytest.raises(GraphCheckpointError, match="execution_mode is 'real_time' but this run uses 'paused_step'"):
        load_store(directory, "fixture", "test", "local_observed", "paused_step")


def test_a_checkpoint_from_before_the_field_is_paused(tmp_path):
    directory = save_store(learned(), tmp_path / "old.json")
    for file in directory.glob("*.json"):
        data = json.loads(file.read_text(encoding="utf-8"))
        del data["execution_mode"]
        file.write_text(json.dumps(data), encoding="utf-8")
    assert load_store(directory, execution_mode="paused_step").execution_mode == "paused_step"
    with pytest.raises(GraphCheckpointError, match="execution_mode"):
        load_store(directory, execution_mode="real_time")


def test_paused_and_real_time_play_use_separate_stores(config, adapter, tmp_path):
    from dave_agent.runner.session import graph_store_path, open_graph

    assert graph_store_path(tmp_path, "C", "dave", "paused_step") == tmp_path / "graphs" / "arm-C" / "dave.json"
    assert graph_store_path(tmp_path, "C", "dave", "real_time") == tmp_path / "graphs" / "arm-C" / "dave-realtime.json"
    paused = config.model_copy(update={"memory": config.memory.model_copy(update={
        "episode_store": tmp_path / "events.sqlite", "graph_checkpoint": None})})
    realtime = paused.model_copy(update={"environment": paused.environment.model_copy(
        update={"execution_mode": "real_time"})})
    graph, directory, _ = open_graph(realtime, "C", adapter, "fixture")
    assert directory == tmp_path / "graphs" / "arm-C" / "fixture-realtime" and graph.execution_mode == "real_time"
    graph.observe(obs())
    save_store(graph, directory)
    graph, directory, _ = open_graph(paused, "C", adapter, "fixture")  # the paused store: still new and empty
    assert directory == tmp_path / "graphs" / "arm-C" / "fixture" and graph.levels == {}
    assert graph.execution_mode == "paused_step"
    graph, _, _ = open_graph(realtime, "C", adapter, "fixture")  # the real-time store kept what it learned
    assert sorted(graph.levels) == ["L1"]
