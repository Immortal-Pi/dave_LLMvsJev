"""Planner improvements (docs/planner.md): rule-first planning that escalates to the planner
model, unreachable loot left out, goals that require the jetpack, the Azure planner's map,
waypoint schema and stuck effort, and the jetpack fuel guidance."""

import json

import httpx

from dave_agent.control.goals import FUEL_RESERVE, GoalManager
from dave_agent.models import azure, tactical
from dave_agent.models.azure import AzurePlanner
from dave_agent.models.planner import GoalCandidate, ScriptedPlanner, rule_choice
from dave_agent.schemas import TilePos

from .test_goals import choose, event, memory, o, started
from .test_graph import obs
from .test_planner import client, request
from .test_platforms import DAVE, offered

TROPHY = "collect:trophy:c6:r1"


# -- escalate mode -----------------------------------------------------------------------------
def test_escalate_mode_plans_by_rule_without_a_call(config):
    gm, planner, mem, step = started(config, llm_calls="escalate")
    assert step.calls == [] and planner.requests == [] and gm.calls == 0
    assert gm.goal.target_ref == TROPHY  # the fixed priority
    assert step.record.planner == "rule" and not step.record.fallback and step.record.attempts == 0
    assert step.events[-1].payload["planner"] == "rule"


def test_escalate_mode_calls_the_planner_after_a_death(config):
    gm, planner, mem, _ = started(config, [choose("collect:gem:c4:r3")], llm_calls="escalate")
    gm.update(o(state="burning", grounded=False, frame=10, oid=2), mem, [event("death")])
    step = gm.update(o(player=(1, 3), state="blinking", frame=20, oid=3), mem, [])
    assert len(step.calls) == 1 and step.record.planner == "llm"
    assert gm.goal.target_ref == "collect:gem:c4:r3" and gm.calls == 1


def test_escalate_mode_calls_the_planner_when_the_rule_choice_keeps_failing(config):
    gm, planner, mem, _ = started(config, [choose("explore:right")], llm_calls="escalate", rule_repeat_limit=2)
    gm.log.attempts.append({"goal": TROPHY, "outcome": "expired"})
    gm._pending.add("inventory_changed")
    step = gm.update(o(frame=10, oid=2), mem, [])
    assert step.calls == [] and step.record.planner == "rule"  # failed once: the rule still chooses
    gm.log.attempts.append({"goal": TROPHY, "outcome": "failed"})
    gm._pending.add("inventory_changed")
    step = gm.update(o(frame=20, oid=3), mem, [])
    assert len(step.calls) == 1 and gm.goal.target_ref == "explore:right"


def test_always_mode_is_the_default(config):
    assert config.planning.llm_calls == "always"
    gm, planner, mem, step = started(config)
    assert len(step.calls) == 1 and step.record.planner == "llm"


def test_rule_planned_goals_replay_with_no_planner_output():
    from dave_agent.runner.inspect import planner_outputs

    rule = {"event_type": "goal_set", "payload": {"target_ref": TROPHY, "attempts": 0, "fallback": False,
                                                  "planner": "rule"}}
    llm = {"event_type": "goal_set", "payload": {"target_ref": "explore:right", "attempts": 1, "fallback": False}}
    assert planner_outputs([rule]) == []
    assert [json.loads(t)["goal"] for t in planner_outputs([rule, llm])] == ["explore:right"]


# -- loot and prerequisites (Dave's reach envelope) ---------------------------------------------
# A wall 3 rows high (col 4) that no jump clears: the trophy behind it is reached only by flight.
# The jetpack is on Dave's side.
WALLED = (
    "#######",
    "#.....#",
    "#...#.#",
    "#...#.#",
    "#.J.#T#",
    "#######",
)
# The same, with a gem behind the wall instead of the trophy.
WALLED_GEM = WALLED[:4] + ("#.J.#*#", "#######")


def walled(grid=WALLED, fuel=0):
    return obs(grid=grid, cols=(0, 6), player=(1, 4), inventory={"trophy": 0, "jetpack_fuel": fuel})


def started_walled(config, first, outputs=()):
    planner = ScriptedPlanner(list(outputs))
    gm = GoalManager(planner, config.planning, max_retries=1, reach=DAVE)
    mem = memory()
    mem.reset(first)
    return gm, planner, gm.reset(first, mem)


def test_a_flight_only_goal_requires_the_jetpack(config):
    gm, planner, step = started_walled(config, walled())
    trophy = next(c for c in planner.requests[0].candidates if c.target_name == "trophy")
    assert trophy.requires == ("jetpack",)
    assert trophy.path.endswith("reachable with the jetpack (not held; jetpack at (2,4))")
    jetpack = next(c for c in planner.requests[0].candidates if c.target_name == "jetpack")
    assert jetpack.requires == () and not jetpack.path.startswith("no known path")
    assert gm.goal.target_ref == "collect:jetpack:c2:r4"  # the rule planner takes the jetpack first


