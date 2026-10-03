"""Phase 4: bounded working memory, episode reset, derived motion, progress and stuck detection."""

import pytest

from dave_agent.adapters.fixture import FixtureAdapter
from dave_agent.config import SkillSpec
from dave_agent.control.skills import execute, generate_candidates
from dave_agent.memory.detector import EventDetector
from dave_agent.memory.working import WorkingMemory, player_tile
from dave_agent.schemas import Decision, Goal, PixelPos, TilePos

from ..conftest import LEVELS
from .test_skills import BUTTONS, CFG


def spec(name, buttons, ticks=1, **kw):
    return SkillSpec.model_validate({"name": name, "kind": "single",
                                     "phases": [{"buttons": buttons, "ticks": ticks}], **kw})


WAIT = spec("wait", [])
RIGHT = spec("right", ["right"])
LEFT = spec("left", ["left"])
NEVER = SkillSpec.model_validate({"name": "never", "kind": "single",
                                  "phases": [{"buttons": [], "until": "has_gun", "max_ticks": 2}]})
SKILLS = (WAIT, RIGHT, LEFT, NEVER)


def memory(**kw):
    args = dict(history_frames=120, max_entries=64, context_entries=8, no_progress_frames=5, failure_limit=3)
    args.update(kw)
    return WorkingMemory(**args)


def act(adapter, mem, skill):
    obs = mem.latest
    candidate = next(c for c in generate_candidates(SKILLS, BUTTONS, obs).candidates if c.skill == skill)
    run = execute(adapter, candidate, SKILLS, obs, CFG)
    mem.record(Decision(candidate_id=candidate.candidate_id, observation_id=obs.observation_id), run)
    return run


def test_history_is_bounded_by_entries_and_frames(adapter):
    mem = memory(max_entries=5)
    mem.reset(adapter.reset("fixture_l1", 0))
    for _ in range(40):
        act(adapter, mem, "wait")
    assert len(mem.history) == 5

    mem = memory(history_frames=10)
    mem.reset(adapter.reset("fixture_l1", 0))
    for _ in range(40):
        act(adapter, mem, "wait")
    frame = mem.latest.frame
    assert all(e.end_frame >= frame - 10 for e in mem.history) and len(mem.history) == 11
    assert len(mem.context().recent) == 8  # context_entries


def test_reset_clears_episode_state(adapter):
    mem = memory()
    mem.reset(adapter.reset("fixture_l1", 0))
    mem.set_goal(Goal(goal_id="g1", goal_type="reach", target_ref="door", success_predicate="at_door",
                      source_observation_id=mem.latest.observation_id))
    for skill in ("right", "wait", "never"):
        act(adapter, mem, skill)
    obs = adapter.reset("fixture_l1", 0)
    mem.reset(obs)
    ctx = mem.context()
    assert mem.history == () and mem.goal is None and mem.last_decision is None
    assert ctx.progress.tiles_visited == 1 and ctx.progress.consecutive_failures == 0
    assert ctx.progress.no_progress_frames == 0 and ctx.motion is None and ctx.recent == ()


def test_record_from_another_episode_is_refused(adapter):
    mem = memory()
    mem.reset(adapter.reset("fixture_l1", 0))
    obs = adapter.reset("fixture_l1", 0)  # new episode id, memory not reset
    candidate = generate_candidates(SKILLS, BUTTONS, obs).candidates[0]
    run = execute(adapter, candidate, SKILLS, obs, CFG)
    with pytest.raises(ValueError, match="call reset"):
        mem.record(Decision(candidate_id=candidate.candidate_id, observation_id=obs.observation_id), run)


def test_derived_motion_and_velocity_from_walking(adapter):
    mem = memory()
    mem.reset(adapter.reset("fixture_l1", 0))  # start (1,4)
    act(adapter, mem, "right")
    act(adapter, mem, "right")
    ctx = mem.context()
    assert ctx.velocity.dx == 16
    assert (ctx.motion.dx, ctx.motion.dy, ctx.motion.frames) == (32, 0, 2)
    assert ctx.last.end_tile == TilePos(col=3, row=4) and ctx.progress.no_progress_frames == 0


