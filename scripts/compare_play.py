"""Compare a recorded human play (scripts/record_play.py) with the agent's skill catalog.

Replays the key log headless (the game is deterministic) and cuts it into moves:

- **Jumps**, from the tick Up was pressed on the ground to the landing. Each is compared with
  every catalog jump traced from the same start (control/reach.py ``trace_skill``, cooldown
  included) and with "hold the direction h ticks, then let go" for every h. A jump no catalog
  skill lands within ``--tolerance`` px of is a move the agent cannot make; when a hold-h jump
  matches, the missing piece is a hold length, otherwise it is a mid-air change (a reversal, a
  re-press). The threat screen's verdict at the take-off is shown too: a jump you survived that
  the screen masks is a screen disagreement.
- **Walks and waits** on the ground: their lengths against the catalog's 24/72-tick walks and
  6/24-tick waits.
- **Fire**, standing, walking or in the air (the catalog fires only standing, between skills).
- **Deaths** and their cause (``threats.contact_cause``).
- **Decisions:** how often your keys changed, on the ground and in the air; the agent decides
  only between skills and never in the air.
- **Timing near monsters:** for each jump started with a monster whose fire pattern is known on
  screen, how long you stood idle before it, and when the forecast says the matching catalog jump
  is first safe (``threats.safe_window``): do you and the forecast pick the same moments?

Usage: uv run python scripts/compare_play.py artifacts/human/level4-....jsonl [--tolerance 6]
       [--all]   # list every jump, not only the ones the catalog misses
"""

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

from dave_agent.adapters import create_adapter
from dave_agent.adapters.dave import BUTTON_KEYS
from dave_agent.config import load_config
from dave_agent.control.reach import TILE, ReachMap, takeoff_delay, trace_skill
from dave_agent.control.skills import generate_candidates
from dave_agent.control.threats import assess_timing, contact_cause, safe_window, screen

KEY_BUTTONS = {v: k for k, v in BUTTON_KEYS.items()}
GROUND = ("standing", "walking")


def learn(cells: dict, obs) -> None:
    r = obs.region
    for col in range(r.min.col, r.max.col + 1):
        for row in range(r.min.row, r.max.row + 1):
            cells.setdefault((col, row), "empty")
    for t in obs.tiles:
        cells[(t.pos.col, t.pos.row)] = t.kind


def runs(keys: list[str]) -> str:
    """'J J R R R - -' -> 'J2 R3 -2'."""
    out: list[list] = []
    for k in keys:
        if out and out[-1][0] == k:
            out[-1][1] += 1
        else:
            out.append([k, 1])
    return " ".join(f"{k}{n}" for k, n in out)


def tile(x: int, y: int) -> str:
    return f"({(x + 8) // TILE},{(y + 8) // TILE})"


def best_hold(reach: ReachMap, x: int, y: int, direction: int, cooldown: int, land: tuple[int, int]):
    """(error px, hold ticks) of the "hold the direction h ticks" jump landing nearest ``land``."""
    wait = takeoff_delay(cooldown)
    best = None
    for hold in range(0, 121, 2):
        path = reach.trace(x, y, direction, hold, stop_on_hazard=False)[0] or [(x, y)]
        end = path[-1]
        err = abs(end[0] - land[0]) + abs(end[1] - land[1])
        if best is None or err < best[0]:
            best = (err, hold, wait + len(path))
    return best


