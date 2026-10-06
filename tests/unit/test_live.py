"""The live viewer server (runner/live.py) and the episode loop's write-only on_event hook."""

import json
import threading
import time
import urllib.error
import urllib.request

import pytest

from dave_agent.adapters.fixture import FixtureAdapter
from dave_agent.config import ConfigError
from dave_agent.control.goals import GoalManager
from dave_agent.memory.working import WorkingMemory
from dave_agent.models.mock import SeededMockController
from dave_agent.models.planner import RuleMockPlanner
from dave_agent.runner.episode import run_episode
from dave_agent.runner.live import LiveHub, LiveServer, serve

from ..conftest import LEVELS


def episode(config, on_event=None):
    adapter = FixtureAdapter(LEVELS)
    goals = GoalManager(RuleMockPlanner(), config.planning, config.models.max_retries)
    try:
        return run_episode(adapter, SeededMockController(seed=3, label="m"), config.skills.for_adapter("fixture"),
                           config.skills.executor, WorkingMemory.from_config(config), "fixture_l1", 3,
                           max_frames=200, goals=goals, on_event=on_event)
    finally:
        adapter.close()


def test_on_event_reports_each_step_and_changes_nothing(config):
    seen = []
    watched = episode(config, lambda kind, data: seen.append((kind, json.loads(json.dumps(data)))))
    plain = episode(config)
    assert watched.decisions == plain.decisions and watched.candidate_sets == plain.candidate_sets
    kinds = [k for k, _ in seen]
    assert kinds[0] == "episode" and seen[0][1]["status"] == "started"
    assert kinds[-1] == "episode" and seen[-1][1]["status"] == "finished"
    assert kinds[1] == "plan" and seen[1][1]["chosen"] and "map" in seen[1][1]
    decisions = [d for k, d in seen if k == "decision"]
    outcomes = [d for k, d in seen if k == "outcome"]
    assert len(decisions) == len(outcomes) == len(watched.decisions)
    first = decisions[0]
    assert first["chosen"] in {c["id"] for c in first["candidates"]} and "goal" in first
    assert outcomes[0]["candidate_id"] == first["chosen"]
    # every decision is followed by its outcome before the next decision
    order = [k for k in kinds if k in ("decision", "outcome")]
    assert order == ["decision", "outcome"] * len(decisions)
    # a model is asked ("deciding") before every decision that is not forced
    assert kinds.count("deciding") == sum(not d.forced for d in watched.decisions)


def test_a_failing_viewer_never_stops_the_episode(config):
    def broken(kind, data):
        raise RuntimeError("viewer gone")
    assert episode(config, broken).decisions == episode(config).decisions


def test_hub_replays_the_current_run_and_waits_for_new_events():
    hub = LiveHub()
    hub.publish("a", {"n": 1})
    hub.reset()
    hub.publish("b", {"n": 2})
    assert [e["type"] for e in hub.events_after(0, 0)] == ["reset", "b"]  # the previous run is gone
    last = hub.events_after(0, 0)[-1]["seq"]
    assert hub.events_after(last, 0.01) == []
    threading.Timer(0.05, hub.publish, ("c", {})).start()
    assert [e["type"] for e in hub.events_after(last, 2)] == ["c"]


def server(config, tmp_path, **kw):
    return LiveServer(config, "fixture", tmp_path / "live.sqlite", tick_ms=kw.pop("tick_ms", 0), **kw)


def wait_idle(srv, timeout=30):
    deadline = time.monotonic() + timeout
    while srv.running and time.monotonic() < deadline:
        time.sleep(0.02)
    assert not srv.running


def test_paid_runs_need_allow_paid_and_runs_do_not_overlap(config, tmp_path):
    srv = server(config, tmp_path, tick_ms=50)
    with pytest.raises(ConfigError, match="--allow-paid"):
        srv.start({"scenario": "fixture_l1", "arm": "B", "tactical": "live"})
    with pytest.raises(ConfigError, match="unknown level"):
        srv.start({"scenario": "level9", "arm": "B"})
    info = srv.start({"scenario": "fixture_l1", "arm": "B"})
    assert info["mode"] == "mock" and info["run_id"].startswith("live-")
    with pytest.raises(ConfigError, match="still going"):
        srv.start({"scenario": "fixture_l1", "arm": "B"})
    assert srv.stop()
    wait_idle(srv)
    kinds = [e["type"] for e in srv.hub.events_after(0, 0)]
    assert kinds[:2] == ["reset", "run"] and "stopped" in json.dumps(srv.hub.events_after(0, 0)) \
        and kinds[-1] == "idle"


