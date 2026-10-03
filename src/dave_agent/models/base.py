"""Tactical controller interface shared by LLM, Jev and mock providers."""

from typing import Protocol

from dave_agent.memory.working import MemoryContext
from dave_agent.schemas import Decision, Goal, ModelCallRecord, Observation, SkillCandidate


class TacticalController(Protocol):
    provider: str
    model: str

    def decide(
        self,
        observation: Observation,
        goal: Goal | None,
        candidates: list[SkillCandidate],
        memory: MemoryContext,
    ) -> tuple[Decision, ModelCallRecord]: ...
