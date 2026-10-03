"""Phase 3 acceptance: LLM and Jev arms receive exactly the same candidate lists."""

from dave_agent.adapters.fixture import FixtureAdapter
from dave_agent.config import SkillSpec
from dave_agent.models.mock import SeededMockController
from dave_agent.runner.episode import run_episode

from ..conftest import LEVELS


class SpyController:
    """Records exactly what a controller is shown, then delegates."""

    def __init__(self, inner):
        self.inner, self.provider, self.model = inner, inner.provider, inner.model
        self.seen = []

    def decide(self, observation, goal, candidates):
        self.seen.append((observation.model_dump(exclude={"episode_id"}), [c.model_dump() for c in candidates]))
        return self.inner.decide(observation, goal, candidates)


def _run(controller, config, seed=0):
    adapter = FixtureAdapter(LEVELS)
    try:
        return run_episode(adapter, controller, config.skills.for_adapter("fixture"), config.skills.executor,
                           "fixture_l1", seed, max_frames=200)
    finally:
        adapter.close()


def test_llm_and_jev_arms_see_identical_candidates(config):
    llm = SpyController(SeededMockController(seed=3, label="mock-llm"))
    jev = SpyController(SeededMockController(seed=3, label="mock-jev"))
    a, b = _run(llm, config, 3), _run(jev, config, 3)
    assert llm.seen and llm.seen == jev.seen
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
                             "fixture_l1", 0, max_frames=20)
    finally:
        adapter.close()
    assert result.model_calls == [] and all(d.forced for d in result.decisions)
    assert result.outcome == "truncated" and result.frames == 20
