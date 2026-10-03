"""Phase 5 on the real game: graph learned from local observations only, deterministic checkpoints.
Skipped when the deadly-dave bridge is not built."""

import pytest

from dave_agent.adapters.dave import BRIDGE_EXE, DaveBridgeAdapter
from dave_agent.memory.graph import WorldGraph
from dave_agent.memory.persistence import load_checkpoint, save_checkpoint
from dave_agent.memory.working import WorkingMemory
from dave_agent.models.mock import SeededMockController
from dave_agent.runner.episode import run_episode

from ..conftest import ROOT

DAVE_DIR = ROOT / "external" / "deadly-dave"
pytestmark = [
    pytest.mark.dave,
    pytest.mark.skipif(not (DAVE_DIR / BRIDGE_EXE).exists(), reason="deadly-dave bridge not built"),
]


class RegionSpy:
    """Delegates to the adapter and records every observed column."""

    def __init__(self, inner):
        self.inner, self.cols = inner, set()

    def __getattr__(self, name):
        return getattr(self.inner, name)

    def _seen(self, obs):
        self.cols |= set(range(obs.region.min.col, obs.region.max.col + 1))
        return obs

    def reset(self, scenario_id, seed):
        return self._seen(self.inner.reset(scenario_id, seed))

    def step(self, buttons, frames):
        result = self.inner.step(buttons, frames)
        self._seen(result.observation)
        return result


def _learn(config, seed):
    adapter = RegionSpy(DaveBridgeAdapter(DAVE_DIR))
    caps = adapter.capabilities()
    graph = WorldGraph("dave", caps.build_id, "local_observed")
    try:
        run_episode(adapter, SeededMockController(seed, "mock-jev"), config.skills.for_adapter("dave"),
                    config.skills.executor, WorkingMemory.from_config(config), "level1", seed, 600, graph=graph)
    finally:
        adapter.close()
    graph.add_lineage("fixed-run", "fixed-run/dave-level1", "C", "level1")
    return graph, adapter.cols


def test_dave_graph_local_only_and_deterministic(config, tmp_path):
    graph, cols = _learn(config, 1)
    assert graph.g.number_of_nodes() > 0
    for _, d in graph.g.nodes(data=True):
        assert d["level_id"] == "level1" and set(range(d["col_min"], d["col_max"] + 1)) <= cols
    for u, v, d in graph.g.edges(data=True):
        assert d["successes"] >= 1 and u != v  # edges only from observed successful transitions
    first = save_checkpoint(graph, tmp_path / "a.json").read_bytes()
    second = save_checkpoint(_learn(config, 1)[0], tmp_path / "b.json").read_bytes()
    assert first == second
    assert load_checkpoint(tmp_path / "a.json", "dave", graph.build_id, "local_observed").counts() == graph.counts()
