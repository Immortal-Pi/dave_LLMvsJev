"""Threat prediction (control/threats.py): plasma and hazard contact along each skill's path,
the candidate screen, the threat_incoming interrupt and the death cause recorded on the graph."""

from dave_agent.control.reach import ReachMap, iter_jumps, trace_skill
from dave_agent.control.skills import _interrupt, generate_candidates
from dave_agent.control.threats import Contact, assess, contact_cause, screen
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
                "jump_left": (-1, None), "jump_right": (1, None)}[skill])
            assert land == landing and path, (start, skill)


def test_plasma_flying_at_a_standing_dave_is_dodged_by_jumping_up(config):
    # Plasma 64 px to Dave's right at his chest height, flying left at 2 px/tick.
    found, _ = contacts(config, scene(entities=[plasma(160, 4 * 16 + 8)]))
    for skill in ("wait_short", "wait_long", "move_right_1", "move_left_1"):
        assert found[skill] is not None and found[skill].kind == "plasma", skill
    assert found["jump_up"] is None  # he is above the plasma's line while it passes under him
    assert found["move_right_1"].tick < found["wait_short"].tick  # walking into it meets it sooner


def test_unknown_plasma_direction_is_assumed_toward_dave(config):
    found, _ = contacts(config, scene(entities=[plasma(160, 4 * 16 + 8, dx=0)]))
    assert found["wait_short"] is not None


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
