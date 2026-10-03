"""Derived events the adapters do not emit, computed from consecutive observations.

- ``inventory_changed``: any inventory value differs from the previous observation.
- ``area_discovered``: the local view contains map columns not seen before this episode.

Both are direct comparisons of observed fields (``certainty="observed"``); nothing is inferred.
"""

from __future__ import annotations

from dave_agent.schemas import Event, Observation


class EventDetector:
    def __init__(self) -> None:
        self._inventory: dict[str, int] | None = None
        self._seen_cols: set[int] = set()

    def reset(self, observation: Observation) -> list[Event]:
        self._inventory = observation.inventory
        self._seen_cols = set()
        return self._discover(observation)

    def observe(self, observation: Observation) -> list[Event]:
        events: list[Event] = []
        before, after = self._inventory, observation.inventory
        if before is not None and after is not None and before != after:
            changes = {k: [before.get(k), after.get(k)] for k in sorted(before.keys() | after.keys())
                       if before.get(k) != after.get(k)}
            events.append(Event(event_type="inventory_changed", episode_id=observation.episode_id,
                                frame=observation.frame, payload={"changes": changes}))
        self._inventory = after
        events.extend(self._discover(observation))
        return events

    def _discover(self, observation: Observation) -> list[Event]:
        region = observation.region
        new = set(range(region.min.col, region.max.col + 1)) - self._seen_cols
        if not new:
            return []
        self._seen_cols |= new
        return [
            Event(
                event_type="area_discovered",
                episode_id=observation.episode_id,
                frame=observation.frame,
                payload={"level_id": observation.level_id, "min_col": min(new), "max_col": max(new),
                         "new_cols": len(new)},
            )
        ]
