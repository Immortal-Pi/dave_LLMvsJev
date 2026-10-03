"""Phase 5: deterministic route search, inventory filtering, unknown routes, replan triggers."""

import pytest

from dave_agent.memory.graph import WorldGraph, edge_key
from dave_agent.memory.persistence import from_checkpoint, to_checkpoint
from dave_agent.memory.routes import RouteTracker, edge_cost, find_route


def node(g, nid, visited=True, open_right=False, items=()):
    g.g.add_node(nid, level_id="L", row=0, col_min=0, col_max=0, open_left=False, open_right=open_right,
                 surface="solid", visited=visited, items=list(items), stays=0, inconclusive=0, failed_attempts={},
                 evidence=[], evidence_count=0, last_verified_frame=0)


def edge(g, u, v, skill="hop", successes=10, attempts=10, frames=50, ctx=()):
    g.g.add_edge(u, v, key=edge_key(skill, ctx), skill=skill, inventory_context=list(ctx), validation="observed",
                 attempts=attempts, successes=successes, failures=attempts - successes, fatal=0,
                 frames_total=frames * successes, evidence=[], evidence_count=attempts, last_outcome="success")
    g.topology_version += 1


def world(*names, **kw):
    g = WorldGraph("fixture", "test", "local_observed")
    for n in names:
        node(g, n, **kw)
    return g


def test_reliable_detour_beats_unreliable_shortcut(config):
    g = world("A", "B", "C")
    edge(g, "A", "B")
    edge(g, "B", "C")
    edge(g, "A", "C", skill="long_jump", successes=1, attempts=5)
    route = find_route(g, "A", "C", {}, config.graph)
    assert route.status == "found" and route.nodes == ("A", "B", "C")
    assert route.cost == pytest.approx(sum(s.cost for s in route.steps))
    edge(g, "A", "C", skill="walk", successes=10, attempts=10)  # a reliable parallel edge
    route = find_route(g, "A", "C", {}, config.graph)
    assert route.nodes == ("A", "C") and route.steps[0].skill == "walk"


def test_edges_needing_unheld_items_are_filtered(config):
    g = world("A", "C")
    edge(g, "A", "C", skill="shoot_through", ctx=("gun",))
    assert find_route(g, "A", "C", {"gun": 0}, config.graph).status == "unreachable"
    assert find_route(g, "A", "C", {"gun": 1}, config.graph).status == "found"


def test_exit_requires_trophy(config):
    g = world("A")
    node(g, "Door", items=[{"kind": "exit", "name": "door", "col": 0, "row": 0}])
    edge(g, "A", "Door")
    route = find_route(g, "A", "Door", {"trophy": 0}, config.graph)
    assert route.status == "unreachable" and route.reason == "requires:trophy"
    assert find_route(g, "A", "Door", {"trophy": 1}, config.graph).status == "found"


def test_unknown_or_unconnected_target_returns_frontier(config):
    g = world("A")
    node(g, "Edge", open_right=True)
    node(g, "Seen", visited=False)
    edge(g, "A", "Edge")
    route = find_route(g, "A", "Seen", {}, config.graph)
    assert (route.status, route.reason) == ("unreachable", "no_verified_route")
    assert route.frontier == ("Edge", "Seen")
    route = find_route(g, "A", "Nowhere", {}, config.graph)
    assert route.reason == "unknown_target" and route.frontier == ("Edge", "Seen")
    assert find_route(g, "A", "A", {}, config.graph).status == "at_target"


def test_untried_reliability_is_uncertain_not_certain(config):
    tried, fresh = {"attempts": 10, "successes": 10, "frames_total": 500}, {"attempts": 1, "successes": 1,
                                                                              "frames_total": 50}
    assert edge_cost(fresh, config.graph) > edge_cost(tried, config.graph)


def test_ties_resolve_deterministically_and_survive_round_trip(config):
    g = world("A", "B", "C", "D")
    for u, v in (("A", "B"), ("A", "C"), ("B", "D"), ("C", "D")):
        edge(g, u, v)
    first = find_route(g, "A", "D", {}, config.graph)
    assert all(find_route(g, "A", "D", {}, config.graph) == first for _ in range(5))
    assert find_route(from_checkpoint(to_checkpoint(g)), "A", "D", {}, config.graph) == first


def test_route_tracker_replan_triggers(config):
    g = world("A", "B", "C", "X")
    edge(g, "A", "B")
    edge(g, "B", "C")
    route = find_route(g, "A", "C", {}, config.graph)
    tracker = RouteTracker(route, g, {})
    assert tracker.next_step.target == "B"
    assert tracker.update(g, "A", {}) is None and tracker.update(g, None, {}) is None
    assert tracker.update(g, "B", {}) is None and tracker.next_step.target == "C"
    assert tracker.update(g, "X", {}) == "off_route"
    assert tracker.update(g, "B", {}, step_failed=True) == "edge_failed"
    assert tracker.update(g, "B", {"gun": 1}) == "inventory_changed"
    assert tracker.update(g, "C", {}) == "target_reached"
    edge(g, "A", "C")
    assert tracker.update(g, "B", {}) == "topology_changed"
    assert RouteTracker(find_route(g, "C", "A", {}, config.graph), g, {}).update(g, "C", {}).startswith("no_route")
