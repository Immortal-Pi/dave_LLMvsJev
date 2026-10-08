"""The offline scoreboard: sweep config values over levels and seeds with the route follower.

Free and deterministic (rule planner, ``RouteFollower``, the real game). See ``runner/sweep.py``
and docs/benchmark.md "Sweep".

Usage:
  uv run python scripts/sweep.py --out artifacts/sweeps/baseline
  uv run python scripts/sweep.py --grid configs/sweeps/planning.yaml --scenarios level1,level2,level3 \
      --graph warm --out artifacts/sweeps/planning
Grid file: dotted config keys to value lists, e.g. ``graph.credit_penalty: [0.5, 1.0, 2.0]``.
"""

import argparse
import sys
from pathlib import Path

import yaml

from dave_agent.config import ConfigError, load_config
from dave_agent.runner.sweep import GRAPH_MODES, run_sweep


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", type=Path, default=Path("configs/benchmark_dave.yaml"))
    parser.add_argument("--grid", type=Path, help="YAML of dotted config keys to value lists (default: baseline only)")
    parser.add_argument("--scenarios", default="level1,level2,level3,level4")
    parser.add_argument("--seeds", default="0", help="Dave has no randomness: more seeds repeat the same episode")
    parser.add_argument("--frames", type=int, default=18000)
    parser.add_argument("--graph", choices=GRAPH_MODES, default="none")
    parser.add_argument("--train-episodes", type=int, default=3, help="warm only: follower episodes per scenario")
    parser.add_argument("--adapter", default="dave")
    parser.add_argument("--workers", type=int, default=1, help="processes; each (config, scenario) is one unit")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()

    try:
        config = load_config(args.config)
        grid = (yaml.safe_load(args.grid.read_text(encoding="utf-8")) or {}) if args.grid else {}
        summary = run_sweep(
            config, grid, tuple(args.scenarios.split(",")), tuple(int(s) for s in args.seeds.split(",")),
            args.frames, args.graph, args.out, args.adapter, args.train_episodes, workers=args.workers,
            notify=lambda r: print(f"{r['config_id']} {r['scenario']} seed {r['seed']}: {r['outcome']}, "
                                   f"{r['frames']} frames, {r['deaths']} deaths, col {r['furthest_col']}",
                                   file=sys.stderr, flush=True))
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    for s in summary:
        if s["scenario"] == "*":
            print(f"{s['completions']}/{s['episodes']} completed, deaths {s['deaths_mean']}, "
                  f"frames {s['frames_to_complete_mean']}, col {s['furthest_col_unfinished_mean']}: {s['config_id']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
