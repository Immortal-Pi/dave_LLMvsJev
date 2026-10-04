"""Run a fixed sequence of catalog skills on the real game and report what each one did.

A free, deterministic feasibility check: can the current skill catalog reach a target (e.g. the
level 1 trophy and door) at all? Uses the same candidate generation and executor as every arm.
A skill that is not legal at that point is reported with the failed precondition and skipped.

Usage: uv run python scripts/try_skills.py --scenario level1 move_right_3 jump_right ... [--watch]
       [--extra-skills trial.yaml]   # measure trial skills; they are not added to the catalog
"""

import argparse
import sys
from pathlib import Path

import yaml

from dave_agent.adapters import create_adapter
from dave_agent.config import SkillSpec, load_config
from dave_agent.control.skills import execute, generate_candidates


def tile(obs) -> tuple[int, int] | None:
    p = obs.player_position
    return None if p is None else ((p.x + 8) // 16, (p.y + 8) // 16)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("skills", nargs="+", help="catalog skill names, run in order")
    parser.add_argument("--scenario", default="level1")
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--config", type=Path, default=Path("configs/experiments.yaml"))
    parser.add_argument("--extra-skills", type=Path,
                        help="YAML list of trial skill specs (same format as configs/skills.yaml) added to the "
                             "catalog for this run only; for measuring a skill before adding it")
    parser.add_argument("--watch", action="store_true")
    parser.add_argument("--watch-delay", type=int, default=14)
    args = parser.parse_args()

    config = load_config(args.config)
    catalog = config.skills.for_adapter("dave")
    if args.extra_skills:
        extra = yaml.safe_load(args.extra_skills.read_text(encoding="utf-8"))
        catalog = catalog + tuple(SkillSpec.model_validate(s) for s in extra)
    adapter = create_adapter("dave", config.environment, args.watch, args.watch_delay)
    try:
        obs = adapter.reset(args.scenario, args.seed)
        buttons = adapter.capabilities().buttons
        print(f"start tile {tile(obs)} px ({obs.player_position.x},{obs.player_position.y}) frame {obs.frame}")
        for i, name in enumerate(args.skills):
            offered = generate_candidates(catalog, buttons, obs)
            candidate = next((c for c in offered.candidates if c.skill == name), None)
            if candidate is None:
                print(f"{i:2d} {name:18s} NOT LEGAL: {offered.masked.get(name, 'unknown skill')}")
                continue
            start = obs
            run = execute(adapter, candidate, catalog, obs, config.skills.executor)
            obs = run.observation
            events = sorted({e.event_type for e in run.events} - {"skill_started", "skill_finished", "moved"})
            a, b = start.player_position, obs.player_position
            print(f"{i:2d} {name:18s} {run.outcome:11s} {run.frames:3d}f  {tile(start)} -> {tile(obs)}  "
                  f"px ({a.x},{a.y})->({b.x},{b.y})  state {obs.player_state}  score {obs.score}  "
                  f"inv {obs.inventory}{'  ' + ','.join(events) if events else ''}"
                  f"{'  reason ' + run.reason if run.reason else ''}")
            if obs.terminal != "running":
                print(f"terminal: {obs.terminal}")
                break
    finally:
        adapter.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
