"""The planner's explored map (control/level_map.py) and its waypoints (control/goals.py)."""

import json

from dave_agent.control.level_map import render

from .test_goals import o, started


def plan(goal_id, waypoints=()):
    return json.dumps({"goal": goal_id, "rationale": "test", "waypoints": [list(w) for w in waypoints]})


def test_map_shows_what_was_seen_with_dave_and_waypoints(config):
    gm, planner, mem, _ = started(config, [plan("collect:trophy:c6:r1")], start=o(cols=(3, 9)))
    view = planner.requests[0].map
    assert view["origin"] == [3, 0] and view["screen_cols"] == [3, 9]
    assert view["col_ruler"] == ["   0000000", "   3456789"]
    assert view["rows"][1] == "01 ...T..." and view["rows"][4] == "04 #######"
    assert view["rows"][3][3 + 5 - 3] == "@"  # Dave at (5, 3)
    # Columns 0-2 come into view: they are remembered from then on.
    gm.observe(o(frame=5, oid=2))
    gm.update(o(frame=5, oid=2), mem, [])
    cells = gm._cells
    assert render(cells, o(), ((6, 1),))["rows"][1] == "01 ......1..."
    assert render({(0, 0): "empty", (2, 0): "solid"}, o())["rows"][0] == "00 .?#"


def test_valid_waypoints_lead_the_goal_and_pop_in_order(config):
    # Floor row 3 (cols 0-1 and 3-9: fire at 2); ledge row 1 on cols 6-7.
    gm, planner, mem, step = started(config, [plan("collect:trophy:c6:r1", [(4, 3), (7, 1)])])
    assert step.events[-1].payload["waypoints"] == [[4, 3], [7, 1]]
    assert gm.goal.next_waypoint.col == 4 and gm.goal.next_waypoint.row == 3
    at_first = o(player=(4, 3), frame=10, oid=2)
    gm.observe(at_first)
    gm.update(at_first, mem, [])
    assert (gm.goal.next_waypoint.col, gm.goal.next_waypoint.row) == (7, 1)
    assert mem.goal.next_waypoint == gm.goal.next_waypoint


def test_invalid_waypoints_are_sent_back_then_dropped(config):
    bad = [(4, 2), (6, 1)]  # (4, 2) is in mid-air: nothing to stand on
    gm, planner, mem, step = started(config, [plan("collect:trophy:c6:r1", bad), plan("collect:trophy:c6:r1", bad)])
    assert [c.status for c in step.calls] == ["invalid_output", "invalid_output"]
    assert "[[4, 2]]" in planner.feedback[1] and "above a '#'" in planner.feedback[1]
    # Retries spent: the goal stands with only its valid waypoint.
    assert gm.goal.target_ref == "collect:trophy:c6:r1" and step.record.fallback is False
    assert step.events[-1].payload["waypoints"] == [[6, 1]]


def test_unseen_cells_are_not_waypoints(config):
    gm, planner, mem, step = started(config, [plan("collect:trophy:c6:r1", [(1, 3)])], start=o(cols=(3, 9)))
    assert step.calls[0].status == "invalid_output" and gm.goal.target_ref == "collect:trophy:c6:r1"


def test_waypoints_still_ahead_are_in_the_next_request(config):
    gm, planner, mem, _ = started(config, [plan("collect:trophy:c6:r1", [(8, 3), (7, 1)])])
    gm._pending.add("inventory_changed")  # any soft trigger: the next request shows the queue
    gm.update(o(frame=100, oid=2), mem, [])
    assert planner.requests[-1].waypoints == ((8, 3), (7, 1))
    rows = planner.requests[-1].map["rows"]
    assert rows[3][3 + 8] == "1" and rows[1][3 + 7] == "2"  # "RR " prefix, then the cells from col 0
