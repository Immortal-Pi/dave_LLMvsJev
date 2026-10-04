"""Read-only: the distribution of skill durations (simulation frames per executed skill) in
episode stores, per adapter. Used to derive Dave's frame-based settings (memory window, goal
timeout, planner debounce, episode length) in configs/benchmark_dave.yaml from data rather
than fixture-scale guesses.

    uv run python scripts/skill_frames.py artifacts/watch.sqlite artifacts/p8-live.sqlite [--adapter dave]

Stores of another schema version are skipped with a note (they are opened read-only).
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dave_agent.evaluation.statistics import mean, percentile  # noqa: E402


def frames(paths: list[Path], adapter: str) -> tuple[dict[str, list[int]], dict]:
    by_skill: dict[str, list[int]] = defaultdict(list)
    sources = {}
    for path in paths:
        conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
        try:
            rows = conn.execute(
                "SELECT s.skill, s.frames, s.outcome FROM skill_executions s JOIN episodes e USING (episode_key) "
                "WHERE e.adapter = ?", (adapter,)).fetchall()
            episodes = conn.execute("SELECT count(*), sum(outcome = 'level_complete') FROM episodes "
                                    "WHERE adapter = ?", (adapter,)).fetchone()
        except sqlite3.Error as exc:
            sources[str(path)] = f"skipped: {exc}"
            continue
        finally:
            conn.close()
        sources[str(path)] = {"executions": len(rows), "episodes": episodes[0], "completed": episodes[1]}
        for skill, n, _ in rows:
            by_skill[skill].append(n)
    return by_skill, sources


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("stores", nargs="+", type=Path)
    parser.add_argument("--adapter", default="dave")
    args = parser.parse_args()
    by_skill, sources = frames(args.stores, args.adapter)
    every = [n for values in by_skill.values() for n in values]
    report = {
        "adapter": args.adapter,
        "sources": sources,
        "all": {"n": len(every), "mean": mean(every), "p50": percentile(every, 50), "p75": percentile(every, 75),
                "p95": percentile(every, 95), "max": max(every, default=None)},
        "by_skill": {skill: {"n": len(v), "p50": percentile(v, 50), "p75": percentile(v, 75), "max": max(v)}
                     for skill, v in sorted(by_skill.items())},
    }
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
