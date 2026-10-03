"""Real-game tests through the deadly-dave bridge. Skipped when the bridge is not built
(run scripts\\setup_dave.bat). No network or credentials needed."""

import pytest

from dave_agent.adapters.base import AdapterError, GameAdapter
from dave_agent.adapters.dave import BRIDGE_EXE, DaveBridgeAdapter, monster_type

from ..conftest import ROOT

DAVE_DIR = ROOT / "external" / "deadly-dave"
pytestmark = [
    pytest.mark.dave,
    pytest.mark.skipif(not (DAVE_DIR / BRIDGE_EXE).exists(), reason="deadly-dave bridge not built"),
]
RIGHT, LEFT = frozenset({"right"}), frozenset({"left"})


@pytest.fixture(scope="module")
def dave():
    adapter = DaveBridgeAdapter(DAVE_DIR)
    yield adapter
    adapter.close()


def _hold(adapter, buttons, ticks):
    events, result = [], None
    for _ in range(ticks):
        result = adapter.step(buttons, 1)
        events += result.events
        if result.observation.terminal != "running" or any(e.event_type == "death" for e in result.events):
            break
    return result, events


def _comparable(obs):
    return obs.model_dump(exclude={"observation_id", "episode_id"})


def test_satisfies_protocol(dave):
    assert isinstance(dave, GameAdapter)
    assert dave.capabilities().adapter == "dave"


def test_level1_start_state(dave):
    obs = dave.reset("level1", 0)
    assert obs.player_position.model_dump() == {"x": 32, "y": 144}
    assert obs.lives == 4 and obs.score == 0 and obs.grounded is True
    assert obs.inventory == {"trophy": 0, "gun": 0, "jetpack_fuel": 0}
    assert "player_velocity" in obs.unavailable_fields


def test_deterministic_replay(dave):
    def trace():
        dave.reset("level1", 0)
        return [_comparable(dave.step(b, n).observation) for b, n in [(RIGHT, 12), (frozenset({"jump"}), 1), (LEFT, 30)]]

    assert trace() == trace()


def test_multi_tick_step_matches_single_ticks(dave):
    dave.reset("level1", 0)
    batched = dave.step(RIGHT, 20).observation
    dave.reset("level1", 0)
    single, _ = _hold(dave, RIGHT, 20)
    assert batched.player_position == single.observation.player_position
    assert batched.frame == single.observation.frame


def test_gem_pickup_event_and_score(dave):
    dave.reset("level1", 0)
    _, first = _hold(dave, frozenset({"jump", "left"}), 1)
    result, rest = _hold(dave, LEFT, 40)
    pickups = [e for e in first + rest if e.event_type == "item_collected"]
    assert [(e.payload["item"], e.location.col, e.location.row) for e in pickups] == [("loot", 1, 7)]
    assert result.observation.score == 100
    assert all(t.pos.col != 1 or t.pos.row != 7 for t in result.observation.tiles)


def test_fire_death_respawns_at_start(dave):
    start = dave.reset("level2", 0)
    result, events = _hold(dave, RIGHT, 400)
    death = [e for e in events if e.event_type == "death"]
    assert len(death) == 1 and death[0].payload["cause"] == "unknown"
    assert events[-1].event_type == "respawn" and events[-1].payload["auto_ticks"] > 0
    assert result.observation.lives == start.lives - 1
    assert result.observation.player_position == start.player_position
    assert result.frames_advanced == 1 + events[-1].payload["auto_ticks"]


def test_snapshot_restores_identical_future(dave):
    dave.reset("level1", 0)
    dave.step(RIGHT, 15)
    snap = dave.save_snapshot()
    a = dave.step(frozenset({"jump", "right"}), 30).observation
    restored = dave.load_snapshot(snap)
    assert restored.frame == snap.frame
    b = dave.step(frozenset({"jump", "right"}), 30).observation
    assert _comparable(a) == _comparable(b)


def test_local_observed_viewport(dave):
    obs = dave.reset("level2", 0)
    assert obs.region.min.col == 0 and obs.region.max.col == 19
    assert all(obs.region.contains(t.pos) for t in obs.tiles)
    assert not any(t.name == "door" for t in obs.tiles)  # level 2 door is at column 48


def test_velocity_is_derived(dave):
    dave.reset("level1", 0)
    obs = dave.step(RIGHT, 1).observation
    assert obs.player_velocity_source == "derived" and obs.player_velocity.dx > 0


def test_invalid_inputs_are_actionable(dave):
    with pytest.raises(AdapterError, match="level files 1-9"):
        dave.reset("level42", 0)
    dave.reset("level1", 0)
    with pytest.raises(ValueError, match="unsupported buttons \\['duck'\\]"):
        dave.step(frozenset({"duck"}), 1)


def test_watch_mode_command_line():
    headless = DaveBridgeAdapter(DAVE_DIR)
    watched = DaveBridgeAdapter(DAVE_DIR, watch=True, watch_delay_ms=30)
    assert headless._bridge_args()[1:] == []
    assert watched._bridge_args()[1:] == ["-window", "-delay", "30"]
    assert any("Watch mode" in n for n in watched.capabilities().notes)


def test_monster_sprite_mapping():
    assert monster_type(89) == "spider" and monster_type(100) == "sun" and monster_type(112) == "guard"
    assert monster_type(5) == "unknown"
