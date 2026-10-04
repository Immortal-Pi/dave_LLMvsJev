"""Phase 6, graph-enabled arms: Python route search turns the goal into the next waypoint.

Learned routes never cost a planner call: rerouting on topology changes or progress is local;
only a route lost after being found raises the soft trigger ``route_invalidated``.
"""

from dave_agent.control.goals import GoalManager
from dave_agent.memory.graph import GraphStore, edge_key
from dave_agent.memory.working import WorkingMemory
from dave_agent.models.planner import ScriptedPlanner
from dave_agent.schemas import TilePos

from .test_graph import obs, run

# Floor row 4; ledge A on row 2 (cols 4-5); ledge B on row 1 (cols 8-9) holding the trophy.
GRID = (
    "..........",
    "........T.",
    "........##",
    "....##....",
    "..........",
    "##########",
)
FLOOR, LEDGE_A, LEDGE_B = "L1:r4:c0", "L1:r2:c4", "L1:r1:c8"


def o(**kw):
    kw.setdefault("grid", GRID)
    kw.setdefault("player", (3, 4))
    return obs(**kw)


def setup(config, graph_enabled=True):
    store = GraphStore("fixture", "test", "local_observed")
    g = store.for_level("L1")
    planner = ScriptedPlanner([])
    cfg = config.planning.model_copy(update={"min_frames_between_calls": 0})
    gm = GoalManager(planner, cfg, 1, store if graph_enabled else None, config.graph if graph_enabled else None)
    mem = WorkingMemory(120, 64, 8, 180, 3)
    start = o()
    g.observe(start)
    mem.reset(start)
    gm.reset(start, mem)
    return g, gm, planner, mem


def learn_ledges(g):
    g.record_execution(o(player=(3, 4)), run("jump_right", o(player=(4, 2), oid=2)), "r#1")
    g.record_execution(o(player=(5, 2)), run("jump_right", o(player=(8, 1), oid=3)), "r#2")


def test_unknown_route_gives_frontier_and_target_waypoint(config):
    g, gm, planner, mem = setup(config)
    assert gm.goal.target_ref == "collect:trophy:c8:r1"
    request = planner.requests[0]
    assert request.graph_routes
    routes = {c.candidate_id: c.route for c in request.candidates}
    assert routes["collect:trophy:c8:r1"]["status"] == "unreachable"
    assert routes["collect:trophy:c8:r1"]["reason"] == "no_verified_route"
    assert routes["collect:trophy:c8:r1"]["frontier"]  # explicit places to explore instead
    assert routes["explore:right"]["status"] in ("found", "at_target", "unreachable")
    assert gm.goal.next_waypoint == TilePos(col=8, row=1)  # no route: head for the target itself


def test_learned_route_sets_next_waypoint_without_planner_calls(config):
    g, gm, planner, mem = setup(config)
    learn_ledges(g)  # topology changed after planning
    gm.update(o(oid=4, frame=10), mem, [])
    assert len(planner.requests) == 1  # rerouting is Python only
    assert gm.goal.next_waypoint == TilePos(col=5, row=2)  # ledge A, the cell nearest the trophy
    assert mem.goal.next_waypoint == TilePos(col=5, row=2)
    gm.update(o(player=(5, 2), oid=5, frame=20), mem, [], run("jump_right", o(player=(5, 2))))
    assert gm.goal.next_waypoint == TilePos(col=8, row=1)  # on ledge A: next is ledge B
    assert len(planner.requests) == 1


def test_route_lost_after_found_asks_planner(config):
    g, gm, planner, mem = setup(config)
    learn_ledges(g)
    gm.update(o(oid=4, frame=10), mem, [])
    g.g.remove_edge(LEDGE_A, LEDGE_B, key=edge_key("jump_right", ()))
    g.topology_version += 1
    step = gm.update(o(player=(5, 2), oid=5, frame=20), mem, [])
    assert step.record is not None and "route_invalidated" in step.record.triggers
    assert len(planner.requests) == 2


def test_graph_disabled_arm_gets_same_goal_without_routes(config):
    _, plain, plain_planner, _ = setup(config, graph_enabled=False)
    _, routed, routed_planner, _ = setup(config)
    a, b = plain_planner.requests[0], routed_planner.requests[0]
    assert not a.graph_routes and all(c.route is None for c in a.candidates)
    strip = {"graph_routes": True, "candidates": {"__all__": {"route"}}}
    assert a.model_dump(exclude=strip) == b.model_dump(exclude=strip)
    assert plain.goal.target_ref == routed.goal.target_ref
    assert plain.goal.next_waypoint == TilePos(col=8, row=1)
