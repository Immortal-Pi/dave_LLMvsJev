"""Estimated reachability (control/reach.py) on the observed Dave level 1 layout. The expected
route is the one verified on the real game with scripts/try_skills.py (docs/skills.md)."""

from pathlib import Path

from dave_agent.config import load_config
from dave_agent.control.reach import ReachMap, iter_jumps, next_waypoint

# Rows 0-10 of level 1 as the tactical grid shows them (row 9 = floor; T trophy, D door).
LEVEL1 = [
    "....................",
    "####################",
    "#$...............$##",
    "#..$...$...T...$..##",
    "#..#...#...#...#..##",
    "#$...$...$...$...$##",
    "##...#...#...#...###",
    "#$.....$..........##",
    "#...####...######.##",
    "##.........#D.....##",
    "####################",
]
KINDS = {"#": "solid", "$": "collectible", "T": "required_item", "D": "exit", ".": "empty", "X": "hazard"}
DAVE = load_config(Path(__file__).resolve().parents[2] / "configs" / "experiments.yaml").skills.reach["dave"]


def reach(rows=LEVEL1):
    return ReachMap({(c, r): KINDS[ch] for r, line in enumerate(rows) for c, ch in enumerate(line)}, DAVE)


def test_simulated_jumps_match_real_game_landings():
    """Landings measured on the real game with scripts/try_skills.py (2026-10-03)."""
    m = reach()
    real = {((2, 9), "jump_right"): (4, 7), ((9, 9), "jump_right"): (11, 7), ((10, 9), "jump_right"): (11, 7),
            ((11, 7), "jump_right"): (13, 5), ((11, 7), "jump_right_short"): (12, 7),
            ((13, 5), "jump_left"): (11, 3), ((13, 5), "jump_left_short"): (12, 7),
            ((6, 7), "jump_right"): (9, 5), ((9, 5), "jump_right"): (11, 3), ((11, 3), "jump_right"): (16, 7)}
    for (start, skill), landing in real.items():
        assert dict(iter_jumps(m, start))[skill] == landing, (start, skill)


def test_route_to_the_trophy_is_the_verified_climb():
    m = reach()
    route = m.path((2, 9), m.targets_for((11, 3)))
    assert route == [((4, 7), "jump"), ((5, 7), "walk"), ((6, 7), "walk"), ((9, 5), "jump"), ((11, 3), "jump")]
    assert [next_waypoint(m, s, (11, 3)) for s in [(2, 9), (10, 9), (11, 7), (13, 5)]] ==         [(4, 7), (11, 7), (9, 5), (11, 3)]


def test_no_jump_under_a_ceiling():
    m = reach()
    assert dict(iter_jumps(m, (5, 9)))["jump_up"] == (5, 9)  # '#' directly above at (5,8)
    assert not any(cell[1] < 9 for cell, kind, _ in m.moves((5, 9)) if kind == "jump")


def test_route_from_the_trophy_to_the_boxed_in_door_goes_round_through_the_gap():
    m = reach()
    cells = [cell for cell, _ in m.path((11, 3), m.targets_for((12, 9)))]
    assert cells[0] == (16, 7) and (17, 9) in cells and cells[-1] == (12, 9)
    assert m.locate(186, 48) == (11, 3)  # standing across two columns: the one with ground under it


def test_hazard_cells_are_never_landing_spots():
    rows = list(LEVEL1)
    rows[9] = "##........X#D.....##"
    m = reach(rows)
    assert not m.standable((10, 9)) and all(cell != (10, 9) for cell, _, _ in m.moves((9, 9)))


def test_unknown_or_unreachable_target_gives_no_waypoint():
    m = reach()
    assert next_waypoint(m, (2, 9), (40, 9)) is None  # never observed
    assert next_waypoint(m, (2, 9), (2, 9)) == (2, 9)  # already there
