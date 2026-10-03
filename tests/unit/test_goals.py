"""Phase 6: candidate goals, goal lifecycle, shared planning triggers, debounce, cap and fallback."""

import json

from dave_agent.control.goals import GoalManager, TargetMemory, goal_candidates
from dave_agent.memory.working import WorkingMemory
from dave_agent.models.planner import ScriptedPlanner
from dave_agent.schemas import Decision, Event, TilePos

from .test_graph import obs, run

# Trophy on the ledge (6,1); fire (2,3), gem (4,3) and door (8,3) on the floor row.
GRID = (
    "..........",
    "......T...",
    "......##..",
    "..F.*...X.",
    "##########",
)


def o(**kw):
    kw.setdefault("grid", GRID)
    kw.setdefault("player", (5, 3))
    return obs(**kw)


def memory(no_progress=180, failures=3):
    return WorkingMemory(history_frames=120, max_entries=64, context_entries=8, no_progress_frames=no_progress,
                         failure_limit=failures)


def manager(config, outputs=(), **planning):
    planner = ScriptedPlanner(list(outputs))
    cfg = config.planning.model_copy(update={"min_frames_between_calls": 0, **planning})
    return GoalManager(planner, cfg, max_retries=1), planner


def started(config, outputs=(), start=None, mem=None, **planning):
    gm, planner = manager(config, outputs, **planning)
    mem = mem or memory()
    first = start or o()
    mem.reset(first)
    step = gm.reset(first, mem)
    return gm, planner, mem, step


def choose(goal_id):
    return json.dumps({"goal": goal_id, "rationale": "test"})


def event(kind, frame=10, **kw):
    return Event(event_type=kind, episode_id="e1", frame=frame, **kw)


# -- candidates -------------------------------------------------------------------------
def test_candidates_follow_observed_facts_and_rules(config):
    targets = TargetMemory()
    targets.reset(o())
    ids = [c.candidate_id for c in goal_candidates(o(), targets, config.planning)]
    # Door not offered without the trophy (verified rule); col 0 seen, so no explore:left.
    assert ids == ["collect:trophy:c6:r1", "collect:gem:c4:r3", "explore:right"]
    with_trophy = o(inventory={"trophy": 1})
    ids = [c.candidate_id for c in goal_candidates(with_trophy, targets, config.planning)]
    assert ids[0] == "reach:door:c8:r3"


def test_candidates_mark_hazards_and_unseen_left(config):
    view = o(cols=(3, 9))
    targets = TargetMemory()
    targets.reset(view)
    cands = {c.candidate_id: c for c in goal_candidates(view, targets, config.planning)}
    assert "explore:left" in cands and cands["explore:left"].target == TilePos(col=2, row=3)
    targets.observe(o())  # fire at (2,3) now seen, next to the gem's neighbour cell
    gem = next(c for c in goal_candidates(o(), targets, config.planning) if c.target_name == "gem")
    assert "hazard_near_target" not in gem.constraints  # (4,3) is two cells from the fire
    near = o(grid=tuple(r[:3] + "*" + r[4:] if i == 3 else r for i, r in enumerate(GRID)))
    targets.reset(near)
    loot = {c.candidate_id: c for c in goal_candidates(near, targets, config.planning)}
    assert "hazard_near_target" in loot["collect:gem:c3:r3"].constraints  # next to the fire


def test_nearest_collectibles_limit(config):
    many = tuple("*.*.*.*.*." if i == 0 else r for i, r in enumerate(GRID))
    targets = TargetMemory()
    targets.reset(o(grid=many))
    loot = [c for c in goal_candidates(o(grid=many), targets, config.planning) if c.target_kind == "collectible"]
    assert len(loot) == config.planning.nearest_collectibles


# -- lifecycle --------------------------------------------------------------------------
def test_unknown_target_rejected_then_valid_retry(config):
    gm, planner, mem, step = started(config, [choose("collect:key:c1:r1"), choose("collect:gem:c4:r3")])
    assert [c.status for c in step.calls] == ["invalid_output", "ok"]
    assert "not an offered candidate" in planner.feedback[1]
    assert gm.goal.target_ref == "collect:gem:c4:r3" and mem.goal == gm.goal
    assert [e.event_type for e in step.events] == ["model_failure", "goal_set"]


def test_collect_achieved_by_pickup_at_target(config):
    gm, planner, mem, _ = started(config, [choose("collect:gem:c4:r3")])
    after = o(player=(4, 3), frame=10, oid=2,
              grid=tuple(r[:4] + "." + r[5:] if i == 3 else r for i, r in enumerate(GRID)))
    gm.observe(after)
    step = gm.update(after, mem, [event("item_collected", location=TilePos(col=4, row=3))])
    types = [e.event_type for e in step.events]
    assert types[0] == "goal_achieved" and types[-1] == "goal_set"
    assert step.record.triggers == ("goal_achieved",)
    assert step.record.request.previous_goal["status"] == "achieved"


def test_target_gone_from_view_fails_goal(config):
    gm, planner, mem, _ = started(config, [choose("collect:gem:c4:r3")])
    gone = o(frame=10, oid=2, grid=tuple(r[:4] + "." + r[5:] if i == 3 else r for i, r in enumerate(GRID)))
    gm.observe(gone)
    step = gm.update(gone, mem, [])
    assert step.events[0].payload["reason"] == "target_invalid"
    assert step.record.triggers == ("goal_failed",)
    assert step.record.chosen != "collect:gem:c4:r3"  # the gem is no longer a candidate


