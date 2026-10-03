"""Phase 3 skill calibration against the real deadly-dave bridge.

Measures walking, jumping, the landing cooldown, the gun and the burn duration from
fixed scripted starts. Each measurement runs twice from the same snapshot and must
match exactly (determinism). The results are checked against ``configs/skills.yaml``
and saved to ``artifacts/calibration/skills.json``. Exits 1 if any check fails.

Usage: uv run python scripts/calibrate_skills.py [--out artifacts/calibration] [--watch]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from dave_agent.adapters.dave import DaveBridgeAdapter
from dave_agent.config import load_config

ROOT = Path(__file__).resolve().parents[1]
TILE = 16


def tick(adapter, *buttons):
    return adapter.step(frozenset(buttons), 1).observation


def hold(adapter, buttons, n):
    obs = None
    for _ in range(n):
        obs = tick(adapter, *buttons)
    return obs


def until(adapter, buttons, predicate, limit):
    """Hold buttons until predicate(obs); return (ticks used, obs) or (None, obs) on timeout."""
    obs = None
    for i in range(1, limit + 1):
        obs = tick(adapter, *buttons)
        if predicate(obs):
            return i, obs
    return None, obs


def airborne_jump(obs):
    return obs.player_state == "jumping"


def landed(obs):
    return obs.grounded and obs.player_state in ("standing", "walking")


def twice(adapter, measure):
    """Run measure() from a snapshot twice; both results must be identical."""
    snap = adapter.save_snapshot()
    first = measure()
    adapter.load_snapshot(snap)
    second = measure()
    return first, first == second


# -- measurements ---------------------------------------------------------------------------
def walk(adapter):
    adapter.reset("level1", 0)

    def measure():
        x0 = adapter.observe().player_position.x
        xs = [tick(adapter, "right").player_position.x - x0 for _ in range(72)]
        released = [tick(adapter).player_state for _ in range(3)]
        # Dave moves 2 px on the first tick of every 3-tick walk cycle.
        return {"dx_by_tick": xs, "first_tick_at_one_tile": xs.index(TILE) + 1 if TILE in xs else None,
                "states_after_release": released}

    return twice(adapter, measure)


def jump_up(adapter):
    adapter.reset("level1", 0)

    def measure():
        y0 = adapter.observe().player_position.y
        start, _ = until(adapter, ("jump",), airborne_jump, 10)
        ys, n = [], 0
        while True:
            obs = tick(adapter)
            n += 1
            ys.append(obs.player_position.y)
            if landed(obs) or n > 300:
                break
        return {"press_ticks_to_start": start, "ticks_to_land": n, "apex_dy": y0 - min(ys)}

    return twice(adapter, measure)


def jump_hold(adapter):
    """Holding Up through a landing: does the game jump again?"""
    adapter.reset("level1", 0)

    def measure():
        states = [tick(adapter, "jump").player_state for _ in range(130)]
        restarts = sum(1 for a, b in zip(states, states[1:]) if a != "jumping" and b == "jumping")
        # Ticks spent not jumping between the first landing and the re-jump.
        gap = None
        if restarts:
            first_land = next(i for i in range(1, len(states)) if states[i] != "jumping")
            gap = next(i for i in range(first_land, len(states)) if states[i] == "jumping") - first_land
        return {"rejumps_while_held": restarts, "ticks_on_ground_before_rejump": gap}

    return twice(adapter, measure)


def landing_cooldown(adapter):
    """After landing, how many ticks of held Up until a new jump starts?"""
    adapter.reset("level1", 0)
    until(adapter, ("jump",), airborne_jump, 10)
    until(adapter, (), landed, 300)

    def measure():
        ticks, _ = until(adapter, ("jump",), airborne_jump, 20)
        return {"press_ticks_to_start_after_landing": ticks}

    return twice(adapter, measure)


def jump_side(adapter, air_ticks):
    """Jump right on level 3's flat floor; hold right for air_ticks (None = until landing)."""
    adapter.reset("level3", 0)

    def measure():
        x0 = adapter.observe().player_position.x
        start, _ = until(adapter, ("jump", "right"), airborne_jump, 10)
        n, obs = 0, None
        while True:
            held = ("right",) if air_ticks is None or n < air_ticks else ()
            obs = tick(adapter, *held)
            n += 1
            if landed(obs) or n > 300:
                break
        return {"press_ticks_to_start": start, "ticks_to_land": n,
                "dx": obs.player_position.x - x0, "lives_lost": 4 - obs.lives}

    return twice(adapter, measure)


