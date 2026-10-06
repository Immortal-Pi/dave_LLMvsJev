"""Threat prediction (control/threats.py): plasma and hazard contact along each skill's path,
the candidate screen, the threat_incoming interrupt and the death cause recorded on the graph."""

from dave_agent.control.reach import ReachMap, iter_jumps, trace_skill
from dave_agent.control.skills import _interrupt, generate_candidates
from dave_agent.control.threats import Contact, assess, assess_timing, contact_cause, screen
from dave_agent.schemas import Entity, Observation, ObservedTile, PixelPos, PixelVelocity, Region, StepResult, TilePos

from .test_reach import DAVE, reach

KINDS = {"#": ("solid", "brick"), "X": ("hazard", "hazard")}
# Dave stands on row 4 (floor row 5); walls at both ends; fire at (11, 4).
ROOM = (
    "####################",
    "#..................#",
    "#..................#",
    "#..................#",
    "#..........X.......#",
    "####################",
)


def cells(rows=ROOM):
    out = {}
    for r, line in enumerate(rows):
        for c, ch in enumerate(line):
            out[(c, r)] = KINDS[ch][0] if ch in KINDS else "empty"
    return out


def plasma(x, y, dx=-2):
    return Entity(entity_id="plasma3", entity_type="plasma", position=PixelPos(x=x, y=y),
                  velocity=PixelVelocity(dx=dx, dy=0) if dx else None, velocity_source="derived" if dx else None,
                  last_observed_frame=0, visible=True, source="adapter")


def scene(player=(5 * 16, 4 * 16), entities=(), rows=ROOM, state="standing", facing="right"):
    tiles = tuple(ObservedTile(pos=TilePos(col=c, row=r), kind=KINDS[ch][0], name=KINDS[ch][1])
                  for r, line in enumerate(rows) for c, ch in enumerate(line) if ch in KINDS)
    return Observation(
        adapter="dave", build_id="test", episode_id="e1", level_id="level1", frame=0, observation_id=1,
        player_position=PixelPos(x=player[0], y=player[1]), player_velocity=None, grounded=True, player_state=state,
        facing=facing, lives=3, inventory={"trophy": 0, "gun": 0, "jetpack_fuel": 0}, score=0, tiles=tiles,
        entities=tuple(entities), region=Region(min=TilePos(col=0, row=0), max=TilePos(col=19, row=len(rows) - 1)),
        terminal="running", unavailable_fields=frozenset({"player_velocity"}),
    )


def offered(config, obs):
    skills = config.skills.for_adapter("dave")
    found = generate_candidates(skills, frozenset({"left", "right", "jump", "fire"}), obs)
    return list(found.candidates), {s.name: s for s in skills}


def contacts(config, obs):
    candidates, specs = offered(config, obs)
    found = assess(obs, ReachMap(cells(), DAVE), candidates, specs, config.skills.executor.threats)
    return {c.skill: found[c.candidate_id] for c in candidates}, candidates


def test_trace_lands_where_fly_does():
    m = reach()
    for start in [(2, 9), (11, 7), (13, 5)]:
        for skill, landing in iter_jumps(m, start):
            path, land = m.trace(start[0] * 16, start[1] * 16, *{
                "jump_up": (0, 0), "jump_left_short": (-1, 32), "jump_right_short": (1, 32),
                "jump_left_4": (-1, 64), "jump_right_4": (1, 64), "jump_left_5": (-1, 80), "jump_right_5": (1, 80),
                "jump_left": (-1, None), "jump_right": (1, None)}[skill])
            assert land == landing and path, (start, skill)


def test_plasma_flying_at_a_standing_dave_is_dodged_by_jumping_up(config):
    # Plasma 64 px to Dave's right at his chest height, flying left at 2 px/tick.
    found, _ = contacts(config, scene(entities=[plasma(160, 4 * 16 + 8)]))
    for skill in ("move_right_1", "move_left_1"):
        assert found[skill] is not None and found[skill].kind == "plasma", skill
    assert found["jump_up"] is None  # he is above the plasma's line while it passes under him
    assert found["move_right_1"].tick < found["move_left_1"].tick  # walking into it meets it sooner
    # Waits are safe: the plasma arrives at tick 32 and a jump after either wait still dodges it.
    assert found["wait_short"] is None and found["wait_long"] is None


def test_a_wait_that_ends_too_late_to_dodge_is_screened(config):
    # 24 px away, hitting a standing Dave at tick 12: wait_long is hit during the wait, and after
    # wait_short no jump gets clear in time, so it keeps the standing contact.
    found, _ = contacts(config, scene(entities=[plasma(120, 4 * 16 + 8)]))
    assert found["wait_long"] is not None and found["wait_long"].tick == 12
    assert found["wait_short"] is not None and found["wait_short"].tick == 12
    assert found["jump_up"] is None  # going now still works


