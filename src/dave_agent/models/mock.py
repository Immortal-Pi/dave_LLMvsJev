"""Offline mock controller: seeded uniform choice over the offered candidates (ignores memory)."""

import random

from dave_agent.memory.working import MemoryContext
from dave_agent.schemas import Decision, Goal, ModelCallRecord, Observation, SkillCandidate


class SeededMockController:
    def __init__(self, seed: int, label: str) -> None:
        self.provider = "mock"
        self.model = label
        self._rng = random.Random(seed)

    def decide(
        self,
        observation: Observation,
        goal: Goal | None,
        candidates: list[SkillCandidate],
        memory: MemoryContext,
    ) -> tuple[Decision, ModelCallRecord]:
        if not candidates:
            raise ValueError("no candidates offered")
        chosen = self._rng.choice(candidates)
        decision = Decision(
            candidate_id=chosen.candidate_id,
            observation_id=observation.observation_id,
            goal_id=goal.goal_id if goal else None,
        )
        record = ModelCallRecord(
            provider=self.provider, model=self.model, purpose="tactical", latency_ms=0.0, status="ok"
        )
        return decision, record
