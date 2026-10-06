"""Goal credit (control/credit.py): what each skill did toward the goal's target, as notes this
episode (every arm) and, on graph-enabled arms, as learned per-platform credit and route costs."""

import json

from dave_agent.control.credit import GoalCredit, GoalSteps
from dave_agent.memory.graph import GraphStore
from dave_agent.memory.persistence import load_checkpoint, save_checkpoint
from dave_agent.memory.routes import credit_factor, find_route
from dave_agent.schemas import Decision, PixelPos

from .test_graph import obs, run
from .test_platforms import GUN, LEVEL3, offered, plan, started


def test_credit_counts_progress_loops_and_deaths():
    credit = GoalCredit()
    credit.start("collect:gun:c10:r4", "level3", (8, 6))
    credit.record((8, 6), "move_left_1", 4.0, 3.0, (7, 6), died=False)  # closer
    credit.record((7, 6), "move_right_1", 3.0, 4.0, (8, 6), died=False)  # farther, back at (8,6)
    credit.record((7, 6), "move_right_1", 3.0, 4.0, (8, 6), died=False)
    credit.record((8, 6), "jump_right", 4.0, None, None, died=True)
    assert credit.note("collect:gun:c10:r4", (8, 6), "move_left_1") == "for this goal from here: 1x, 1 closer"
    assert credit.note("collect:gun:c10:r4", (7, 6), "move_right_1") == \
        "for this goal from here: 2x, never closer, 2x back where Dave had already been"
    assert credit.note("collect:gun:c10:r4", (8, 6), "jump_right") == "for this goal from here: 1x, 1 died"
    assert credit.note("collect:loot:c1:r4", (8, 6), "move_left_1") is None  # another target
    done = credit.finish(achieved=False)
    assert done is not None and not done.reached and [s[1] for s in done.steps][:2] == ["move_left_1", "move_right_1"]
    assert credit.finish(achieved=True) is None  # nothing open


def level3_at(x, frame=0, oid=1):
    first = obs(grid=LEVEL3, cols=(0, 19), level="level3", player=((x + 8) // 16, 6), frame=frame, oid=oid)
    return first.model_copy(update={"player_position": PixelPos(x=x, y=96)})


def test_the_level3_shuffle_is_called_out_on_the_options():
    """The live run's loop: left to (7,6), right back to (8,6), again and again."""
    first = level3_at(126)
    gm, planner, mem, _ = started([plan(GUN)], first=first)
    latest, frame = first, 0
    for i in range(3):
        for skill, x in (("move_left_1", 110), ("move_right_1", 126)):
            frame += 24
            end = level3_at(x, frame=frame, oid=latest.observation_id + 1)
            mem.record(Decision(candidate_id=f"c0_{skill}", observation_id=latest.observation_id), run(skill, end))
            gm.update(end, mem, [], run(skill, end))
            latest = end
    at = level3_at(110, frame=frame + 24, oid=latest.observation_id + 1)
    candidates, catalog = offered(at)
    notes = {c.skill: c.description for c in gm.annotate(at, candidates, catalog)[0]}
    assert "for this goal from here: 3x, never closer, 3x back where Dave had already been" in notes["move_right_1"]
    # x 110 is the take-off of the 4-tile jump that takes the gun, with no credit against it.
    assert notes["jump_right_4"].startswith("route: picks up")


def graph_with_two_ways():
    """Two moves from the floor (row 3) to the ledge (row 1): a jump and a walk-then-jump."""
    store = GraphStore("fixture", "test", "local_observed")
    store.record_execution(obs(player=(3, 3)), run("jump_right", obs(player=(6, 1), oid=2)), "r#1")
    store.record_execution(obs(player=(3, 3), oid=3), run("jump_up", obs(player=(6, 1), oid=4)), "r#2")
    return store


def test_learned_credit_is_kept_per_platform_and_survives_a_checkpoint(tmp_path):
    store = graph_with_two_ways()
    goal = GoalSteps("collect:gem:c7:r1", "L1", reached=True,
                     steps=[((3, 3), "jump_up", True), ((3, 3), "jump_up", False), ((99, 9), "jump_up", True)])
    assert store.record_credit(goal) == 2  # the step off any mapped platform is skipped
    store.record_credit(GoalSteps("collect:gem:c7:r1", "L1", reached=False, steps=[((3, 3), "jump_right", False)]))
    evidence = store.credit_evidence(obs(player=(3, 3)), "collect:gem:c7:r1")
    assert evidence == {"jump_up": {"tries": 2, "closer": 1, "goals": 1, "reached": 1},
                        "jump_right": {"tries": 1, "closer": 0, "goals": 1, "reached": 0}}
    assert store.credit_evidence(obs(player=(3, 3)), "explore:right") == {}
    graph = store.levels["L1"]
    view = json.loads(json.dumps(graph.view()))
    assert any(n["credit"] for n in view["nodes"])
    path = save_checkpoint(graph, tmp_path / "g.json")
    assert load_checkpoint(path).credit_evidence(obs(player=(3, 3)), "collect:gem:c7:r1") == evidence


def test_credit_weighs_the_learned_route(config):
    store = graph_with_two_ways()
    graph = store.levels["L1"]
    start, target = graph.node_at("L1", 3, 3), graph.node_at("L1", 1, 6)
    plain = find_route(graph, start, target, {}, config.graph)
    assert plain.steps[0].skill in {"jump_right", "jump_up"}
    other = "jump_up" if plain.steps[0].skill == "jump_right" else "jump_right"
    # Past goals for the gem: the move the plain route uses never got there, the other one did.
    store.record_credit(GoalSteps("collect:gem:c7:r1", "L1", reached=False, steps=[((3, 3), plain.steps[0].skill, False)]))
    store.record_credit(GoalSteps("collect:gem:c7:r1", "L1", reached=True, steps=[((3, 3), other, True)]))
    weighted = find_route(graph, start, target, {}, config.graph, "collect:gem:c7:r1")
    assert weighted.steps[0].skill == other
    assert find_route(graph, start, target, {}, config.graph, "explore:right").steps[0].skill == plain.steps[0].skill
    assert credit_factor(None, config.graph) == 1.0
    assert credit_factor({"goals": 2, "reached": 2}, config.graph) == 1 - config.graph.credit_bonus
    assert credit_factor({"goals": 2, "reached": 0}, config.graph) == 1 + config.graph.credit_penalty