def test_timing_notes_say_when_to_go(config):
    obs = scene(entities=[plasma(160, 4 * 16 + 8)])
    candidates, specs = offered(config, obs)
    found, notes = assess_timing(obs, ReachMap(cells(), DAVE), candidates, specs, config.skills.executor.threats)
    by_skill = {c.skill: notes.get(c.candidate_id) for c in candidates}
    assert by_skill["wait_short"].startswith("timing: standing is safe for 31 ticks")
    assert "then safe: " in by_skill["wait_short"] and "jump_up" in by_skill["wait_short"]
    # jump_up works now and after a short wait, not once the plasma is under him.
    assert by_skill["jump_up"].startswith("timing: go now, unsafe if started ")


def test_unknown_plasma_direction_is_assumed_toward_dave(config):
    found, _ = contacts(config, scene(entities=[plasma(120, 4 * 16 + 8, dx=0)]))
    assert found["wait_long"] is not None


def test_plasma_stops_at_a_brick(config):
    rows = list(ROOM)
    rows[4] = "#.......#..........#"  # a brick between Dave (col 5) and the plasma
    obs = scene(entities=[plasma(160, 4 * 16 + 8)], rows=tuple(rows))
    candidates, specs = offered(config, obs)
    found = assess(obs, ReachMap(cells(tuple(rows)), DAVE), candidates, specs, config.skills.executor.threats)
    wait = next(c for c in candidates if c.skill == "wait_short")
    assert found[wait.candidate_id] is None


def test_walking_into_fire_is_flagged_and_jumping_over_it_is_not(config):
    found, _ = contacts(config, scene(player=(8 * 16, 4 * 16)))
    assert found["move_right_3"] == Contact("hazard", "hazard", found["move_right_3"].tick)
    assert found["move_right_1"] is None and found["jump_right"] is None and found["wait_short"] is None


def test_a_falling_dave_steers_away_from_fire_below(config):
    # Free-falling straight above the fire at (11, 4): waiting drops him in, walking left steers clear.
    obs = scene(player=(11 * 16, 2 * 16), state="freefalling", facing="front")
    candidates = [c for c in offered(config, scene())[0] if c.skill in ("wait_long", "move_left_3", "move_right_1")]
    specs = {s.name: s for s in config.skills.for_adapter("dave")}
    found = assess(obs, ReachMap(cells(), DAVE), candidates, specs, config.skills.executor.threats)
    by_skill = {c.skill: found[c.candidate_id] for c in candidates}
    assert by_skill["wait_long"] is not None and by_skill["wait_long"].kind == "hazard"
    assert by_skill["move_left_3"] is None
    assert assess(scene(state="jumping"), ReachMap(cells(), DAVE), candidates, specs,
                  config.skills.executor.threats) == {}  # mid-jump: the remaining arc is unknown
    # Falling Dave drifts the way he faces with no key held: facing left, waiting carries him into
    # the fire from 2 columns right of it.
    drifting = scene(player=(13 * 16, 2 * 16), state="freefalling", facing="left")
    found = assess(drifting, ReachMap(cells(), DAVE), candidates, specs, config.skills.executor.threats)
    assert found[next(c.candidate_id for c in candidates if c.skill == "wait_long")] is not None


def test_screen_drops_contacts_and_keeps_the_latest_when_all_touch(config):
    found, candidates = contacts(config, scene(entities=[plasma(160, 4 * 16 + 8)]))
    by_id = {c.candidate_id: found[c.skill] for c in candidates}
    kept, masked = screen(candidates, by_id)
    assert {c.skill for c in kept} == {s for s, v in found.items() if v is None}
    assert set(masked) == {c.candidate_id for c in candidates if found[c.skill] is not None}
    assert all(r.startswith("threat:") for r in masked.values())
    everyone = {c.candidate_id: Contact("plasma3", "plasma", 10 + i % 3) for i, c in enumerate(candidates)}
    kept, masked = screen(candidates, everyone)
    assert kept and all(everyone[c.candidate_id].tick == 12 for c in kept) and len(kept) + len(masked) == len(
        candidates)