class Jump:
    def __init__(self, t: int, obs, cells: dict, verdict: dict) -> None:
        self.t, self.obs, self.cells, self.verdict = t, obs, cells, verdict
        self.keys: list[str] = []
        self.airborne = False
        self.idle = 0  # ticks standing with no key just before Up
        self.monsters = any(e.motion and e.entity_id.startswith("monster") for e in obs.entities)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("log", type=Path)
    parser.add_argument("--config", type=Path, default=Path("configs/watch.yaml"))
    parser.add_argument("--tolerance", type=int, default=6, help="px: a landing this close counts as a match")
    parser.add_argument("--all", action="store_true", help="list every jump")
    parser.add_argument("--json", type=Path, help="also write the per-move table here")
    args = parser.parse_args()

    lines = args.log.read_text(encoding="utf-8").splitlines()
    header, ticks = json.loads(lines[0]), [json.loads(s) for s in lines[1:]]
    config = load_config(args.config)
    catalog = config.skills.for_adapter("dave")
    specs = {s.name: s for s in catalog}
    jumps_specs = [s for s in catalog if "jump" in s.phases[0].buttons and "jetpacking" not in s.preconditions]
    rcfg, tcfg = config.skills.reach["dave"], config.skills.executor.threats
    adapter = create_adapter("dave", config.environment)

    cells: dict = {}
    moves: list[dict] = []
    walks, idles, fires = [], [], Counter()
    ground_changes = air_changes = ground_ticks = air_ticks = 0
    deaths: list[str] = []
    drift = 0
    jump: Jump | None = None
    run_keys, run_len = None, 0
    idle_before = 0
    try:
        obs = adapter.reset(header["scenario"], 0)
        if obs.build_id != header["build_id"]:
            print(f"warning: recorded on build {header['build_id']}, replaying on {obs.build_id}")
        buttons = adapter.capabilities().buttons
        prev_keys = "-"
        for rec in ticks:
            k = rec["keys"]
            learn(cells, obs)
            state = obs.player_state
            grounded = state in GROUND
            changed = k != prev_keys
            if grounded:
                ground_ticks += 1
                ground_changes += changed
            elif state in ("jumping", "freefalling"):
                air_ticks += 1
                air_changes += changed
            if "F" in k and "F" not in prev_keys:
                fires[{"standing": "standing", "walking": "walking"}.get(state, "in the air")] += state in GROUND + ("jumping", "freefalling")

            # Ground runs: one key combination held while on the ground.
            g = k.replace("F", "") or "-"
            if grounded and g == run_keys:
                run_len += 1
            else:
                if run_keys in ("L", "R"):
                    walks.append(run_len)
                elif run_keys == "-":
                    idles.append(run_len)
                idle_before = run_len if run_keys == "-" else 0
                run_keys, run_len = (g, 1) if grounded else (None, 0)

            # Jumps: from Up pressed on the ground to the landing.
            if jump is None and grounded and "J" in k:
                offered = generate_candidates(catalog, buttons, obs).candidates
                contacts, _ = assess_timing(obs, ReachMap(cells, rcfg), offered, specs, tcfg)
                _, masked = screen(offered, contacts)
                verdict = {c.skill: masked.get(c.candidate_id) for c in offered}
                jump = Jump(rec["t"], obs, dict(cells), verdict)
                jump.idle = idle_before
            if jump is not None:
                jump.keys.append(k)
                if not jump.airborne and "J" not in k:
                    jump = None  # Up let go before the take-off: no jump

            prev_keys = k
            result = adapter.step(frozenset(KEY_BUTTONS[c] for c in k if c != "-"), 1)
            new = result.observation
            p = new.player_position
            if p is not None and rec["x"] is not None and (p.x, p.y) != (rec["x"], rec["y"]):
                drift += 1
            if new.player_state == "burning" and state != "burning":
                cause, where = contact_cause(new)
                deaths.append(f"tick {rec['t']} at {tile(p.x, p.y)} px ({p.x},{p.y}): {cause}"
                              f"{' (in a jump started at tick %d)' % jump.t if jump else ''}")
            if jump is not None and new.player_state in ("jumping", "freefalling"):
                jump.airborne = True
            if jump is not None and jump.airborne and (new.player_state in GROUND or new.player_state == "burning"):
                moves.append(judge(jump, new, rec["t"], rcfg, jumps_specs, args.tolerance, tcfg))
                jump = None
            obs = new
            if obs.terminal != "running":
                break
    finally:
        adapter.close()

    report(header, ticks, moves, walks, idles, fires, deaths, drift, ground_changes, ground_ticks, air_changes,
           air_ticks, args)
    if args.json:
        args.json.write_text(json.dumps(moves, indent=1), encoding="utf-8")
    return 0


def judge(jump: Jump, landed, t_end: int, rcfg, jump_specs, tol: int, tcfg=None) -> dict:
    s = jump.obs.player_position
    reach = ReachMap(jump.cells, rcfg).opened()
    land = (landed.player_position.x, landed.player_position.y)
    took = t_end - jump.t + 1
    air = [k for k in jump.keys if k != "J"] or ["-"]
    dirs = [c for k in jump.keys for c in k if c in "LR"]
    held_dirs = set(dirs)
    # Mid-air pattern: a reversal (both directions), or a direction pressed again after a gap.
    seq = runs([("L" if "L" in k else "R" if "R" in k else "-") for k in jump.keys])
    presses = sum(1 for part in seq.split() if part[0] in "LR")
    fits = []
    for spec in jump_specs:
        path = trace_skill(reach, s.x, s.y, spec, cooldown=jump.obs.jump_cooldown or 0)
        end = path[-1]
        fits.append((abs(end[0] - land[0]) + abs(end[1] - land[1]), spec.name, len(path), end))
    fits.sort()
    err, name, ticks, end = fits[0]
    direction = 1 if dirs.count("R") >= dirs.count("L") else -1
    hold = best_hold(reach, s.x, s.y, direction if dirs else 0, jump.obs.jump_cooldown or 0, land)
    if landed.player_state == "burning":
        kind = "died"
    elif err <= tol:
        kind = "catalog"
    elif hold[0] <= tol:
        kind = "hold length"
    elif len(held_dirs) > 1:
        kind = "mid-air reversal"
    elif presses > 1:
        kind = "re-press in the air"
    else:
        kind = "other"
    masked = jump.verdict.get(name)
    timing = None
    if jump.monsters and tcfg is not None:
        spec = next(s for s in jump_specs if s.name == name)
        window = safe_window(jump.obs, ReachMap(jump.cells, rcfg), spec, tcfg)
        timing = {"idle": jump.idle, "first_safe": None if window is None else window[0],
                  "safe_for": None if window is None else window[1]}
    return {
        "timing": timing,
        "tick": jump.t, "from": tile(s.x, s.y), "from_px": [s.x, s.y], "to": tile(*land), "to_px": list(land),
        "ticks": took, "keys": runs(jump.keys), "fire_in_air": any("F" in k for k in air),
        "match": kind, "best_skill": name, "best_err_px": err, "best_skill_end": list(end),
        "best_skill_ticks": ticks, "hold": {"err_px": hold[0], "ticks": hold[1]},
        "screen_on_best": masked, "screen_masked": sorted(n for n, why in jump.verdict.items() if why),
    }


