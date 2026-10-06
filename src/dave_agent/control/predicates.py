"""Named observation predicates for skill preconditions and phase ``until`` conditions.

Each predicate reads only the ``Observation`` (never adapter internals), so every arm and
memory component sees the same facts. A predicate whose input field is unavailable
returns False: the skill is masked out rather than offered on a guess.
"""

from __future__ import annotations

from collections.abc import Callable

from dave_agent.schemas import Observation

Predicate = Callable[[Observation], bool]

# States in which Dave accepts movement input (burning/dead/blinking ignore it).
CONTROLLABLE = frozenset({"standing", "walking", "jumping", "climbing", "freefalling", "jetpacking"})


def alive(obs: Observation) -> bool:
    return obs.player_state in CONTROLLABLE


def on_ground(obs: Observation) -> bool:
    """Standing or walking: a jump can start (after the game's hidden 5-tick landing cooldown)."""
    return obs.player_state in ("standing", "walking")


def jumping(obs: Observation) -> bool:
    return obs.player_state == "jumping"


def airborne(obs: Observation) -> bool:
    """Jumping or falling: a step off the platform's end has left the floor."""
    return obs.player_state in ("jumping", "freefalling")


def jetpacking(obs: Observation) -> bool:
    return obs.player_state == "jetpacking"


def not_jetpacking(obs: Observation) -> bool:
    """Walks are walks only without the jetpack: with it on the same keys fly (1 px a tick)."""
    return obs.player_state != "jetpacking"


def has_fuel(obs: Observation) -> bool:
    return obs.inventory is not None and obs.inventory.get("jetpack_fuel", 0) > 0


def landed(obs: Observation) -> bool:
    return obs.grounded is True and obs.player_state in ("standing", "walking")


def has_gun(obs: Observation) -> bool:
    return obs.inventory is not None and obs.inventory.get("gun", 0) > 0


def facing_side(obs: Observation) -> bool:
    return obs.facing in ("left", "right")


def no_bullet(obs: Observation) -> bool:
    """deadly-dave allows one bullet at a time; bullets die at the screen edge, so a
    live bullet is always inside the observed viewport."""
    return not any(e.entity_type == "bullet" for e in obs.entities)


SCROLL_COLS, LAST_SCROLL = 15, 80  # game.c game_adjust_scroll_to_dave: 15-column scrolls, offset at most 80


def screen_still(obs: Observation) -> bool:
    """The screen is not scrolling. While it scrolls (15 ticks) the game moves nothing and ignores
    every key (game.c: game_adjust_scroll_to_dave returns before Dave, monsters and bullets
    tick): mid-scroll the offset is not a multiple of 15, and a scroll starts once Dave is more
    than 280 px into the screen (or less than 30 with the screen scrolled): the next 16 ticks
    are frozen. Level 3: eight shots pressed mid-air while the screen scrolled fired nothing.
    Always True for other adapters."""
    if obs.adapter != "dave":
        return True
    pos, left = obs.player_position, obs.region.min.col
    if left % SCROLL_COLS and left != LAST_SCROLL:
        return False
    if pos is None:
        return True
    delta = pos.x - left * 16
    return not (delta > 320 - 40 and left < LAST_SCROLL) and not (delta < 30 and left > 0)


PREDICATES: dict[str, Predicate] = {
    "alive": alive,
    "on_ground": on_ground,
    "jumping": jumping,
    "landed": landed,
    "has_gun": has_gun,
    "facing_side": facing_side,
    "no_bullet": no_bullet,
    "screen_still": screen_still,
    "jetpacking": jetpacking,
    "airborne": airborne,
    "not_jetpacking": not_jetpacking,
    "has_fuel": has_fuel,
}


def check(name: str, obs: Observation) -> bool:
    return PREDICATES[name](obs)
