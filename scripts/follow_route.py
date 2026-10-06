"""Play the real game with the rule planner and a controller that always takes the ``route:`` option.

A free check of the route notes (control/goals.py): when the tactical model follows the plan, does
Dave get where the plan says? Each decision takes a shot whose note says it hits a monster (mid-air
too), else the first candidate whose description starts with ``route:``; with none, ``wait_short``
(or the first candidate). The same goal manager, threat screen
and executor as every arm; nothing is recorded.

Usage: uv run python scripts/follow_route.py --scenario level3 [--frames 6000] [--verbose]
"""

import argparse
from collections import Counter
from pathlib import Path

from dave_agent.adapters import create_adapter
from dave_agent.config import load_config
from dave_agent.control.goals import GoalManager
from dave_agent.memory.working import WorkingMemory, player_tile
from dave_agent.models.planner import RuleMockPlanner
from dave_agent.runner.episode import run_episode
from dave_agent.schemas import Decision


class RouteFollower:
    provider, model = "scripted", "route-follower"

    def __init__(self, verbose: bool) -> None:
        self.verbose = verbose
        self.followed = self.unguided = 0

    def decide(self, observation, goal, candidates, memory):
        chosen = next((c for c in candidates if "shot: hits" in c.description), None) \
            or next((c for c in candidates if c.description.startswith("route:")), None)
        if chosen is None:
            self.unguided += 1
            chosen = next((c for c in candidates if c.skill == "wait_short"), candidates[0])
        else:
            self.followed += 1
        if self.verbose:
            tile = None if observation.player_position is None else player_tile(observation.player_position)
            where = f"({tile.col},{tile.row})" if tile else "?"
            note = chosen.description.split(";")[0] if chosen.description.startswith(("route:", "shot:")) else "(no route note)"
            print(f"  frame {observation.frame:5d} at {where:8s} x={observation.player_position.x if observation.player_position else '?':>4} "
                  f"goal {goal.target_ref if goal else None} -> {chosen.skill}: {note}")
        return Decision(candidate_id=chosen.candidate_id, observation_id=observation.observation_id), ()


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
