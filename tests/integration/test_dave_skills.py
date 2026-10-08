"""Phase 3 acceptance on the real game: calibrated skills from identical snapshots.
Skipped when the deadly-dave bridge is not built."""

import pytest

from dave_agent.adapters.dave import BRIDGE_EXE, DaveBridgeAdapter
from dave_agent.control.skills import execute, generate_candidates, revalidate

from ..conftest import ROOT

DAVE_DIR = ROOT / "external" / "deadly-dave"
pytestmark = [
    pytest.mark.dave,
    pytest.mark.skipif(not (DAVE_DIR / BRIDGE_EXE).exists(), reason="deadly-dave bridge not built"),
]


@pytest.fixture(scope="module")
def dave():
    adapter = DaveBridgeAdapter(DAVE_DIR)
    yield adapter
    adapter.close()


@pytest.fixture
def catalog(config):
    return config.skills.for_adapter("dave")


def _run(dave, catalog, config, obs, skill):
    offered = generate_candidates(catalog, dave.capabilities().buttons, obs)
    candidate = next(c for c in offered.candidates if c.skill == skill)
    return execute(dave, candidate, catalog, obs, config.skills.executor)


def _trace(run):
    return (run.outcome, run.reason, run.input_ticks,
            [s.observation.model_dump(exclude={"observation_id"}) for s in run.steps])


@pytest.mark.parametrize(
    "skill", ["move_right_1", "move_left_3", "jump_up", "jump_right", "jump_right_short", "wait_short"]
)
def test_skill_repeats_identically_from_snapshot(dave, catalog, config, skill):
    dave.reset("level3", 0)
    snap = dave.save_snapshot()
    traces = [_trace(_run(dave, catalog, config, dave.load_snapshot(snap), skill)) for _ in range(3)]
    assert traces[0] == traces[1] == traces[2]


def test_jumps_complete_on_landing_within_cap(dave, catalog, config):
    obs = dave.reset("level3", 0)
    run = _run(dave, catalog, config, obs, "jump_right")
    spec = next(s for s in catalog if s.name == "jump_right")
    assert run.outcome == "completed" and run.input_ticks <= spec.max_frames
    assert run.observation.player_state in ("standing", "walking") and run.observation.grounded
    assert run.observation.player_position.x - obs.player_position.x == 94  # calibrated flat long jump


def test_walk_into_fire_interrupts_and_later_candidate_is_rejected(dave, catalog, config):
    obs = dave.reset("level2", 0)  # fire floor from column 3
    stale = generate_candidates(catalog, dave.capabilities().buttons, obs)
    run = None
    for _ in range(5):
        run = _run(dave, catalog, config, obs, "move_right_1")
        obs = run.observation
        if run.outcome != "completed":
            break
    assert run.outcome == "interrupted" and run.reason == "hazard_contact"
    assert obs.player_state == "burning"
    # Burning ignores input: only the precondition-free wait is legal now.
    offered = generate_candidates(catalog, dave.capabilities().buttons, obs)
    assert [c.skill for c in offered.candidates] == ["wait_long"]
    old_move = next(c for c in stale.candidates if c.skill == "move_right_1")
    assert revalidate(old_move, catalog, obs) == "precondition:alive"


def test_shoot_masked_without_gun_then_fires_one_bullet(dave, catalog, config):
    obs = dave.reset("level3", 0)
    buttons = dave.capabilities().buttons
    assert generate_candidates(catalog, buttons, obs).masked["shoot"] == "has_gun"
    # Scripted route to the gun at tile (10,4) (see scripts/calibrate_skills.py).
    obs = _run(dave, catalog, config, obs, "jump_right").observation
    for _ in range(39):
        obs = dave.step(frozenset({"left"}), 1).observation
    obs = _run(dave, catalog, config, obs, "wait_short").observation
    for _ in range(6):
        if obs.player_state == "jumping":
            break
        obs = dave.step(frozenset({"jump", "right"}), 1).observation
    for _ in range(66):
        obs = dave.step(frozenset({"right"}), 1).observation
    while obs.player_state == "jumping":
        obs = dave.step(frozenset(), 1).observation
    assert obs.inventory["gun"] == 1 and obs.facing == "right"

    snap = dave.save_snapshot()
    results = []
    for _ in range(2):
        start = dave.load_snapshot(snap)
        run = _run(dave, catalog, config, start, "shoot")
        bullets = [e for e in run.observation.entities if e.entity_type == "bullet"]
        results.append((run.outcome, [b.position.model_dump() for b in bullets]))
        assert run.outcome == "completed" and len(bullets) == 1
        assert bullets[0].position.x == start.player_position.x + 8
        assert generate_candidates(catalog, buttons, run.observation).masked["shoot"] == "no_bullet"
    assert results[0] == results[1]
    # Turn and shoot: one walk tick turns him and steps him 2 px, then the bullet flies left.
    start = dave.load_snapshot(snap)
    assert "shoot_left" in {c.skill for c in generate_candidates(catalog, buttons, start).candidates}
    run = _run(dave, catalog, config, start, "shoot_left")
    bullets = [e for e in run.observation.entities if e.entity_type == "bullet"]
    assert run.outcome == "completed" and run.observation.facing == "left" and len(bullets) == 1
    assert 0 < start.player_position.x - run.observation.player_position.x <= 2
    assert bullets[0].position.x < run.observation.player_position.x
