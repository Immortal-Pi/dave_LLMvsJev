import pytest

from dave_agent.adapters.base import AdapterError, GameAdapter
from dave_agent.adapters.fixture import FixtureAdapter
from dave_agent.schemas import TilePos

from ..conftest import LEVELS

RIGHT = frozenset({"right"})
JUMP_RIGHT = frozenset({"jump", "right"})


def _trace(actions):
    adapter = FixtureAdapter(LEVELS)
    obs = adapter.reset("fixture_l1", 0)
    trace = [obs.model_dump(exclude={"observation_id"})]
    for buttons, frames in actions:
        trace.append(adapter.step(buttons, frames).model_dump(exclude={"observation": {"observation_id"}}))
    return trace


def test_satisfies_protocol(adapter):
    assert isinstance(adapter, GameAdapter)


def test_deterministic_replay():
    actions = [(RIGHT, 2), (JUMP_RIGHT, 3), (RIGHT, 2), (frozenset(), 4)]
    assert _trace(actions) == _trace(actions)


def test_snapshot_round_trip(adapter):
    adapter.reset("fixture_l1", 0)
    adapter.step(RIGHT, 2)
    snap = adapter.save_snapshot()
    before = adapter.step(JUMP_RIGHT, 3).model_dump(exclude={"observation": {"observation_id"}})
    restored = adapter.load_snapshot(snap)
    assert restored.frame == snap.frame
    after = adapter.step(JUMP_RIGHT, 3).model_dump(exclude={"observation": {"observation_id"}})
    assert before == after


def test_observation_ids_monotonic(adapter):
    ids = [adapter.reset("fixture_l1", 0).observation_id]
    ids += [adapter.step(RIGHT, 1).observation.observation_id for _ in range(3)]
    assert ids == sorted(ids) and len(set(ids)) == len(ids)


def test_walking_into_fire_kills_and_respawns(adapter):
    adapter.reset("fixture_l1", 0)
    result = adapter.step(RIGHT, 10)
    types = [e.event_type for e in result.events]
    assert types == ["death", "respawn"]
    assert result.frames_advanced == 3  # stops at the death frame
    assert result.events[0].payload["cause"] == "hazard_tile:fire"
    assert result.observation.lives == 2
    assert result.observation.player_position.x == 16  # back at start tile col 1


def test_jump_over_fire_collect_trophy_and_exit(adapter):
    adapter.reset("fixture_l1", 0)
    adapter.step(RIGHT, 2)  # col 3
    jumped = adapter.step(JUMP_RIGHT, 3)
    assert jumped.observation.player_position.model_dump() == {"x": 96, "y": 32}
    assert jumped.observation.grounded is True
    got = adapter.step(RIGHT, 1)
    assert [e.payload.get("item") for e in got.events] == ["trophy"]
    assert got.observation.inventory == {"trophy": 1}
    done = adapter.step(RIGHT, 5)
    assert done.observation.terminal == "level_complete"
    with pytest.raises(AdapterError, match="terminal"):
        adapter.step(RIGHT, 1)


def test_game_over_after_last_life(adapter):
    adapter.reset("fixture_l1", 0)
    for _ in range(3):
        result = adapter.step(RIGHT, 10)
    assert result.observation.terminal == "game_over"
    assert result.observation.lives == 0


def test_local_observed_hides_offscreen_tiles(adapter):
    obs = adapter.reset("fixture_l1", 0)
    names = {t.name for t in obs.tiles}
    assert "fire" in names and "gem" in names
    assert "trophy" not in names and "door" not in names  # cols 7 and 9 are outside view
    assert all(obs.region.contains(t.pos) for t in obs.tiles)
    assert obs.region.max == TilePos(col=5, row=5)


def test_collected_items_disappear(adapter):
    adapter.reset("fixture_l1", 0)
    adapter.step(RIGHT, 2)
    adapter.step(JUMP_RIGHT, 3)
    obs = adapter.step(RIGHT, 1).observation
    assert TilePos(col=7, row=2) not in {t.pos for t in obs.tiles}


def test_unsupported_button_rejected(adapter):
    adapter.reset("fixture_l1", 0)
    with pytest.raises(ValueError, match="unsupported buttons \\['duck'\\]"):
        adapter.step(frozenset({"duck"}), 1)


def test_step_before_reset_is_actionable(adapter):
    with pytest.raises(AdapterError, match="call reset"):
        adapter.observe()


def test_unknown_scenario_lists_available(adapter):
    with pytest.raises(AdapterError, match="fixture_l1"):
        adapter.reset("nope", 0)


def test_holding_jump_does_not_rejump(adapter):
    adapter.reset("fixture_l1", 0)
    rows = [adapter.step(frozenset({"jump"}), 1).observation.player_position.y for _ in range(6)]
    assert rows == [48, 32, 48, 64, 64, 64]