def hist(values: list[int], edges: list[int]) -> str:
    out, lo = [], 0
    for hi in edges + [10 ** 9]:
        n = sum(1 for v in values if lo <= v < hi)
        if n:
            out.append(f"{lo}-{hi - 1 if hi < 10 ** 9 else ''}: {n}")
        lo = hi
    return ", ".join(out) or "none"


def report(header, ticks, moves, walks, idles, fires, deaths, drift, gc, gt, ac, at, args) -> None:
    secs = len(ticks) * 0.014
    print(f"{args.log.name}: {header['scenario']}, {len(ticks)} ticks ({secs:.0f} s of game time)")
    if drift:
        print(f"warning: the replay differs from the recording on {drift} ticks (other build or settings?)")
    print(f"\nKey changes: on the ground {gc} in {gt} ticks ({gc / max(gt, 1) * 71.4:.1f}/s), "
          f"in the air {ac} in {at} ticks ({ac / max(at, 1) * 71.4:.1f}/s). The agent never changes keys in "
          f"the air; on the ground it picks a skill of 6-72 ticks.")

    kinds = Counter(m["match"] for m in moves)
    print(f"\nJumps: {len(moves)}. " + ", ".join(f"{k}: {n}" for k, n in kinds.most_common()))
    print(f"  catalog skill closest per jump: {dict(Counter(m['best_skill'] for m in moves if m['match'] == 'catalog'))}")
    holds = [m["hold"]["ticks"] for m in moves if m["match"] == "hold length"]
    if holds:
        print(f"  hold lengths the catalog lacks (it has 32 and until-landing): {sorted(holds)}")
    disagree = [m for m in moves if m["match"] != "died" and m["screen_on_best"]]
    if disagree:
        print(f"  survived but masked by the threat screen: {len(disagree)} "
              f"(ticks {[m['tick'] for m in disagree][:20]})")
    air_fire = sum(m["fire_in_air"] for m in moves)
    shown = [m for m in moves if args.all or m["match"] not in ("catalog",)]
    for m in shown:
        print(f"  tick {m['tick']:5d} {m['from']}->{m['to']} {m['ticks']:3d}t [{m['match']}] keys {m['keys']}")
        print(f"       nearest skill {m['best_skill']} ends {m['best_err_px']} px off at {m['best_skill_end']}; "
              f"best hold {m['hold']['ticks']} ticks ({m['hold']['err_px']} px off)"
              f"{'; screen masks it: ' + m['screen_on_best'] if m['screen_on_best'] else ''}")

    near = [m for m in moves if m.get("timing")]
    if near:
        now = [m for m in near if m["timing"]["first_safe"] == 0]
        unsafe = [m for m in near if m["timing"]["first_safe"] != 0]
        print(f"\nTiming near monsters: {len(near)} jumps with a monster's fire pattern on screen. "
              f"The forecast says {len(now)} were safe when you started, {len(unsafe)} were not "
              f"({sum(m['match'] == 'died' for m in unsafe)} of those died).")
        for m in near:
            t = m["timing"]
            first = "never within 160 ticks" if t["first_safe"] is None else \
                ("now" if t["first_safe"] == 0 else f"in {t['first_safe']} ticks")
            print(f"  tick {m['tick']:5d} {m['from']}->{m['to']} as {m['best_skill']}: you stood {t['idle']} ticks first; "
                  f"forecast: safe {first}{' for %s ticks' % t['safe_for'] if t['safe_for'] else ''}"
                  f"{'  DIED' if m['match'] == 'died' else ''}")

    print(f"\nWalks on the ground ({len(walks)}), ticks: {hist(walks, [6, 12, 24, 25, 48, 72, 73])}. "
          f"The catalog walks 24 or 72 ticks; shorter steps place Dave more finely.")
    print(f"Waits on the ground ({len(idles)}), ticks: {hist(idles, [3, 6, 7, 12, 24, 25, 48])}. "
          f"The catalog waits 6 or 24.")
    print(f"Fire: {dict(fires) or 'none'}{f' ({air_fire} during jumps)' if air_fire else ''}. "
          f"The catalog fires only standing, between skills.")
    print(f"\nDeaths: {len(deaths)}")
    for d in deaths:
        print(f"  {d}")


if __name__ == "__main__":
    sys.exit(main())
