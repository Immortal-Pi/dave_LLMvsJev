"""Platforms (control/platforms.py), physics-checked planner waypoints and the attempt log
(control/attempts.py, control/goals.py) on the real level 2 start screen."""

import json
from pathlib import Path

from dave_agent.config import load_config
from dave_agent.control.attempts import AttemptLog
from dave_agent.control.goals import GoalManager
from dave_agent.control.platforms import Platforms
from dave_agent.control.reach import ReachMap
from dave_agent.models.planner import ScriptedPlanner

from .test_goals import memory
from .test_graph import obs

DAVE = load_config(Path(__file__).resolve().parents[2] / "configs" / "experiments.yaml").skills.reach["dave"]

# Dave level 2, first screen, as the bridge reports it (F fire, T trophy, * gems).
LEVEL2 = (
    "....................",
    "####################",
    "#*.....*............",
    "#...................",
    "##..#........#......",
    "#.......###...#.....",
    "#.##.....#...T#.####",
    "#........#.#..#.....",
    "#...###.*#....#.****",
    "#........#*..##.....",
    "###FFFFFF#FFFF#FFFFF",
    "....................",
)
TROPHY = "collect:trophy:c13:r6"
# A pit three rows deep right of the trophy's floor: Dave can fall in but not jump out (a jump
# rises two rows).
TRAP = (
    "..........",
    "##########",
    "#........#",
    "#........#",
    "#........#",
    "#.T......#",
    "#####...##",
    "#####...##",
    "#####...##",
    "##########",
)
TRAP_TROPHY = "collect:trophy:c2:r5"


def level2(**kw):
    kw.setdefault("player", (1, 9))
    return obs(grid=LEVEL2, cols=(0, 19), level="level2", **kw)


def reach(failed=None, grid=LEVEL2):
    o = obs(grid=grid, cols=(0, len(grid[0]) - 1))
    cells = {(c, r): "empty" for c in range(len(grid[0])) for r in range(len(grid))}
    cells.update({(t.pos.col, t.pos.row): t.kind for t in o.tiles})
    return ReachMap(cells, DAVE, failed)


def plan(goal, waypoints=()):
    return json.dumps({"goal": goal, "rationale": "test", "waypoints": list(waypoints)})


def started(outputs, first=None):
    planner = ScriptedPlanner(list(outputs))
    gm = GoalManager(planner, load_config(Path(__file__).resolve().parents[2] / "configs" / "experiments.yaml")
                     .planning, max_retries=1, reach=DAVE)
    mem = memory()
    first = first or level2()
    mem.reset(first)
    return gm, planner, mem, gm.reset(first, mem)


# -- reach: jumps from a platform's end --------------------------------------------------
def test_jump_from_the_ledge_end_reaches_the_top_platform():
    """Real game (scripts/try_skills.py, level 2): Dave walks to x 72 on the one-tile ledge at
    (4,3), overhanging it, and jump_right lands at (8,4). From mid-cell it falls short."""
    m = reach()
    assert m.edge_x((4, 3), 1) == 75 and m.edge_x((4, 3), -1) == 56
    assert m.fly((4, 3), 1, None) != (8, 4)
    assert (8, 4) in {cell for cell, kind, _ in m.moves((4, 3)) if kind == "jump"}


def test_failed_moves_cost_more_but_stay_usable():
    plain = reach().path((4, 7), {(3, 5)})
    assert plain == [((3, 5), "jump")]
    # The direct jump failed twice: the route goes round by c2r5 when it can...
    m = reach(failed={((4, 7), (3, 5)): 2})
    assert ((3, 5), "jump") != m.path((4, 7), {(3, 5)})[0]
    # ...but a move that is the only way up stays usable (a weak executor fails right moves too).
    m = reach(failed={((4, 3), (8, 4)): 5})
    assert m.path((1, 9), {(8, 4)})[-1] == ((8, 4), "jump")


# -- platforms -------------------------------------------------------------------------------
def test_platforms_exits_and_reachability():
    p = Platforms(reach(), (1, 9))
    views = {v["id"]: v for v in p.views()}
    assert views["c1r9"]["hops"] == 0
    assert {"to": "c8r4", "by": "jump right", "from_col": 4} in views["c4r3"]["exits"]
    assert views["c16r5"]["open"] == ["right"] and views["c16r5"]["reachable"]
    assert all(v["row"] >= 1 for v in views.values())  # nothing on top of the level's top wall
    assert p.resolve("c8r4", (13, 6)) == (10, 4) and p.resolve("c9r9") is None


def test_candidate_path_is_the_climb_over_the_left_ledges():
    p = Platforms(reach(), (1, 9))
    assert p.path_note((13, 6)) == ("c1r9 -jump right-> c4r7 -jump left-> c2r5 -jump right-> c4r3 "
                                    "-jump right-> c8r4 -jump right-> c13r8")
    pit = Platforms(reach(grid=TRAP), (6, 8))
    assert pit.path_note((2, 5)) == "no known path over the explored platforms; nearest reachable platform: c5r8"
    assert pit.items["c1r5"].hops is None and pit.items["c5r8"].hops == 0


def test_request_carries_platforms_and_paths():
    gm, planner, mem, step = started([plan(TROPHY)])
    request = planner.requests[0]
    trophy = next(c for c in request.candidates if c.candidate_id == TROPHY)
    assert trophy.path.endswith("-jump right-> c13r8")
    assert any(v["id"] == "c13r8" and v.get("items") == [TROPHY] for v in request.platforms)
    assert step.events[-1].payload["path"][0] == [1, 9, "start"]
    assert step.events[-1].payload["path"][-1] == [13, 8, "jump"]