def gun(adapter):
    """Scripted route to level 3's gun at tile (10,4), then fire."""
    adapter.reset("level3", 0)
    until(adapter, ("jump", "right"), airborne_jump, 10)
    until(adapter, ("right",), landed, 300)
    hold(adapter, ("left",), 39)
    hold(adapter, (), 6)
    until(adapter, ("jump", "right"), airborne_jump, 10)
    hold(adapter, ("right",), 66)
    obs = until(adapter, (), landed, 300)[1]
    picked = obs.inventory["gun"]

    def measure():
        before = adapter.observe()
        first = tick(adapter, "fire")
        bullets = [e for e in first.entities if e.entity_type == "bullet"]
        xs = [b.position.x for b in bullets]
        held = [next((e.position.x for e in tick(adapter, "fire").entities if e.entity_type == "bullet"), None)
                for _ in range(10)]
        return {"facing": before.facing, "dave_x": before.player_position.x,
                "bullet_spawn_dx": xs[0] - before.player_position.x if xs else None,
                "bullet_x_while_fire_held": held}

    result, same = twice(adapter, measure)
    result["has_gun_after_route"] = picked
    return result, same


def climb_attempt(adapter):
    """Level 5 tree trunk at column 4: does Up on the tree start a climb?"""
    adapter.reset("level5", 0)

    def measure():
        ticks, obs = until(adapter, ("right",), lambda o: False, 25)
        obs = tick(adapter, "jump")
        return {"state_after_up_on_tree": obs.player_state}

    return twice(adapter, measure)