def test_explore_achieved_on_new_columns_and_goal_expires(config):
    start = o(cols=(0, 5))
    gm, planner, mem, _ = started(config, [choose("explore:right")], start=start, goal_timeout_frames=50)
    assert gm.goal.target_ref == "explore:right"
    step = gm.update(o(cols=(2, 7), frame=10, oid=2), mem,
                     [event("area_discovered", payload={"level_id": "L1", "min_col": 6, "max_col": 7})])
    assert step.events[0].event_type == "goal_achieved"
    later = o(cols=(2, 7), frame=100, oid=3)
    step = gm.update(later, mem, [])
    assert step.events[0].payload["status"] == "expired" and step.record.triggers == ("goal_expired",)


def test_level_change_fails_goal_and_replans(config):
    gm, planner, mem, _ = started(config)
    nxt = o(level="L2", frame=10, oid=2)
    gm.observe(nxt)
    step = gm.update(nxt, mem, [])
    assert step.events[0].payload["reason"] == "level_changed"
    assert step.record.request.level_id == "L2"


# -- triggers ---------------------------------------------------------------------------
def test_no_calls_during_stable_goal_execution(config):
    gm, planner, mem, step = started(config)
    assert len(step.calls) == 1 and step.record.triggers == ("no_goal",)
    for frame, x in ((10, 5), (20, 6), (30, 5), (40, 4)):
        current = o(player=(x, 3), frame=frame, oid=frame)
        gm.observe(current)
        assert gm.update(current, mem, [], run("move_right", current)).calls == []
    assert len(planner.requests) == 1


def test_death_triggers_after_respawn_not_while_burning(config):
    gm, planner, mem, _ = started(config)
    burning = o(state="burning", grounded=False, frame=10, oid=2)
    assert gm.update(burning, mem, [event("death")]).calls == []  # latched: inputs are ignored now
    respawned = o(player=(1, 3), state="blinking", frame=20, oid=3)
    step = gm.update(respawned, mem, [])
    assert step.record.triggers == ("death",) and len(planner.requests) == 2
    assert "recover:safe" not in step.record.request.candidate_ids  # no completed skill yet


def test_soft_triggers_are_debounced_and_latched(config):
    gm, planner, mem, _ = started(config, min_frames_between_calls=60)
    held = o(inventory={"trophy": 1}, frame=10, oid=2)
    assert gm.update(held, mem, []).calls == []  # within 60 frames of the last call
    step = gm.update(o(inventory={"trophy": 1}, frame=70, oid=3), mem, [])
    assert step.record.triggers == ("inventory_changed",)


def test_goal_end_bypasses_debounce(config):
    gm, planner, mem, _ = started(config, [choose("collect:gem:c4:r3")], min_frames_between_calls=1000)
    at = o(player=(4, 3), frame=5, oid=2)
    step = gm.update(at, mem, [event("item_collected", frame=5, location=TilePos(col=4, row=3))])
    assert step.record.triggers == ("goal_achieved",) and len(planner.requests) == 2


def test_repeated_failures_and_stuck_trigger(config):
    gm, planner, mem, _ = started(config)
    for frame in (10, 20):
        assert gm.update(o(frame=frame, oid=frame), mem, [], run("jump_left", o(frame=frame), "interrupted")).calls == []
    step = gm.update(o(frame=30, oid=30), mem, [], run("jump_left", o(frame=30), "interrupted"))
    assert step.record.triggers == ("repeated_failures",)

    gm, planner, mem, _ = started(config, mem=memory(no_progress=100))
    end = o(frame=150, oid=5)
    mem.record(Decision(candidate_id="c0_wait", observation_id=1), run("wait_long", end))
    step = gm.update(end, mem, [], run("wait_long", end))
    assert "stuck" in step.record.triggers
    assert gm.update(o(frame=160, oid=6), mem, []).calls == []  # the new goal restarted the clock


def test_call_cap_then_deterministic_fallback(config):
    gm, planner, mem, _ = started(config, max_calls_per_episode=1)
    at = o(player=(4, 3), frame=5, oid=2)
    step = gm.update(at, mem, [event("item_collected", frame=5, location=TilePos(col=6, row=1))])
    assert step.calls == [] and step.events[0].event_type == "goal_achieved"
    assert step.record.fallback and step.record.fallback_reason == "call_cap"
    assert len(planner.requests) == 1


def test_malformed_output_bounded_then_fallback(config):
    gm, planner, mem, step = started(config, ["not json", '{"goal": "explore:right", "extra": 1}'])
    assert [c.status for c in step.calls] == ["invalid_output", "invalid_output"]  # 1 try + max_retries=1
    assert step.record.fallback and step.record.fallback_reason == "planner_failed"
    assert gm.goal.target_ref == "collect:trophy:c6:r1"  # the fixed priority
    assert sum(e.event_type == "model_failure" for e in step.events) == 2


def test_failed_call_keeps_current_goal_on_soft_trigger(config):
    gm, planner, mem, _ = started(config, [choose("collect:gem:c4:r3"), None, None])
    step = gm.update(o(frame=10, oid=2), mem, [event("death")])
    assert [c.status for c in step.calls] == ["error", "error"] and step.record is None
    assert gm.goal.target_ref == "collect:gem:c4:r3"
    assert gm.update(o(frame=20, oid=3), mem, []).calls == []  # the trigger was consumed