# -- waypoints checked with the physics ----------------------------------------------------
def test_platform_id_waypoints_resolve_toward_the_goal():
    gm, planner, mem, step = started([plan(TROPHY, ["c4r7", "c2r5", "c4r3", "c8r4"])])
    assert [c.status for c in step.calls] == ["ok"]
    assert step.events[-1].payload["waypoints"] == [[6, 7], [3, 5], [4, 3], [10, 4]]


def test_unreachable_waypoint_is_sent_back_with_the_exits():
    # (2, 0) is on top of the level's top wall: standing ground, but no way up there.
    bad = [[4, 7], [2, 0]]
    gm, planner, mem, step = started([plan(TROPHY, bad), plan(TROPHY, bad)])
    assert [c.status for c in step.calls] == ["invalid_output", "invalid_output"]
    feedback = planner.feedback[1]
    assert "waypoint [2, 0]" in feedback and "not reachable from [4, 7]" in feedback
    assert "from c4r7 Dave can reach: " in feedback and "c2r5 (jump left)" in feedback
    assert step.events[-1].payload["waypoints"] == [[4, 7]]  # the valid prefix stands


def test_goal_must_be_reachable_from_the_last_waypoint():
    start = obs(grid=TRAP, cols=(0, 9), level="trap", player=(4, 5))
    into_the_pit = [[1, 5], [6, 8]]
    gm, planner, mem, step = started([plan(TRAP_TROPHY, into_the_pit)] * 2, first=start)
    feedback = planner.feedback[1]
    assert f"the goal {TRAP_TROPHY} at [2, 5] is not reachable from [6, 8]" in feedback
    assert "the goal is reachable this way: c1r5" in feedback
    assert step.events[-1].payload["waypoints"] == [[1, 5]]  # the last waypoint is dropped


# -- attempts and failed moves -------------------------------------------------------------
def test_attempt_log_records_outcomes_and_failed_moves():
    log = AttemptLog()
    log.start("g1", TROPHY, [(4, 7), (8, 4)], 0, (1, 9))
    log.progress((4, 7), reached=1)
    log.progress((6, 7))
    log.start("g2", TROPHY, [], 100, (6, 7))  # replaces g1
    log.finish("failed", "death", 160)
    assert [(a["goal"], a["outcome"], a.get("waypoints_reached")) for a in log.attempts] == \
        [(TROPHY, "replaced", "1/2"), (TROPHY, "failed", None)]
    assert log.attempts[0]["furthest"] == [6, 7] and log.attempts[1]["frames"] == 60
    log.fail((4, 3), (8, 4), "stuck")
    assert log.failed_links()[0]["avoid"] is False
    log.fail((4, 3), (8, 4), "death: fire")
    assert log.failures() == {((4, 3), (8, 4)): 2}
    assert log.failed_links()[0] == {"from": [4, 3], "to": [8, 4], "times": 2, "how": ["stuck", "death: fire"],
                                     "avoid": True}
    log.reset("level3")
    assert not log.attempts and not log.failures()


def test_stuck_records_the_failed_move_and_the_next_request_shows_it():
    gm, planner, mem, _ = started([plan(TROPHY)])
    at = level2(player=(4, 3), frame=40, oid=2)
    gm.observe(at)
    gm.update(at, mem, [])
    assert gm.goal.next_waypoint.col == 8 and gm.goal.next_waypoint.row == 4  # the jump to the top
    gm._pending.add("goal_failed")  # stands in for the stuck trigger's replan
    gm._record_stuck(at, "stuck")
    gm._record_stuck(at, "stuck")
    assert gm.log.failures() == {((4, 3), (8, 4)): 2}
    gm.update(level2(player=(4, 3), frame=60, oid=3), mem, [])
    request = planner.requests[-1]
    assert request.failed_links[0]["from"] == [4, 3] and request.failed_links[0]["avoid"]
    c4r3 = next(v for v in request.platforms if v["id"] == "c4r3")
    assert {"to": "c8r4", "by": "jump right", "from_col": 4, "note": "failed 2x this level"} in c4r3["exits"]
    assert request.attempts and request.attempts[-1]["goal"] == TROPHY


def test_a_fatal_move_is_recorded_as_a_death_and_a_failed_link():
    from .test_graph import run

    gm, planner, mem, _ = started([plan(TROPHY)])
    gm._stand, gm._heading = (4, 7), (2, 5)
    burned = level2(player=(5, 9), state="burning", frame=80, oid=2)
    events = [e for e in run("jump_left", burned, death=True).events]
    gm.update(burned, mem, events, run=run("jump_left", burned, outcome="death", death=True))
    assert gm.log.deaths == [{"cause": "fire", "tile": [5, 9]}]
    assert gm.log.failed_links()[0]["from"] == [4, 7] and gm.log.failed_links()[0]["how"] == ["death: fire"]
    gm.log.links.clear()
    gm._record_death(level2(player=(5, 3), state="burning"), None, (4, 7), (2, 5))  # nothing touches him here
    assert gm.log.deaths[-1]["cause"] == "unknown" and gm.log.failed_links()[0]["how"] == ["death: unknown"]
