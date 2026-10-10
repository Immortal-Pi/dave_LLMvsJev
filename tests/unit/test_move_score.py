"""Live move scores (control/move_score.py): the best outcome from each position, from the live graph."""

import math

import pytest

from dave_agent.control.move_score import NOTE_MAX, MoveScore, position_values, score_moves
from dave_agent.memory.graph import shot_killed
from dave_agent.memory.routes import find_route
from dave_agent.schemas import Entity, PixelPos, SkillCandidate

from .test_graph import graph, obs, run
from .test_routes import edge, world


def cand(skill, i=0, frames=40):
    return SkillCandidate(candidate_id=f"c{i}_{skill}", skill=skill, max_frames=frames)


def scores(g, here, cands, cfg, target="G", ends=None, inventory=None):
    values, mode = position_values(g, target, inventory, cfg)
    return score_moves(g, here, values, mode, cands, ends or {}, inventory, cfg), values


def test_option_on_the_cheapest_path_is_best_and_v_matches_find_route(config):
    g = world("A", "B", "C", "G")
    edge(g, "A", "B", skill="jump_right")
    edge(g, "B", "G", skill="jump_right")
    edge(g, "A", "C", skill="jump_left")
    edge(g, "C", "G", skill="jump_left", successes=1, attempts=6)  # an unreliable way on
    cands = [cand("jump_right", 0), cand("jump_left", 1)]
    s, values = scores(g, "A", cands, config.graph)
    right, left = s["c0_jump_right"], s["c1_jump_left"]
    assert right.best and right.regret == 0 and not left.best and left.regret > 0
    assert right.q == pytest.approx(find_route(g, "A", "G", {}, config.graph).cost)
    assert values["A"] == pytest.approx(right.q)  # V(here) is the best option's Q
    assert right.land == "B" and right.mode == "goal"


def test_failures_this_episode_raise_the_move_at_the_next_decision(config):
    g = graph()
    start, ledge = obs(player=(5, 3)), obs(player=(6, 1), oid=2)
    g.record_execution(start, run("jump_right", ledge), "r#1")
    cands = [cand("jump_right")]
    target = g.locate(ledge)
    before = scores(g, g.locate(start), cands, config.graph, target)[0]["c0_jump_right"]
    burning = obs(player=(3, 3), state="burning", oid=9)
    for i in range(3):
        g.record_execution(start, run("jump_right", burning, "interrupted", "hazard_contact", death=True), f"r#{i + 2}")
    after = scores(g, g.locate(start), cands, config.graph, target)[0]["c0_jump_right"]
    assert after.q > before.q and after.fatal == 3 and after.attempts == 4 and after.p_ok < before.p_ok


def test_untried_move_uses_the_reach_estimate_landing(config):
    g = graph()
    g.observe(obs(player=(5, 3)))
    here = g.locate(obs(player=(5, 3)))
    ledge = g.node_at("L1", 1, 6)
    s, _ = scores(g, here, [cand("jump_up")], config.graph, ledge, ends={"c0_jump_up": (6, 1)})
    # No edge to the ledge yet, so V(ledge) = 0 and Q is the untried move's own cost.
    assert s["c0_jump_up"].land == ledge and s["c0_jump_up"].p_ok == 0.5 and s["c0_jump_up"].q > 0
    s, _ = scores(g, here, [cand("jump_up")], config.graph, ledge, ends={"c0_jump_up": None})
    assert s["c0_jump_up"].q is None and "no safe landing" in s["c0_jump_up"].note()


def test_stay_actions_cost_time_plus_v_here_and_kills_lower_a_shot(config):
    g = world("A", "G")
    edge(g, "A", "G", skill="jump_right")
    cands = [cand("shoot", 0, frames=20), cand("wait_short", 1, frames=20)]
    s, values = scores(g, "A", cands, config.graph)
    assert s["c0_shoot"].land == "A" and s["c0_shoot"].q == pytest.approx(s["c1_wait_short"].q)
    cfg = config.graph  # time, plus the risk floor every cost has (p is clipped to p_max)
    assert s["c0_shoot"].q == pytest.approx(values["A"] + cfg.weights.time * 20 / cfg.reference_frames
                                            + cfg.weights.risk * -math.log(cfg.p_max))
    g.g.nodes["A"].update(shots=2, kills=2)
    s, _ = scores(g, "A", cands, config.graph)
    assert s["c0_shoot"].q < s["c1_wait_short"].q and s["c0_shoot"].best