def test_threat_incoming_interrupts_a_wait_only_for_a_new_threat(config):
    spec = next(s for s in config.skills.for_adapter("dave") if s.name == "wait_long")
    cfg = config.skills.executor
    close = scene(entities=[plasma(5 * 16 + 30, 4 * 16 + 8)])  # about 6 ticks away
    step = StepResult(observation=close, frames_advanced=1, applied_buttons=frozenset())
    assert _interrupt(spec, step, frozenset({"plasma3"}), cfg).startswith("threat_incoming:plasma3@")
    assert _interrupt(spec, step, frozenset({"plasma3"}), cfg, expected=frozenset({"plasma3"})) is None
    far = scene(entities=[plasma(5 * 16 + 120, 4 * 16 + 8)])
    assert _interrupt(spec, StepResult(observation=far, frames_advanced=1, applied_buttons=frozenset()),
                      frozenset({"plasma3"}), cfg) is None


def test_trace_skill_waits_in_place(config):
    spec = next(s for s in config.skills.for_adapter("dave") if s.name == "wait_short")
    assert trace_skill(ReachMap(cells(), DAVE), 80, 64, spec) == [(80, 64)] * spec.max_frames


def test_contact_cause_names_what_touched_dave():
    assert contact_cause(scene(entities=[plasma(5 * 16 + 4, 4 * 16 + 8)]))[0] == "plasma"
    assert contact_cause(scene(player=(11 * 16, 4 * 16))) == ("hazard", [11, 4])
    assert contact_cause(scene())[0] == "unknown"


# -- scripted monsters (monster.c): route steps and shots not fired yet ----------------------
def monster(x, y, steps=((-2, 0),), cooldown=0, shoot_in=3, fire_rate=5, plasma_state=None):
    return Entity(entity_id="monster0", entity_type="swirl", position=PixelPos(x=x, y=y), last_observed_frame=0,
                  visible=True, source="adapter",
                  motion={"steps": [list(s) for s in steps], "cooldown": cooldown, "shoot_in": shoot_in,
                          "fire_rate": fire_rate, "plasma": plasma_state})


def test_monster_follows_its_route_one_step_every_five_ticks():
    from dave_agent.control.threats import positions, threats

    o = scene(entities=[monster(200, 20, steps=((4, 1), (-3, 2)), cooldown=3, shoot_in=99)])
    (t,) = threats(o)
    path = positions(t, 12, cells())
    # cooldown 3 -> 4 -> 5, which resets to 0 and steps: steps on ticks 3, 8, 13 (monster.c).
    assert path[1] == (200, 20) and path[2] == (204, 21) and path[6] == (204, 21)
    assert path[7] == (201, 23) and path[11] == (201, 23)


def test_the_next_shot_is_predicted_toward_dave_and_stops_at_a_wall():
    from dave_agent.control.threats import forecast

    # Dave at x 80 left of the monster: the shot spawns at x - 21, y + 8 and flies left.
    o = scene(entities=[monster(200, 56, steps=((0, 0),), shoot_in=2)])
    shots = [f for f in forecast(o, 120) if f["kind"] == "plasma"]
    first = shots[0]
    assert first["id"] == "shot0.1" and first["spawn"] == 3
    assert first["path"][0] == (200 - 21 - 2, 64)  # spawned, then moved 2 px in the same tick
    assert first["path"][-1][0] <= 2 * 16 + 2  # dies at the left wall (col 0)
    assert len(shots) >= 2  # fire_rate 5: the next one follows once the first is gone


def test_a_skill_walking_into_the_line_of_fire_is_screened(config):
    o = scene(entities=[monster(140, 56, steps=((0, 0),), shoot_in=10)])
    found, _ = contacts(config, o)
    assert found["wait_long"] is not None and found["wait_long"].what == "shot0"
    assert found["wait_long"].note().startswith("danger: the next shot hits in")
    assert found["jump_up"] is None or found["jump_up"].tick > found["wait_long"].tick


def test_a_jump_under_way_is_assessed_to_the_end_and_the_walk_after_it(config):
    from dave_agent.control.reach import trace_in_jump

    rows = list(ROOM)
    rows[4] = "#...X..............#"
    m = ReachMap(cells(rows), DAVE)
    specs = {s.name: s for s in config.skills.for_adapter("dave")}
    # 87 ticks into a jump, 7 px above the floor: move_left_3 lands, then walks on into the fire.
    path = trace_in_jump(m, 7 * 16, 4 * 16 - 7, 87, specs["move_left_3"])
    assert path[7][1] == 4 * 16 and len(path) == specs["move_left_3"].max_frames
    assert path[-1][0] < 5 * 16
    o = scene(player=(7 * 16, 4 * 16 - 7), rows=rows, state="jumping").model_copy(update={"jump_tick": 87})
    candidates, sp = offered(config, o)
    found = assess(o, m, candidates, sp, config.skills.executor.threats)
    by_skill = {c.skill: found.get(c.candidate_id) for c in candidates}
    assert by_skill["move_left_3"] is not None and by_skill["move_left_3"].what == "hazard"
    assert by_skill["wait_short"] is None


