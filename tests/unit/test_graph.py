"""Phase 5: segmentation, node merging, evidence-based edge updates and no-leak (synthetic observations)."""

import pytest

from dave_agent.control.skills import ExecutionResult
from dave_agent.memory.graph import WorldGraph, edge_key, segments, success_probability
from dave_agent.schemas import Event, Observation, ObservedTile, PixelPos, Region, TilePos

KINDS = {"#": ("solid", "brick"), "F": ("hazard", "fire"), "X": ("exit", "door"), "T": ("required_item", "trophy"),
         "*": ("collectible", "gem"), "J": ("item", "jetpack")}
# Standable: row 3 above the floor (cols 0-9, door at 8) and row 1 above the ledge (cols 6-7).
GRID = (
    "..........",
    "..........",
    "......##..",
    "..F.....X.",
    "##########",
)


def obs(cols=(0, 9), player=(1, 3), grounded=True, state="standing", inventory=None, frame=0, oid=1,
        grid=GRID, level="L1"):
    lo, hi = cols
    tiles = tuple(
        ObservedTile(pos=TilePos(col=c, row=r), kind=KINDS[ch][0], name=KINDS[ch][1])
        for r, line in enumerate(grid) for c, ch in enumerate(line) if ch in KINDS and lo <= c <= hi
    )
    return Observation(
        adapter="fixture", build_id="test", episode_id="e1", level_id=level, frame=frame, observation_id=oid,
        player_position=PixelPos(x=player[0] * 16, y=player[1] * 16), player_velocity=None, grounded=grounded,
        player_state=state, facing="right", lives=3, inventory=inventory or {"trophy": 0}, score=0, tiles=tiles,
        entities=(), region=Region(min=TilePos(col=lo, row=0), max=TilePos(col=hi, row=len(grid) - 1)),
        terminal="running", unavailable_fields=frozenset({"player_velocity"}),
    )


def run(skill, end, outcome="completed", reason=None, frames=40, death=False):
    events = [Event(event_type="death", episode_id="e1", frame=end.frame)] if death else []
    return ExecutionResult(candidate_id=f"c0_{skill}", skill=skill, outcome=outcome, reason=reason, frames=frames,
                           input_ticks=frames, observation=end, events=events)


def graph():
    return WorldGraph("fixture", "test", "local_observed")


def test_segments_split_by_hazards_and_flag_view_edges():
    segs = {(s["row"], s["col_min"], s["col_max"]): (s["open_left"], s["open_right"]) for s in segments(obs())}
    # The fire at (2,3) splits the floor; the ledge row 1 is supported by the bricks at row 2.
    assert segs == {(1, 6, 7): (False, False), (3, 0, 1): (True, False), (3, 3, 9): (False, True)}


def test_segment_merges_as_view_scrolls_and_keeps_oldest_id():
    g = graph()
    g.observe(obs(cols=(3, 6), player=(4, 3)))
    (floor,) = [n for n, d in g.g.nodes(data=True) if d["row"] == 3]
    assert floor == "L1:r3:c3" and g.g.nodes[floor]["open_right"]
    g.observe(obs(cols=(5, 9), player=(7, 3)))
    data = g.g.nodes[floor]
    # Neither view showed what lies left of col 3 (the fire at col 2), so that side stays open.
    assert (data["col_min"], data["col_max"], data["open_left"], data["open_right"]) == (3, 9, True, True)
    g.observe(obs(cols=(0, 9), player=(7, 3)))
    assert not g.g.nodes[floor]["open_left"]
    assert [i["name"] for i in data["items"]] == ["door"]
    assert sorted(n for n, d in g.g.nodes(data=True) if d["row"] == 3) == ["L1:r3:c0", floor]  # fire splits them


def test_absorb_merges_two_partial_nodes_and_aliases():
    g = graph()
    g.observe(obs(cols=(3, 4), player=(3, 3)))
    g.observe(obs(cols=(6, 7), player=(6, 3)))
    assert {"L1:r3:c3", "L1:r3:c6"} <= set(g.g.nodes)
    g.observe(obs(cols=(3, 7), player=(5, 3)))  # col 5 joins them: one platform
    assert "L1:r3:c6" not in g.g and g.resolve("L1:r3:c6") == "L1:r3:c3"
    assert g.g.nodes["L1:r3:c3"]["visited"]


def test_locate_tries_neighbouring_cells_for_straddling_hitbox():
    g = graph()
    g.observe(obs())
    # Centre over the fire cell (2,3), but standing (supported) on the brick beside it.
    straddle = obs(player=(2, 3)).model_copy(update={"player_position": PixelPos(x=2 * 16 - 9, y=48)})
    assert g.locate(straddle) == "L1:r3:c0"
    assert g.locate(obs(player=(4, 2), grounded=False, state="jumping")) is None


def test_success_creates_directed_edge_with_counts_and_prior():
    g = graph()
    start, end = obs(player=(5, 3), oid=1), obs(player=(6, 1), oid=2, frame=40)
    assert g.record_execution(start, run("jump_right", end), "r#1") == "success"
    edge = g.g.edges["L1:r3:c3", "L1:r1:c6", edge_key("jump_right", ())]
    assert (edge["attempts"], edge["successes"], edge["frames_total"], edge["validation"]) == (1, 1, 40, "observed")
    assert success_probability(edge) == pytest.approx(2 / 3)
    assert not g.g.has_edge("L1:r1:c6", "L1:r3:c3")  # direction matters
    g.record_execution(end, run("move_left_1", obs(player=(5, 3), oid=3)), "r#2")
    assert g.g.has_edge("L1:r1:c6", "L1:r3:c3")


