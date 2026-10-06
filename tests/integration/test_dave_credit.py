"""Level 3 on the real game: the route notes lead Dave to the gun he can only take in flight, and
the learned graph credits the moves that got it. Skipped when the deadly-dave bridge is not built."""

import pytest

from dave_agent.adapters.dave import BRIDGE_EXE, DaveBridgeAdapter
from dave_agent.control.goals import GoalManager
from dave_agent.memory.graph import GraphStore
from dave_agent.memory.working import WorkingMemory
from dave_agent.models.planner import RuleMockPlanner
from dave_agent.runner.episode import run_episode
from dave_agent.schemas import Decision

from ..conftest import ROOT

DAVE_DIR = ROOT / "external" / "deadly-dave"
pytestmark = [
    pytest.mark.dave,
    pytest.mark.skipif(not (DAVE_DIR / BRIDGE_EXE).exists(), reason="deadly-dave bridge not built"),
]
GUN = "collect:gun:c10:r4"


class RouteFollower:
    """Takes the first ``route:`` option, else waits (scripts/follow_route.py)."""

    provider, model = "scripted", "route-follower"

    def __init__(self):
        self.chosen: list[tuple[str, str]] = []

    def decide(self, observation, goal, candidates, memory):
        c = next((c for c in candidates if c.description.startswith("route:")), None) \
            or next((c for c in candidates if c.skill == "wait_short"), candidates[0])
        self.chosen.append((c.skill, c.description.split(";")[0]))
        return Decision(candidate_id=c.candidate_id, observation_id=observation.observation_id), ()


def test_level3_gun_is_taken_in_flight_and_the_moves_are_credited(config):
    adapter = DaveBridgeAdapter(DAVE_DIR)
    caps = adapter.capabilities()
    store = GraphStore(caps.adapter, caps.build_id, config.environment.observation_policy)
    goals = GoalManager(RuleMockPlanner(), config.planning, config.models.max_retries, store, config.graph,
                        reach=config.skills.reach["dave"], threats=config.skills.executor.threats)
    follower = RouteFollower()
    try:
        result = run_episode(adapter, follower, config.skills.for_adapter("dave"), config.skills.executor,
                             WorkingMemory.from_config(config), "level3", 0, 400, graph=store, goals=goals)
    finally:
        adapter.close()
    assert result.executions[-1].observation.inventory["gun"] == 1
    assert not any(e.event_type == "death" for e in result.events)
    # From the start: the 4-tile jump to (6,6), then the long jump through the gun (before the
    # 4- and 5-tile jumps: a long jump to (8,6) and two walks back to the take-off).
    assert [s for s, _ in follower.chosen[:2]] == ["jump_right_4", "jump_right"]
    assert follower.chosen[1][1] == "route: picks up the gun on the way"
    assert any(e.event_type == "goal_achieved" and e.payload["target_ref"] == GUN for e in result.events)
    credit = {key: rec for _, d in store.levels["level3"].g.nodes(data=True)
              for key, rec in d.get("credit", {}).items() if key.endswith(f"|{GUN}")}
    assert credit["jump_right|" + GUN]["reached"] >= 1