def test_motion_restarts_after_respawn(adapter):
    mem = memory()
    mem.reset(adapter.reset("fixture_l1", 0))
    for _ in range(3):
        run = act(adapter, mem, "right")  # third step lands in the fire at (4,4)
    assert run.outcome == "interrupted" and run.reason == "death"
    act(adapter, mem, "right")
    motion = mem.motion()
    assert (motion.dx, motion.frames) == (16, 1)  # measured from the respawn point, not the window start


def test_stuck_detection_on_waiting_and_progress_resets_it(adapter):
    mem = memory(no_progress_frames=5)
    mem.reset(adapter.reset("fixture_l1", 0))
    for i in range(1, 6):
        act(adapter, mem, "wait")
        assert mem.progress().no_progress_frames == i
    progress = mem.progress()
    assert progress.stuck and progress.skill_repeats == 5 and progress.tile_revisits == 4
    act(adapter, mem, "right")  # a tile never visited this episode
    assert not mem.progress().stuck and mem.progress().no_progress_frames == 0
    act(adapter, mem, "left")  # back to a visited tile: not progress
    assert mem.progress().no_progress_frames == 1


def test_waypoint_progress_counts_only_approach(adapter):
    mem = memory()
    mem.reset(adapter.reset("fixture_l1", 0))
    act(adapter, mem, "right")  # (2,4)
    mem.set_goal(Goal(goal_id="g", goal_type="reach", target_ref="left wall", next_waypoint=TilePos(col=1, row=4),
                      success_predicate="at", source_observation_id=mem.latest.observation_id))
    act(adapter, mem, "right")  # new tile, but further from the waypoint
    assert mem.progress().no_progress_frames == 1
    act(adapter, mem, "left")
    act(adapter, mem, "left")  # (1,4): closer than ever
    assert mem.progress().no_progress_frames == 0


def test_repeated_failures_counter(adapter):
    mem = memory(failure_limit=3)
    mem.reset(adapter.reset("fixture_l1", 0))
    for _ in range(3):
        assert act(adapter, mem, "never").outcome == "failed"
    assert mem.progress().consecutive_failures == 3 and mem.progress().repeated_failures
    act(adapter, mem, "wait")
    assert mem.progress().consecutive_failures == 0


def test_context_is_deterministic():
    contexts = []
    for _ in range(2):
        adapter = FixtureAdapter(LEVELS)  # fresh adapter: observation ids restart too
        mem = memory()
        mem.reset(adapter.reset("fixture_l1", 0))
        for skill in ("right", "wait", "right", "never", "right"):
            act(adapter, mem, skill)
        contexts.append(mem.context().model_dump_json())
    assert contexts[0] == contexts[1]


def test_player_tile_uses_sprite_centre():
    assert player_tile(PixelPos(x=32, y=144)) == TilePos(col=2, row=9)
    assert player_tile(PixelPos(x=40, y=144)) == TilePos(col=3, row=9)


def test_detector_inventory_and_area_events(adapter):
    detector = EventDetector()
    obs = adapter.reset("fixture_l1", 0)  # view cols 0-5
    first = detector.reset(obs)
    assert [e.event_type for e in first] == ["area_discovered"]
    assert (first[0].payload["min_col"], first[0].payload["max_col"]) == (0, 5)
    assert detector.observe(obs) == []
    moved = adapter.step(frozenset({"right"}), 1).observation
    (area,) = detector.observe(moved)
    assert area.event_type == "area_discovered" and area.payload["min_col"] == area.payload["max_col"] == 6
    got = moved.model_copy(update={"inventory": {"trophy": 1}})
    (inv,) = detector.observe(got)
    assert inv.event_type == "inventory_changed" and inv.payload == {"changes": {"trophy": [0, 1]}}