def burn(adapter):
    """Level 2: walk right into the fire floor; ticks from ignition to the death event."""
    adapter.reset("level2", 0)

    def measure():
        ticks, _ = until(adapter, ("right",), lambda o: o.player_state == "burning", 400)
        n = 0
        while True:
            step = adapter.step(frozenset(), 1)
            n += 1
            if any(e.event_type == "death" for e in step.events) or n > 600:
                break
        return {"ticks_to_ignite": ticks, "burn_ticks_to_death": n}

    return twice(adapter, measure)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=ROOT / "artifacts" / "calibration")
    parser.add_argument("--watch", action="store_true")
    parser.add_argument("--watch-delay", type=int, default=14, metavar="MS")
    args = parser.parse_args()

    catalog = {s.name: s for s in load_config(ROOT / "configs" / "experiments.yaml").skills.for_adapter("dave")}
    adapter = DaveBridgeAdapter(ROOT / "external" / "deadly-dave", watch=args.watch, watch_delay_ms=args.watch_delay)
    try:
        m = {}
        for name, fn in [
            ("walk", walk), ("jump_up", jump_up), ("jump_hold", jump_hold), ("landing_cooldown", landing_cooldown),
            ("jump_right", lambda a: jump_side(a, None)), ("jump_right_short", lambda a: jump_side(a, 32)),
            ("gun", gun), ("climb", climb_attempt), ("burn", burn),
        ]:
            result, deterministic = fn(adapter)
            m[name] = {**result, "deterministic": deterministic}
        build_id = adapter.capabilities().build_id
    finally:
        adapter.close()

    jr, jrs, short_jump = catalog["jump_right"], catalog["jump_right_short"], catalog["jump_right_short"].phases[1]
    checks = {
        "all measurements deterministic": all(v["deterministic"] for v in m.values()),
        "move_*_1 walks exactly 1 tile":
            m["walk"]["dx_by_tick"][catalog["move_right_1"].phases[0].ticks - 1] == TILE,
        "move_*_3 walks exactly 3 tiles":
            m["walk"]["dx_by_tick"][catalog["move_right_3"].phases[0].ticks - 1] == 3 * TILE,
        "holding Up re-jumps (so jumps release it)": m["jump_hold"]["rejumps_while_held"] >= 1,
        "jump start within phase budget after landing":
            m["landing_cooldown"]["press_ticks_to_start_after_landing"] <= jr.phases[0].max_ticks,
        "wait_short covers the landing cooldown":
            catalog["wait_short"].phases[0].ticks >= m["jump_hold"]["ticks_on_ground_before_rejump"],
        "flat jump lands within landing budget": m["jump_up"]["ticks_to_land"] <= jr.phases[1].max_ticks,
        "jump_right_short air ticks as configured": short_jump.ticks == 32 and m["jump_right_short"]["lives_lost"] == 0,
        "jump_right_short lands within budget": m["jump_right_short"]["ticks_to_land"] <= jrs.max_frames,
        "gun reachable and bullet spawns 8 px ahead":
            m["gun"]["has_gun_after_route"] == 1 and m["gun"]["bullet_spawn_dx"] == 8,
        "one bullet at a time (held fire keeps moving the same bullet)":
            all(b is not None for b in m["gun"]["bullet_x_while_fire_held"])
            and m["gun"]["bullet_x_while_fire_held"] == sorted(m["gun"]["bullet_x_while_fire_held"]),
        "climbing not entered by Up on a tree (stays out of the catalog)":
            m["climb"]["state_after_up_on_tree"] != "climbing" and "climb_up" not in catalog,
    }

    report = {"adapter": "dave", "build_id": build_id, "measurements": m, "checks": checks}
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "skills.json").write_text(json.dumps(report, indent=2), encoding="utf-8")

    w = m["walk"]
    print(f"build {build_id}")
    print(f"walk         : 2 px per 3 ticks; 16 px first at tick {w['first_tick_at_one_tile']}; dx after 1/3/24/72 ticks = "
          f"{w['dx_by_tick'][0]}/{w['dx_by_tick'][2]}/{w['dx_by_tick'][23]}/{w['dx_by_tick'][71]}; "
          f"after release {w['states_after_release']}")
    print(f"jump_up      : start after {m['jump_up']['press_ticks_to_start']} tick(s), apex {m['jump_up']['apex_dy']} px, "
          f"lands after {m['jump_up']['ticks_to_land']} ticks")
    print(f"hold Up      : {m['jump_hold']['rejumps_while_held']} re-jump(s), {m['jump_hold']['ticks_on_ground_before_rejump']} "
          f"ticks on the ground first")
    print(f"cooldown     : held Up starts a jump {m['landing_cooldown']['press_ticks_to_start_after_landing']} tick(s) after landing")
    for k in ("jump_right", "jump_right_short"):
        print(f"{k:<13}: dx {m[k]['dx']} px over {m[k]['ticks_to_land']} ticks, lives lost {m[k]['lives_lost']}")
    g = m["gun"]
    print(f"gun          : picked {g['has_gun_after_route']}, facing {g['facing']}, bullet +{g['bullet_spawn_dx']} px, "
          f"held-fire xs {g['bullet_x_while_fire_held'][:4]}...")
    print(f"climb        : Up on a tree -> {m['climb']['state_after_up_on_tree']}")
    print(f"burn         : ignite after {m['burn']['ticks_to_ignite']} ticks, death after {m['burn']['burn_ticks_to_death']} ticks")
    print()
    for name, ok in checks.items():
        print(f"  {'ok  ' if ok else 'FAIL'} {name}")
    print(f"\nsaved {args.out / 'skills.json'}")
    return 0 if all(checks.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
