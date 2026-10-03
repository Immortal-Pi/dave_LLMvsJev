"""Jev decisions response contract (verified 2026-10-03; see docs/feasibility.md).

Parsing only. The live client arrives in Phase 8. Unknown provider fields are kept,
and fields absent from a response stay None rather than defaulting to zero.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, model_validator


class JevChoiceAnswer(BaseModel):
    model_config = ConfigDict(extra="allow", frozen=True)

    type: Literal["choice"]
    choice: str
    probabilities: dict[str, float] | None = None
    confidence: float | None = None

    @model_validator(mode="after")
    def _choice_in_distribution(self) -> JevChoiceAnswer:
        if self.probabilities is not None and self.choice not in self.probabilities:
            raise ValueError(f"choice {self.choice!r} missing from probabilities")
        return self


class JevUsage(BaseModel):
    model_config = ConfigDict(extra="allow", frozen=True)

    input_tokens: int | None = None
    output_tokens: int | None = None
    cost: float | None = None  # USD as reported by OpenRouter


class JevResponse(BaseModel):
    model_config = ConfigDict(extra="allow", frozen=True)

    model: str
    answers: dict[str, JevChoiceAnswer]
    usage: JevUsage | None = None
    id: str | None = None
    provider: str | None = None


def parse_choice(response: dict, question_id: str, allowed: set[str]) -> JevChoiceAnswer:
    """Validate a decisions response and return one choice answer within ``allowed``."""
    parsed = JevResponse.model_validate(response)
    if question_id not in parsed.answers:
        raise ValueError(f"response has no answer for question {question_id!r}")
    answer = parsed.answers[question_id]
    if answer.choice not in allowed:
        raise ValueError(f"Jev chose {answer.choice!r}, which is not an offered candidate")
    return answer
