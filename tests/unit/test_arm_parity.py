"""LLM and Jev arms receive exactly the same candidates, observations and working memory,
and their episode logs are identical apart from arm and controller labels."""

from dave_agent.adapters.fixture import FixtureAdapter
from dave_agent.config import SkillSpec
from dave_agent.memory.episodes import EpisodeStore
from dave_agent.memory.working import WorkingMemory
from dave_agent.models.mock import SeededMockController
from dave_agent.models.planner import RuleMockPlanner
from dave_agent.runner.episode import run_episode

from ..conftest import LEVELS


class SpyController:
    """Records exactly what a controller is shown, then delegates."""

    def __init__(self, inner):
        self.inner, self.provider, self.model = inner, inner.provider, inner.model
        self.seen = []

    def decide(self, observation, goal, candidates, memory):
        self.seen.append((observation.model_dump(exclude={"episode_id"}), [c.model_dump() for c in candidates],
                          memory.model_dump()))
        return self.inner.decide(observation, goal, candidates, memory)


def _run(controller, config, seed=0, recorder=None):
    adapter = FixtureAdapter(LEVELS)
    try:
        return run_episode(adapter, controller, config.skills.for_adapter("fixture"), config.skills.executor,
                           WorkingMemory.from_config(config), "fixture_l1", seed, max_frames=200, recorder=recorder)
    finally:
        adapter.close()


def test_llm_and_jev_arms_see_identical_candidates_and_memory(config):
    llm = SpyController(SeededMockController(seed=3, label="mock-llm"))
    jev = SpyController(SeededMockController(seed=3, label="mock-jev"))
    a, b = _run(llm, config, 3), _run(jev, config, 3)
    assert llm.seen and llm.seen == jev.seen
    assert any(seen[2]["recent"] for seen in llm.seen)  # memory is populated, not trivially equal
    assert [c.digest for c in a.candidate_sets] == [c.digest for c in b.candidate_sets]


def test_candidate_sets_recorded_per_decision(config):
    result = _run(SeededMockController(seed=0, label="m"), config)
    assert len(result.candidate_sets) == len(result.decisions) == len(result.executions)
    assert all(rec.candidate_ids for rec in result.candidate_sets)


def test_single_legal_candidate_is_forced_without_model_call(config):
    only_wait = (SkillSpec.model_validate({"name": "wait", "kind": "single", "phases": [{"buttons": [], "ticks": 5}]}),)
    adapter = FixtureAdapter(LEVELS)
    try:
        result = run_episode(adapter, SeededMockController(0, "m"), only_wait, config.skills.executor,
                             WorkingMemory.from_config(config), "fixture_l1", 0, max_frames=20)
    finally:
        adapter.close()
    assert result.model_calls == [] and all(d.forced for d in result.decisions)
    assert result.outcome == "truncated" and result.frames == 20


# Fields that legitimately differ between arms: labels and wall-clock times.
_ARM_LABELS = {"run_id", "episode_key", "arm", "controller", "model", "created_at", "started_at", "ended_at"}


def test_episode_logging_identical_across_arms(config, tmp_path):
    exports = []
    for arm, label in (("A", "mock-llm"), ("B", "mock-jev")):
        with EpisodeStore(tmp_path / f"{arm}.sqlite") as store:
            store.create_run(f"run-{arm}", mode="mock", command="test", config_json="{}")
            recorder = store.recorder(f"run-{arm}", arm, label, "fixture_l1", 3, 50)
            _run(SeededMockController(seed=3, label=label), config, 3, recorder)
            exports.append([{k: v for k, v in r.items() if k not in _ARM_LABELS} for r in store.iter_records()])
    assert len(exports[0]) > 50 and exports[0] == exports[1]


class SpyPlanner(RuleMockPlanner):
    def __init__(self):
        self.requests = []

    def propose(self, request, feedback=None):
        self.requests.append(request)
        return super().propose(request, feedback)


def _planned(config, label, graph=None, seed=3):
    """One fixture episode under the goal manager. Same planner code and settings for every arm."""
    from dave_agent.control.goals import GoalManager

    controller, planner = SpyController(SeededMockController(seed=seed, label=label)), SpyPlanner()
    cfg = config.planning.model_copy(update={"min_frames_between_calls": 0})
    goals = GoalManager(planner, cfg, config.models.max_retries, graph, config.graph if graph else None)
    adapter = FixtureAdapter(LEVELS)
    try:
        result = run_episode(adapter, controller, config.skills.for_adapter("fixture"), config.skills.executor,
                             WorkingMemory.from_config(config), "fixture_l1", seed, max_frames=200,
                             graph=graph, goals=goals)
    finally:
        adapter.close()
    return result, controller, planner


def test_llm_and_jev_arms_share_planner_requests_goals_and_inputs(config):
    a, llm, plan_a = _planned(config, "mock-llm")
    b, jev, plan_b = _planned(config, "mock-jev")
    assert plan_a.requests and plan_a.requests == plan_b.requests
    assert [r.chosen for r in a.planning] == [r.chosen for r in b.planning]
    assert any(seen[2]["goal"] for seen in llm.seen) and llm.seen == jev.seen
    assert [d.goal_id for d in a.decisions] == [d.goal_id for d in b.decisions]


def test_graph_arm_differs_only_by_routes_and_waypoint(config):
    from dave_agent.memory.graph import WorldGraph

    plain, plain_ctl, plain_plan = _planned(config, "m")
    routed, routed_ctl, routed_plan = _planned(config, "m", WorldGraph("fixture", "fixture-platformer-v1",
                                                                       "local_observed"))
    strip = {"graph_routes": True, "candidates": {"__all__": {"route"}}}
    assert [r.model_dump(exclude=strip) for r in plain_plan.requests] == \
        [r.model_dump(exclude=strip) for r in routed_plan.requests]
    assert all(c.route is not None for r in routed_plan.requests for c in r.candidates)

    def without_waypoint(seen):
        return [(o, c, {**m, "goal": m["goal"] and {**m["goal"], "next_waypoint": None}}) for o, c, m in seen]

    assert without_waypoint(plain_ctl.seen) == without_waypoint(routed_ctl.seen)


def test_graph_learning_alone_adds_no_controller_context(config):
    """Without the goal manager, a graph-enabled run's controller sees exactly what a plain run's
    controller sees: learned routes reach decisions only through the goal manager's waypoint."""
    from dave_agent.memory.graph import WorldGraph

    plain = SpyController(SeededMockController(seed=3, label="m"))
    learning = SpyController(SeededMockController(seed=3, label="m"))
    _run(plain, config, 3)
    adapter = FixtureAdapter(LEVELS)
    graph = WorldGraph("fixture", "fixture-platformer-v1", "local_observed")
    try:
        run_episode(adapter, learning, config.skills.for_adapter("fixture"), config.skills.executor,
                    WorkingMemory.from_config(config), "fixture_l1", 3, max_frames=200, graph=graph)
    finally:
        adapter.close()
    assert graph.g.number_of_nodes() > 0 and plain.seen == learning.seen