def test_parallel_edges_for_different_skills_and_inventory():
    g = graph()
    start, end = obs(player=(5, 3)), obs(player=(6, 1), oid=2)
    g.record_execution(start, run("jump_right", end), "r#1")
    g.record_execution(start, run("jump_up", end), "r#2")
    g.record_execution(obs(player=(5, 3), inventory={"trophy": 1}), run("jump_right", end), "r#3")
    keys = sorted(g.g.get_edge_data("L1:r3:c3", "L1:r1:c6"))
    assert keys == ["jump_right|", "jump_right|trophy", "jump_up|"]


def test_stay_and_inconclusive_create_no_edges():
    g = graph()
    start = obs(player=(4, 3))
    assert g.record_execution(start, run("move_right_1", obs(player=(5, 3), oid=2)), "r#1") == "stay"
    airborne = obs(player=(5, 2), grounded=False, state="freefalling", oid=3)
    assert g.record_execution(start, run("move_right_3", airborne), "r#2") == "inconclusive"
    assert g.g.number_of_edges() == 0
    node = g.g.nodes["L1:r3:c3"]
    assert (node["stays"], node["inconclusive"]) == (1, 1)


def test_failure_attaches_only_when_target_identifiable():
    g = graph()
    start = obs(player=(5, 3))
    burning = obs(player=(3, 3), state="burning", oid=9)
    # No edge yet for this skill from here: kept on the node, no edge invented.
    assert g.record_execution(start, run("jump_left", burning, "interrupted", "hazard_contact"), "r#1") == "failure_node"
    assert g.g.number_of_edges() == 0
    rec = g.g.nodes["L1:r3:c3"]["failed_attempts"]["jump_left|"]
    assert (rec["attempts"], rec["fatal"]) == (1, 1)

    g.record_execution(start, run("jump_right", obs(player=(6, 1), oid=2)), "r#2")
    assert g.record_execution(start, run("jump_right", burning, "interrupted", "new_hazard_nearby:m0"),
                              "r#3") == "failure_edge"
    edge = g.g.edges["L1:r3:c3", "L1:r1:c6", "jump_right|"]
    assert (edge["attempts"], edge["successes"], edge["failures"], edge["fatal"]) == (2, 1, 1, 0)
    assert success_probability(edge) == pytest.approx(0.5)


def test_unanchored_start_is_counted_not_guessed():
    g = graph()
    airborne = obs(player=(4, 1), grounded=False, state="jumping")
    assert g.record_execution(airborne, run("wait_short", obs(player=(4, 3), oid=2)), "r#1") == "unanchored"
    assert g.unanchored == 1 and g.g.number_of_edges() == 0


def test_suggestions_never_become_topology():
    g = graph()
    g.observe(obs())
    nodes, edges = g.g.number_of_nodes(), g.g.number_of_edges()
    g.suggest("L1", 30, 2, source="planner", rationale="unexplored right side")
    assert (g.g.number_of_nodes(), g.g.number_of_edges()) == (nodes, edges) and len(g.suggestions) == 1
    with pytest.raises(ValueError):
        g.suggest("L1", 1, 1, source="observed")


def test_items_refresh_when_collected():
    g = graph()
    with_gem = tuple(line[:4] + ("*" if r == 3 else line[4]) + line[5:] for r, line in enumerate(GRID))
    g.observe(obs(grid=with_gem))
    assert [i["name"] for i in g.g.nodes["L1:r3:c3"]["items"]] == ["gem", "door"]
    g.observe(obs(frame=5))  # gem gone from view
    assert [i["name"] for i in g.g.nodes["L1:r3:c3"]["items"]] == ["door"]


def test_no_unobserved_map_data_in_local_graph(adapter):
    """Built from a local_observed fixture episode, every node lies inside the observed columns."""
    g = graph()
    seen: set[int] = set()
    o = adapter.reset("fixture_l1", 0)  # start (1,4), view half-width 4
    for _ in range(3):
        g.observe(o)
        seen |= set(range(o.region.min.col, o.region.max.col + 1))
        o = adapter.step(frozenset({"right"}), 1).observation
    assert seen == set(range(0, 8))
    for _, d in g.g.nodes(data=True):
        assert set(range(d["col_min"], d["col_max"] + 1)) <= seen
    # The real floor runs to col 10; the graph only knows it continues past col 7.
    floor = g.g.nodes["fixture_l1:r4:c5"]
    assert (floor["col_max"], floor["open_right"]) == (7, True)


def test_skill_evidence_sums_edges_and_node_failures_for_the_held_items():
    g = graph()
    start = obs(player=(5, 3))
    burning = obs(player=(3, 3), state="burning", oid=9)
    for i in range(2):
        g.record_execution(start, run("jump_right", obs(player=(6, 1), oid=2)), f"r#{i}")
    g.record_execution(start, run("jump_right", burning, "interrupted", "hazard_contact", death=True), "r#3")
    g.record_execution(start, run("jump_left", burning, "interrupted", "hazard_contact"), "r#4")
    g.record_execution(obs(player=(5, 3), inventory={"trophy": 1}), run("jump_up", obs(player=(6, 1))), "r#5")
    evidence = g.skill_evidence(obs(player=(4, 3)))  # anywhere on the same segment
    assert evidence == {
        "jump_right": {"attempts": 3, "successes": 2, "fatal": 1, "lands": (1, 6, 7)},
        "jump_left": {"attempts": 1, "successes": 0, "fatal": 1, "lands": None},
    }  # jump_up was recorded with the trophy held, so it does not apply without it
    assert set(g.skill_evidence(obs(player=(4, 3), inventory={"trophy": 1}))) == {"jump_up"}
    assert g.skill_evidence(obs(player=(4, 1), grounded=False, state="jumping")) == {}
