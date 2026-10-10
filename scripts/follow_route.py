"""Play the real game with the rule planner and a controller that always takes the ``route:`` option.

A free check of the route notes (control/goals.py): when the tactical model follows the plan, does
Dave get where the plan says? The controller is ``RouteFollower`` (``runner/follower.py``). The same
goal manager, threat screen and executor as every arm; nothing is recorded. ``scripts/sweep.py``
runs it over levels, seeds and config values.

Usage: uv run python scripts/follow_route.py --scenario level3 [--frames 6000] [--verbose]
"""

import argparse
from collections import Counter
from pathlib import Path

from dave_agent.adapters import create_adapter
from dave_agent.config import load_config
from dave_agent.control.goals import GoalManager
from dave_agent.memory.working import WorkingMemory
from dave_agent.models.planner import RuleMockPlanner
from dave_agent.runner.episode import run_episode
from dave_agent.runner.follower import RouteFollower


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--scenario", default="level3")
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--config", type=Path, default=Path("configs/watch.yaml"))
    parser.add_argument("--frames", type=int, default=6000)
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    config = load_config(args.config)
    adapter = create_adapter("dave", config.environment)
    goals = GoalManager(RuleMockPlanner(), config.planning, config.models.max_retries,
                        reach=config.skills.reach.get("dave"), threats=config.skills.executor.threats)
    follower = RouteFollower(args.verbose)
    try:
        result = run_episode(adapter, follower, config.skills.for_adapter("dave"), config.skills.executor,
                             WorkingMemory.from_config(config), args.scenario, args.seed, args.frames, goals=goals)
    finally:
        adapter.close()
    events = Counter(e.event_type for e in result.events)
    goals_trace = [e.payload.get("target_ref") for e in result.events if e.event_type == "goal_set"]
    achieved = [e.payload.get("target_ref") for e in result.events if e.event_type == "goal_achieved"]
    print(f"{args.scenario}: {result.outcome} ({result.termination_reason}), {result.frames} frames, "
          f"{len(result.decisions)} decisions ({follower.followed} on a route note, {follower.unguided} without)")
    print(f"  score {result.score}, lives {result.lives}, deaths {events['death']}, "
          f"inventory {result.executions[-1].observation.inventory if result.executions else None}")
    print(f"  goals set: {goals_trace}")
    print(f"  achieved:  {achieved}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
