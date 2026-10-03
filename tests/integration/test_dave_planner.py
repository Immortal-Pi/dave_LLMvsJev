"""Phase 6 on the real game (mock planner and controllers): goals come from observed targets and
the planner is called only on documented triggers, identically for arms A and B.
Skipped when the deadly-dave bridge is not built."""

import pytest

from dave_agent.adapters.dave import BRIDGE_EXE, DaveBridgeAdapter
from dave_agent.control.goals import HARD_TRIGGERS, GoalManager
from dave_agent.memory.working import WorkingMemory
from dave_agent.models.mock import SeededMockController
from dave_agent.models.planner import RuleMockPlanner
from dave_agent.runner.episode import run_episode

from ..conftest import ROOT

DAVE_DIR = ROOT / "external" / "deadly-dave"
pytestmark = [
    pytest.mark.dave,
    pytest.mark.skipif(not (DAVE_DIR / BRIDGE_EXE).exists(), reason="deadly-dave bridge not built"),
]
DOCUMENTED = HARD_TRIGGERS | {"death", "stuck", "repeated_failures", "inventory_changed", "route_invalidated"}


def _play(config, label):
    adapter = DaveBridgeAdapter(DAVE_DIR)
    goals = GoalManager(RuleMockPlanner(), config.planning, config.models.max_retries)
    try:
        return run_episode(adapter, SeededMockController(0, label), config.skills.for_adapter("dave"),
                           config.skills.executor, WorkingMemory.from_config(config), "level1", 0,
                           max_frames=600, goals=goals)
    finally:
        adapter.close()


def test_level1_goals_from_observed_targets_on_documented_triggers(config):
    a, b = _play(config, "mock-llm"), _play(config, "mock-jev")
    assert a.planning[0].triggers == ("no_goal",)
    assert a.planning[0].chosen.startswith("collect:trophy:")  # the trophy is in the first view
    assert all(set(r.triggers) <= DOCUMENTED for r in a.planning)
    planner_calls = [c for c in a.model_calls if c.purpose == "planner"]
    assert len(planner_calls) == len(a.planning) <= config.planning.max_calls_per_episode
    assert [r.chosen for r in a.planning] == [r.chosen for r in b.planning]
    assert [r.request for r in a.planning] == [r.request for r in b.planning]
