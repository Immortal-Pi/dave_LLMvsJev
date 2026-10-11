"""Moves that go nowhere are masked (docs/skills.md "Moves that go nowhere"): a walk into a wall,
a flight into a ceiling, and a skill seen not to move Dave from the same pixel before."""

from dave_agent.control.skills import ExecutionResult
from dave_agent.schemas import Decision, PixelPos, StepResult

from .test_goals import memory
from .test_graph import obs
from .test_planner_escalation import WALLED, started_walled, walled
from .test_platforms import offered


def annotated(gm, o, keep=None):
    candidates, catalog = offered(o)
    if keep is not None:
        candidates = [c for c in candidates if c.skill in keep]
    kept, masked = gm.annotate(o, candidates, catalog)
    return {c.skill for c in kept}, {next(c.skill for c in candidates if c.candidate_id == cid): why
                                     for cid, why in masked.items()}


def test_a_walk_into_a_wall_is_masked_and_a_jump_under_open_sky_is_kept(config):
    first = walled()  # Dave at (1,4), the level's wall at col 0 on his left
    gm, planner, step = started_walled(config, first)
    kept, masked = annotated(gm, first)
    assert masked["move_left_1"] == "blocked: no movement (wall or ceiling)"
    assert {"move_right_1", "jump_up", "wait_short"} <= kept


def test_a_flight_into_the_ceiling_is_masked(config):
    flying = obs(grid=WALLED, cols=(0, 6), player=(1, 1), grounded=False, state="jetpacking",
                 inventory={"trophy": 0, "jetpack_fuel": 500})
    gm, planner, step = started_walled(config, walled(fuel=500))
    kept, masked = annotated(gm, flying)
    assert masked["fly_up_1"] == "blocked: no movement (wall or ceiling)"
    assert masked["fly_left_1"] == "blocked: no movement (wall or ceiling)"
    assert {"fly_right_1", "fly_down_1"} <= kept


def test_nothing_is_masked_when_every_option_goes_nowhere(config):
    first = walled()
    gm, planner, step = started_walled(config, first)
    kept, masked = annotated(gm, first, keep={"move_left_1", "move_left_3"})
    assert kept == {"move_left_1", "move_left_3"} and masked == {}


def test_a_skill_seen_not_to_move_dave_is_masked_from_that_pixel_only(config):
    first = walled()
    gm, planner, step = started_walled(config, first)
    mem = memory()
    mem.reset(first)
    candidates, catalog = offered(first)
    jump = next(c for c in candidates if c.skill == "jump_right")
    # The real game: the jump bumped a ceiling the map does not show and Dave moved 3 px.
    moved = [first.model_copy(update={"frame": first.frame + i, "observation_id": 10 + i,
                                      "player_position": PixelPos(x=16 + min(i, 3), y=64)}) for i in range(1, 6)]
    run = ExecutionResult(candidate_id=jump.candidate_id, skill="jump_right", outcome="completed", reason=None,
                          frames=5, input_ticks=5, observation=moved[-1],
                          steps=[StepResult(observation=m, frames_advanced=1, applied_buttons=frozenset({"jump"}))
                                 for m in moved])
    mem.record(Decision(candidate_id=jump.candidate_id, observation_id=first.observation_id), run)
    gm.update(moved[-1], mem, [], run)
    kept, masked = annotated(gm, first)
    assert masked["jump_right"] == "blocked: moved 4 px or less from here before"
    elsewhere = first.model_copy(update={"player_position": PixelPos(x=40, y=64)})
    kept, masked = annotated(gm, elsewhere)
    assert "jump_right" in kept
