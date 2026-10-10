"""Real-time mode (runner/episode.py ``realtime``): the game runs on while models think."""

import time

import pytest

from dave_agent.adapters.fixture import FixtureAdapter
from dave_agent.config import ConfigError
from dave_agent.control.goals import GoalManager
from dave_agent.control.skills import generate_candidates
from dave_agent.memory.working import WorkingMemory
from dave_agent.models.mock import SeededMockController
from dave_agent.models.planner import RuleMockPlanner
from dave_agent.runner.episode import _still_valid, run_episode
from dave_agent.runner.inspect import InspectError, load_recorded
from dave_agent.runner.live import LiveServer
from dave_agent.schemas import Event

from ..conftest import LEVELS
from .test_live import wait_idle


class SlowController(SeededMockController):
    def decide(self, *args, **kwargs):
        time.sleep(0.03)
        return super().decide(*args, **kwargs)


class PacedAdapter:
    """The fixture adapter at a few ms per tick, logging the keys of every step."""

    def __init__(self) -> None:
        self.inner, self.pressed = FixtureAdapter(LEVELS), []

    def __getattr__(self, name):
        return getattr(self.inner, name)

    def step(self, buttons, frames=1):
        self.pressed.append(buttons)
        time.sleep(0.002)
        return self.inner.step(buttons, frames)


def episode(config, realtime: bool):
    adapter = PacedAdapter()
    goals = GoalManager(RuleMockPlanner(), config.planning, config.models.max_retries)
    try:
        result = run_episode(adapter, SlowController(seed=3, label="m"), config.skills.for_adapter("fixture"),
                             config.skills.executor, WorkingMemory.from_config(config), "fixture_l1", 3,
                             max_frames=200, goals=goals, realtime=realtime)
    finally:
        adapter.close()
    return result, adapter.pressed


def test_the_game_runs_on_with_no_keys_while_a_model_decides(config):
    result, pressed = episode(config, realtime=True)
    waits = [e.payload for e in result.events if e.event_type in ("decision_latency", "decision_stale")]
    assert waits and all(w["wait_ticks"] > 0 and w["start_frame"] > w["decided_frame"] for w in waits)
    # every frame is either a skill's input or an idle tick with no keys
    skill_frames = sum(r.frames for r in result.executions)
    idle = sum(w["wait_ticks"] for w in waits)
    assert len(pressed) == skill_frames + idle == result.frames
    assert sum(1 for b in pressed if not b) >= idle


def test_paused_mode_takes_no_idle_ticks(config):
    result, pressed = episode(config, realtime=False)
    assert not any(e.event_type in ("decision_latency", "decision_stale") for e in result.events)
    assert len(pressed) == sum(r.frames for r in result.executions)


def test_a_late_choice_is_stale_after_a_death_or_when_no_longer_legal(config):
    skills = config.skills.for_adapter("fixture")
    adapter = FixtureAdapter(LEVELS)
    try:
        obs = adapter.reset("fixture_l1", 3)
        buttons = adapter.capabilities().buttons
        candidate = generate_candidates(skills, buttons, obs).candidates[0]
        died = [Event(event_type="death", episode_id=obs.episode_id, frame=obs.frame)]
        assert _still_valid(candidate, died, obs, skills, buttons, None) == (None, "event:death")
        gone = candidate.model_copy(update={"skill": "no_such_skill"})
        assert _still_valid(gone, [], obs, skills, buttons, None)[1].startswith("illegal:")
        fresh, stale = _still_valid(candidate, [], obs, skills, buttons, None)
        assert stale is None and fresh.skill == candidate.skill
    finally:
        adapter.close()


def test_live_pause_toggle(config, tmp_path):
    with pytest.raises(ConfigError, match="paced"):
        LiveServer(config, "fixture", tmp_path / "live.sqlite", tick_ms=0).start(
            {"scenario": "fixture_l1", "arm": "B", "pause": False})
    srv = LiveServer(config, "fixture", tmp_path / "live.sqlite", tick_ms=1)
    info = srv.start({"scenario": "fixture_l1", "arm": "B", "pause": False})
    assert info["pause"] is False
    wait_idle(srv)
    assert any(e["type"] == "summary" for e in srv.hub.events_after(0, 0))
    with pytest.raises(InspectError, match="real time"):
        load_recorded(tmp_path / "live.sqlite", info["run_id"])