def test_a_finished_run_is_recorded_and_summarised(config, tmp_path):
    srv = server(config, tmp_path)
    srv.start({"scenario": "fixture_l1", "arm": "C"})  # a graph arm saves its store too
    wait_idle(srv)
    events = srv.hub.events_after(0, 0)
    summary = next(e["data"] for e in events if e["type"] == "summary")
    assert summary["run_id"] == srv.run_id and summary["graph"] is not None
    assert any(e["type"] == "decision" for e in events)


def test_http_routes(config, tmp_path):
    srv = server(config, tmp_path)
    address = {}
    ready = threading.Event()
    thread = threading.Thread(target=serve, args=(srv, "127.0.0.1", 0, lambda a: (address.update(a=a), ready.set())),
                              daemon=True)
    thread.start()
    assert ready.wait(5)
    base = f"http://127.0.0.1:{address['a'][1]}"
    status = json.load(urllib.request.urlopen(f"{base}/status"))
    assert status["levels"] == ["fixture_l1"] and not status["running"] and "B" in status["arms"]
    request = urllib.request.Request(f"{base}/start", data=json.dumps({"scenario": "fixture_l1", "arm": "B"}).encode(),
                                     headers={"Content-Type": "application/json"}, method="POST")
    assert json.load(urllib.request.urlopen(request))["mode"] == "mock"
    stream = urllib.request.urlopen(f"{base}/events", timeout=30)
    types = []
    while "idle" not in types:
        line = stream.readline().decode()
        if line.startswith("event: "):
            types.append(line[7:].strip())
    assert types[:2] == ["reset", "run"] and "decision" in types and "plan" in types
    with pytest.raises(urllib.error.HTTPError) as err:
        urllib.request.urlopen(urllib.request.Request(f"{base}/start", data=b'{"arm": "Z"}', method="POST"))
    assert err.value.code == 400
    assert urllib.request.urlopen(f"{base}/frame").status == 204  # the fixture has no frames


def test_a_graph_arm_streams_its_graph_after_every_skill(config):
    from dave_agent.memory.graph import GraphStore
    adapter = FixtureAdapter(LEVELS)
    graph = GraphStore("fixture", adapter.capabilities().build_id, "local_observed")
    seen = []
    goals = GoalManager(RuleMockPlanner(), config.planning, config.models.max_retries)
    try:
        result = run_episode(adapter, SeededMockController(seed=3, label="m"), config.skills.for_adapter("fixture"),
                             config.skills.executor, WorkingMemory.from_config(config), "fixture_l1", 3,
                             max_frames=200, graph=graph, goals=goals,
                             on_event=lambda kind, data: seen.append((kind, json.loads(json.dumps(data)))))
    finally:
        adapter.close()
    graphs = [d for k, d in seen if k == "graph"]
    assert len(graphs) == 1 + len(result.executions)  # once at the start, then after every skill
    assert graphs[0]["source"] == "learning" and "last" not in graphs[0]
    assert all(g["last"]["recorded"] for g in graphs[1:])
    final = graphs[-1]["graph"]
    assert final["counts"] == graph.get(final["level_id"]).counts()
    learned = [g["last"]["edge"] for g in graphs[1:] if g["last"]["recorded"] == "success"]
    edges = {(e["source"], e["target"], e["key"]) for e in final["edges"]}
    assert all(tuple(e) in edges for e in learned if e)


def test_a_run_without_a_graph_streams_no_graph(config):
    seen = []
    episode(config, lambda kind, data: seen.append(kind))
    assert "graph" not in seen


def test_hub_keeps_only_the_newest_graph_snapshot():
    hub = LiveHub()
    hub.publish("graph", {"n": 1})
    hub.publish("decision", {"d": 1})
    hub.publish("graph", {"n": 2})
    events = hub.events_after(0, 0)
    assert [e["type"] for e in events] == ["decision", "graph"] and events[-1]["data"] == {"n": 2}
