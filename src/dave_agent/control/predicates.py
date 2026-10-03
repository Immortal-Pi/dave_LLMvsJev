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


PREDICATES: dict[str, Predicate] = {
    "alive": alive,
    "on_ground": on_ground,
    "jumping": jumping,
    "landed": landed,
    "has_gun": has_gun,
    "facing_side": facing_side,
    "no_bullet": no_bullet,
}


def check(name: str, obs: Observation) -> bool:
    return PREDICATES[name](obs)