def test_a_shot_predicted_to_hit_is_best_without_past_kills(config):
    g = world("A", "G")
    edge(g, "A", "G", skill="jump_right")
    cands = [cand("shoot_left", 0, frames=20), cand("wait_short", 1, frames=20)]
    values, mode = position_values(g, "G", None, config.graph)
    s = score_moves(g, "A", values, mode, cands, {}, None, config.graph, hits=frozenset({"c0_shoot_left"}))
    assert s["c0_shoot_left"].best and s["c0_shoot_left"].land == "A"
    assert s["c0_shoot_left"].q < s["c1_wait_short"].q


def test_on_the_target_platform_walking_toward_the_goal_tile_is_best(config):
    g = graph()
    start = obs(player=(5, 3))
    g.observe(start)
    here = g.locate(start)  # the floor right of the fire, cols 3-9; the goal tile is at col 8
    cands = [cand("move_left_1", 0, 24), cand("move_right_1", 1, 24), cand("wait_short", 2, 24)]
    ends = {"c0_move_left_1": (4, 3), "c1_move_right_1": (6, 3)}
    held = {"trophy": 1}  # the door is on this platform: a goal only with the trophy
    values, mode = position_values(g, here, held, config.graph)
    s = score_moves(g, here, values, mode, cands, ends, held, config.graph, goal=(here, 8), col=5)
    assert s["c1_move_right_1"].best
    assert s["c0_move_left_1"].q > s["c2_wait_short"].q > s["c1_move_right_1"].q


def test_shot_kill_is_recorded_on_the_start_platform():
    monster = Entity(entity_id="m0", entity_type="spider", position=PixelPos(x=100, y=48), last_observed_frame=0,
                     visible=True, source="adapter")
    start = obs(player=(5, 3)).model_copy(update={"entities": (monster,)})
    end = obs(player=(5, 3), oid=2)
    assert shot_killed(start, end) and not shot_killed(start, start)
    g = graph()
    g.record_execution(start, run("shoot", end), "r#1")
    g.record_execution(start, run("shoot", start), "r#2")
    data = g.g.nodes[g.locate(start)]
    assert (data["shots"], data["kills"]) == (2, 1)


def test_no_target_falls_back_to_the_frontier_then_to_unknown(config):
    g = world("A", "B")
    g.g.nodes["B"]["open_right"] = True
    edge(g, "A", "B", skill="jump_right")
    s, _ = scores(g, "A", [cand("jump_right")], config.graph, target=None)
    assert s["c0_jump_right"].mode == "explore" and s["c0_jump_right"].best
    g.g.nodes["B"]["open_right"] = False
    s, _ = scores(g, "A", [cand("jump_right")], config.graph, target=None)
    assert s["c0_jump_right"].q is None and s["c0_jump_right"].note().startswith("score: ?")


def test_exit_without_the_trophy_is_not_a_goal_value(config):
    g = world("A", "X")
    g.g.nodes["X"]["items"] = [{"kind": "exit", "name": "door", "col": 0, "row": 0}]
    edge(g, "A", "X", skill="jump_right")
    _, mode = position_values(g, "X", {}, config.graph)
    assert mode == "explore"
    _, mode = position_values(g, "X", {"trophy": 1}, config.graph)
    assert mode == "goal"


def test_notes_stay_short():
    long = MoveScore(q=12345.678, regret=999.9, best=False, p_ok=0.1, attempts=1000, successes=1, fatal=999,
                     land="L1:r9:c2", mode="explore", reason=None)
    unknown = MoveScore(q=None, regret=None, best=False, p_ok=0.5, attempts=0, successes=0, fatal=0, land=None,
                        mode="explore", reason="no known way to the explore from there")
    assert len(long.note()) <= NOTE_MAX and len(unknown.note()) <= NOTE_MAX
