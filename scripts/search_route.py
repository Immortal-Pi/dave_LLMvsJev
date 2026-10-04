"""Breadth-first search for a level route made only of catalog skills, on the real game.

A free, deterministic feasibility check (no model calls): can the current skill catalog finish a
level at all, and if not, where does it stop? From each reached state every legal catalog skill
is tried, restoring the state from a snapshot first (the bridge replays its input log exactly).
Skills that kill or burn Dave are pruned. States are deduplicated by (tile, Dave's state, held
items) by default, or by exact pixel position with ``--key px`` (slower, finer).

Prints JSON: the shortest skill sequence that completes the level (replay it with
``scripts/try_skills.py --scenario LEVEL <skills> --watch``), or, when none is found within the
caps, the frontier: the reached standing tiles, the rightmost and highest ones, and every
(start tile, skill) that killed Dave.

Usage: uv run python scripts/search_route.py --scenario level2 [--max-states 3000] [--max-depth 40]
       [--key tile|px] [--extra-skills trial.yaml] [--out artifacts/search/level2.json]
"""

import argparse
import json
import sys
import time
from collections import deque
from pathlib import Path

import yaml

from dave_agent.adapters import create_adapter
from dave_agent.config import SkillSpec, load_config
from dave_agent.control.skills import execute, generate_candidates

STANDING = {"standing", "walking"}


def tile(obs) -> tuple[int, int] | None:
    p = obs.player_position
    return None if p is None else ((p.x + 8) // 16, (p.y + 8) // 16)


def key(obs, mode: str) -> tuple:
    held = tuple(sorted(k for k, v in obs.inventory.items() if v)) if obs.inventory else ()
    p = obs.player_position
    where = tile(obs) if mode == "tile" else (None if p is None else (p.x, p.y))
    return where, obs.player_state, held


def died(start, run) -> bool:
    obs = run.observation
    return (run.reason in ("death", "hazard_contact") or any(e.event_type == "death" for e in run.events)
            or obs.player_state == "burning" or (obs.lives is not None and start.lives is not None
                                                 and obs.lives < start.lives))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--scenario", default="level2")
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--config", type=Path, default=Path("configs/benchmark_dave.yaml"))
    parser.add_argument("--max-states", type=int, default=3000)
    parser.add_argument("--max-depth", type=int, default=40)
    parser.add_argument("--key", choices=("tile", "px"), default="tile")
    parser.add_argument("--extra-skills", type=Path,
                        help="YAML list of trial skill specs added to the catalog for this search only")
    parser.add_argument("--out", type=Path, help="also write the JSON report here")
    args = parser.parse_args()

    config = load_config(args.config)
    catalog = config.skills.for_adapter("dave")
    if args.extra_skills:
        extra = yaml.safe_load(args.extra_skills.read_text(encoding="utf-8"))
        catalog = catalog + tuple(SkillSpec.model_validate(s) for s in extra)
    adapter = create_adapter("dave", config.environment)
    started = time.monotonic()
    try:
        root = adapter.reset(args.scenario, args.seed)
        buttons = adapter.capabilities().buttons
        seen = {key(root, args.key)}
        # Each queue entry: (snapshot, observation, skill path).
        queue = deque([(adapter.save_snapshot(), root, [])])
        reached: dict[tuple[int, int], list[str]] = {}
        fatal: dict[str, set[str]] = {}
        found, expanded, tried = None, 0, 0
        while queue and found is None and len(seen) < args.max_states:
            snap, obs, path = queue.popleft()
            if len(path) >= args.max_depth:
                continue
            expanded += 1
            for candidate in generate_candidates(catalog, buttons, obs).candidates:
                start = adapter.load_snapshot(snap)
                run = execute(adapter, candidate, catalog, start, config.skills.executor)
                tried += 1
                end, steps = run.observation, path + [candidate.skill]
                if died(start, run):
                    fatal.setdefault(str(tile(start)), set()).add(candidate.skill)
                    continue
                if end.terminal == "level_complete":
                    found = steps
                    break
                if end.terminal != "running":
                    continue
                k = key(end, args.key)
                if k in seen:
                    continue
                seen.add(k)
                if end.player_state in STANDING and tile(end) is not None:
                    reached.setdefault(tile(end), steps)
                queue.append((adapter.save_snapshot(), end, steps))
    finally:
        adapter.close()

    report = {
        "scenario": args.scenario,
        "key": args.key,
        "catalog": [s.name for s in catalog],
        "found": found is not None,
        "route": found,
        "states": len(seen),
        "expanded": expanded,
        "skills_tried": tried,
        "exhausted": not queue and found is None,
        "seconds": round(time.monotonic() - started, 1),
    }
    if found is None:
        tiles = sorted(reached)
        rightmost = max(tiles, key=lambda t: (t[0], -t[1]), default=None)
        highest = min(tiles, key=lambda t: (t[1], -t[0]), default=None)
        report.update({
            "rightmost": rightmost and {"tile": list(rightmost), "route": reached[rightmost]},
            "highest": highest and {"tile": list(highest), "route": reached[highest]},
            "reached_standing_tiles": [list(t) for t in tiles],
            "fatal_from": {t: sorted(s) for t, s in sorted(fatal.items())},
        })
    text = json.dumps(report, indent=2)
    print(text)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text + "\n", encoding="utf-8")
    return 0 if found is not None else 1


if __name__ == "__main__":
    sys.exit(main())