def test_a_shot_flying_past_the_screen_edge_may_die_on_an_unseen_wall(config):
    """Level 4: the swirl's shot flew right off the screen into a wall Dave had not seen; it died
    there and the next one, fired at Dave much sooner, hit him. Both outcomes are predicted."""
    from dave_agent.control.threats import first_contact, threats

    seen = {cell: kind for cell, kind in cells().items() if cell[0] < 17}  # columns 17+ unseen
    flying = {"x": 230, "y": 64, "dx": 2, "dead": False}
    o = scene(entities=[monster(200, 56, steps=((0, 0),), shoot_in=5, plasma_state=flying)])
    contact = first_contact([(80, 64)] * 60, threats(o), seen, config.skills.executor.threats)
    assert contact is not None and contact.what == "shot0"


def test_safe_window_finds_when_a_blocked_move_becomes_safe(config):
    """A shot flying left over Dave's head: jumping up now meets it, and is safe once it has
    passed; the long jump right lands in the fire whenever it starts."""
    from dave_agent.control.threats import safe_window

    obs = scene(entities=[plasma(130, 3 * 16, -2)])
    candidates, specs = offered(config, obs)
    m = ReachMap(cells(), DAVE)
    cfg = config.skills.executor.threats
    found = assess(obs, m, candidates, specs, cfg)
    assert found[next(c.candidate_id for c in candidates if c.skill == "jump_up")].kind == "plasma"
    delay, length = safe_window(obs, m, specs["jump_up"], cfg)
    assert 10 <= delay <= 40 and length > 100
    assert safe_window(obs, m, specs["move_right_1"], cfg)[0] == 0
    assert safe_window(obs, m, specs["jump_right"], cfg) is None


def test_self_sacrifice_is_offered_only_when_stuck_long_with_lives_to_spare(config):
    """A swirl that never fires closes in: after SACRIFICE_FRAMES with no way on, the moves into
    its body (it dies for good, Dave loses a life) are offered as the last resort; never with 1 life."""
    from dave_agent.control.goals import SACRIFICE_FRAMES, GoalManager
    from dave_agent.models.planner import RuleMockPlanner

    def gm_at(frame, lives):
        obs = scene(entities=[monster(9 * 16, 4 * 16, steps=((-1, 0),), shoot_in=10**6)])
        return obs.model_copy(update={"frame": frame, "lives": lives})

    for lives, frames, offered_ in ((3, SACRIFICE_FRAMES, True), (1, SACRIFICE_FRAMES, False), (3, 100, False)):
        gm = GoalManager(RuleMockPlanner(), config.planning, 1, reach=DAVE, threats=config.skills.executor.threats)
        first = gm_at(0, lives)
        gm._learn(first)
        candidates, specs = offered(config, first)
        gm.annotate(first, candidates, tuple(specs.values()))
        later = gm_at(frames, lives)
        kept, masked = gm.annotate(later, candidates, tuple(specs.values()))
        last = [c for c in kept if "last resort" in c.description]
        assert bool(last) == offered_, (lives, frames)
        if offered_:
            assert all(c.description.startswith("route: last resort") and "swirl" in c.description for c in last)


def test_a_death_after_a_move_predicted_safe_is_a_forecast_miss(config):
    """The chosen move was predicted safe and a monster's shot killed Dave: the option from that
    tile is noted from then on, and the miss is an event (never a mask)."""
    from dave_agent.control.goals import GoalManager
    from dave_agent.control.skills import ExecutionResult
    from dave_agent.models.planner import RuleMockPlanner
    from dave_agent.schemas import Event

    gm = GoalManager(RuleMockPlanner(), config.planning, 1, reach=DAVE, threats=config.skills.executor.threats)
    start = scene()
    gm._learn(start)
    candidates, specs = offered(config, start)
    gm.annotate(start, candidates, tuple(specs.values()))
    chosen = next(c for c in candidates if c.skill == "move_right_1")
    assert gm.predicted_safe(chosen.candidate_id) is True
    burning = scene(player=(6 * 16, 4 * 16), state="burning", entities=[plasma(6 * 16 + 4, 4 * 16 + 4)])
    run = ExecutionResult(candidate_id=chosen.candidate_id, skill="move_right_1", outcome="interrupted",
                          reason="hazard_contact", frames=10, input_ticks=10, observation=burning)
    miss = gm._forecast_miss(burning, run)
    assert miss is not None and miss.payload["cause"] == "plasma" and isinstance(miss, Event)
    notes = {c.skill: c.description for c in gm.annotate(start, candidates, tuple(specs.values()))[0]}
    assert "forecast missed here before: hit 1x (plasma) though predicted safe" in notes["move_right_1"]