def test_with_fuel_nothing_is_required(config):
    gm, planner, step = started_walled(config, walled(fuel=900))
    assert all(c.requires == () for c in planner.requests[0].candidates)


def test_unreachable_loot_is_not_offered(config):
    gm, planner, step = started_walled(config, walled(WALLED_GEM))
    assert not any(c.target_kind == "collectible" for c in planner.requests[0].candidates)
    gm, planner, step = started_walled(config, walled(WALLED_GEM, fuel=900))  # flying gets there
    assert any(c.target_kind == "collectible" for c in planner.requests[0].candidates)


def test_rule_choice_takes_the_required_item_first():
    def cand(cid, kind, name, path, requires=()):
        return GoalCandidate(candidate_id=cid, goal_type="collect", target_kind=kind, target_name=name,
                             target=TilePos(col=1, row=1), description="x", success_predicate="item_collected_at",
                             path=path, requires=requires)

    trophy = cand("collect:trophy:c5:r4", "required_item", "trophy", "no known path", ("jetpack",))
    jetpack = cand("collect:jetpack:c9:r4", "item", "jetpack", "no known path over the explored platforms")
    assert rule_choice((trophy, jetpack)) is jetpack
    assert rule_choice((trophy,)) is trophy  # nothing to take first: unchanged


# -- fuel ----------------------------------------------------------------------------------------
def test_planner_request_carries_the_fuel_and_its_reserve(config):
    gm, planner, step = started_walled(config, walled(fuel=650))
    assert planner.requests[0].player["fuel"] == {"left": 650, "reserve": FUEL_RESERVE}


def test_jetpack_on_off_the_route_says_it_uses_fuel(config):
    first = walled(fuel=900)
    gm, planner, step = started_walled(config, first, [choose("collect:jetpack:c2:r4")])
    candidates, catalog = offered(first)
    notes = {c.skill: c.description for c in gm.annotate(first, candidates, catalog)[0]}
    assert "uses fuel (900 left): not needed for the planned route" in notes["jetpack_on"]
    assert "uses fuel" not in notes["move_right_1"]
    # Toward the trophy behind the wall the route flies: jetpack_on carries the route note instead.
    gm, planner, step = started_walled(config, first, [choose("collect:trophy:c5:r4")])
    notes = {c.skill: c.description for c in gm.annotate(first, candidates, catalog)[0]}
    assert notes["jetpack_on"].startswith("route: turns the jetpack on") and "uses fuel" not in notes["jetpack_on"]


def test_prompts_carry_the_fuel_guidance():
    assert "every tick burns one fuel" in tactical.GAME_RULES
    assert "turn the jetpack on only when a `route:` note says so" in tactical.TACTICAL_TASK
    assert "Jetpack fuel (`player.fuel.left`) is limited" in azure.SYSTEM_PROMPT
    assert "Loot (`collect:loot`) only adds score" in azure.SYSTEM_PROMPT


# -- Azure planner body ----------------------------------------------------------------------------
def body_for(planner_kwargs, req):
    sent = []

    def handler(r):
        sent.append(json.loads(r.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": "{}"}}]})

    AzurePlanner(client(handler), **planner_kwargs).propose(req)
    return sent[0]


def test_map_is_left_out_when_platforms_are_sent(config):
    with_platforms = request(config, map={"rows": ["00 #"]}, platforms=({"id": "c1r3"},))
    body = body_for({"send_map": False}, with_platforms)
    assert "map" not in json.loads(body["messages"][1]["content"])
    assert "`map` is the same level" not in body["messages"][0]["content"]
    body = body_for({"send_map": False}, request(config, map={"rows": ["00 #"]}))  # no platforms: keep it
    assert "map" in json.loads(body["messages"][1]["content"])
    body = body_for({}, with_platforms)
    assert "`map` is the same level" in body["messages"][0]["content"]


def test_stuck_triggers_raise_the_reasoning_effort(config):
    kwargs = {"reasoning_effort": "low", "stuck_effort": "medium"}
    assert body_for(kwargs, request(config))["reasoning_effort"] == "low"
    assert body_for(kwargs, request(config, triggers=("stuck",)))["reasoning_effort"] == "medium"
    assert body_for(kwargs, request(config, triggers=("repeated_failures",)))["reasoning_effort"] == "medium"
    assert body_for({"reasoning_effort": "low"}, request(config, triggers=("stuck",)))["reasoning_effort"] == "low"


def test_waypoints_are_platform_ids_only(config):
    schema = body_for({}, request(config))["response_format"]["json_schema"]["schema"]
    assert schema["properties"]["waypoints"]["items"] == {"type": "string"}
