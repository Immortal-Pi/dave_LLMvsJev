"""A scripted tactical controller that always follows the plan (``scripts/follow_route.py``,
``runner/sweep.py``).

Each decision takes a shot whose note says it hits a monster (mid-air too), else the first
candidate whose description starts with ``route:``; with none, ``wait_short`` (or the first
candidate). It makes no model call, so it is free and deterministic: a stand-in for a tactical
model that carries out the route notes perfectly.
"""

from __future__ import annotations

from dave_agent.memory.working import player_tile
from dave_agent.schemas import Decision


class RouteFollower:
    provider, model = "scripted", "route-follower"

    def __init__(self, verbose: bool = False) -> None:
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
