"""Jev (TypeSafe System One) via the OpenRouter decisions route: response contract, client and
the tactical model for arms B and C.

Contract verified 2026-10-03 (docs/feasibility.md section 4). Unknown provider fields are kept,
and fields absent from a response stay None rather than defaulting to zero. Credentials come
from the environment (``.env``) and are never logged or recorded.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Literal

import httpx
from dotenv import load_dotenv
from pydantic import BaseModel, ConfigDict, ValidationError, model_validator

from dave_agent.config import ConfigError, JevModelConfig
from dave_agent.models.http import RETRY_STATUS, HttpResult, post_json
from dave_agent.models.tactical import GAME_RULES, INPUT_GUIDE, TACTICAL_TASK, TacticalChoice, \
    TacticalOutputError, TacticalRequest
from dave_agent.schemas import ModelCallRecord

log = logging.getLogger(__name__)

# 529: "overloaded" in the TypeSafe API docs; retried like 429 and 5xx.
JEV_RETRY_STATUS = RETRY_STATUS | {529}
QUESTION_ID = "skill"
SCORE_MEANING = ("Jev choice probability of the chosen candidate over the offered candidates "
                 "(provider-reported); not a correctness estimate")


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


@dataclass(frozen=True)
class JevSettings:
    endpoint: str
    model_id: str
    api_key: str = field(repr=False)

    @classmethod
    def from_env(cls, cfg: JevModelConfig) -> JevSettings:
        load_dotenv()
        key = os.environ.get(cfg.api_key_env, "").strip()
        if not key:
            raise ConfigError(f"Jev is not configured: set {cfg.api_key_env} in .env (see .env.example)")
        return cls(endpoint=cfg.endpoint, model_id=cfg.model_id, api_key=key)


class JevClient:
    def __init__(self, settings: JevSettings, timeout_seconds: float, max_retries: int,
                 transport: httpx.BaseTransport | None = None, sleep: Callable[[float], None] = time.sleep) -> None:
        self.settings, self.timeout_seconds, self.max_retries, self._sleep = \
            settings, timeout_seconds, max_retries, sleep
        self._client = httpx.Client(timeout=timeout_seconds, transport=transport)

    def decide(self, body: dict) -> HttpResult:
        result = post_json(self._client, self.settings.endpoint,
                           headers={"Authorization": f"Bearer {self.settings.api_key}"}, body=body,
                           max_retries=self.max_retries, retry_status=JEV_RETRY_STATUS, sleep=self._sleep)
        if result.status != "ok":
            log.warning("jev decisions call failed: %s", result.error)
        return result

    def close(self) -> None:
        self._client.close()


def _usage(usage: JevUsage | None) -> dict[str, float] | None:
    """Token counts as reported. ``total_tokens`` is derived (input + output) only when both are
    reported, so the shared token budget counts Jev the same way as Azure."""
    if usage is None:
        return None
    out = {k: float(v) for k, v in (("input_tokens", usage.input_tokens), ("output_tokens", usage.output_tokens))
           if v is not None}
    if len(out) == 2:
        out["total_tokens"] = out["input_tokens"] + out["output_tokens"]
    return out or None


class JevTacticalModel:
    """Jev tactical model (arms B and C): one choice question over the offered candidate ids.

    ``state`` is exactly the JSON the LLM arm gets as its user message, plus the same rules,
    input guide and task text the LLM gets in its system prompt.
    """

    provider = "openrouter_jev"

    def __init__(self, client: JevClient) -> None:
        self.client = client
        self.model = client.settings.model_id

    def settings(self) -> dict:
        """Effective settings recorded with every run (no credentials)."""
        c = self.client
        return {"provider": self.provider, "endpoint": c.settings.endpoint, "model_id": c.settings.model_id,
                "timeout_seconds": c.timeout_seconds, "transport_retries": c.max_retries}

    def body(self, request: TacticalRequest) -> dict:
        state = {"rules": GAME_RULES, "input_guide": INPUT_GUIDE,
                 **request.model_dump(mode="json", exclude={"episode_id", "observation_id"})}
        criteria = {c["id"]: f"{c['description']} (at most {c['max_frames']} frames)" for c in request.candidates}
        return {"model": self.model, "state": state,
                "questions": {QUESTION_ID: {"type": "choice", "instructions": f"{TACTICAL_TASK} Which offered "
                                            "candidate skill should Dave run next?", "criteria": criteria}}}

    def propose(self, request: TacticalRequest, feedback: str | None = None) -> tuple[str | None, ModelCallRecord]:
        # Jev is stateless and its answer is constrained to the criteria, so feedback is not sent.
        body = self.body(request)
        result = self.client.decide(body)
        data = result.data if result.status == "ok" else None
        model, usage, cost, output = self.model, None, None, None
        if data is not None:
            try:
                parsed = JevResponse.model_validate(data)
            except ValidationError:
                parsed = None  # parse() reports it as invalid output
            if parsed is not None:
                model, usage = parsed.model, _usage(parsed.usage)
                cost = None if parsed.usage is None else parsed.usage.cost
                answer = parsed.answers.get(QUESTION_ID)
                output = {"model": parsed.model, "provider": parsed.provider,
                          "probabilities": None if answer is None else answer.probabilities,
                          "confidence": None if answer is None else answer.confidence}
        record = ModelCallRecord(provider=self.provider, model=model, purpose="tactical",
                                 latency_ms=result.latency_ms, retries=result.retries, status=result.status,
                                 usage=usage, cost_usd=cost, cost_source=None if cost is None else "provider_reported",
                                 request_ref=_request_ref(body), response_ref=None if data is None else data.get("id"),
                                 output=output)
        return (None if data is None else json.dumps(data)), record

    def parse(self, text: str | None, allowed: tuple[str, ...]) -> TacticalChoice:
        if not text:
            raise TacticalOutputError("empty output")
        try:
            answer = parse_choice(json.loads(text), QUESTION_ID, set(allowed))
        except (ValueError, ValidationError) as exc:  # JSONDecodeError and ValidationError are ValueErrors
            raise TacticalOutputError(f"invalid Jev response: {str(exc).splitlines()[0][:200]}") from exc
        score = None if answer.probabilities is None else answer.probabilities[answer.choice]
        return TacticalChoice(candidate_id=answer.choice, provider_score=score,
                              provider_score_meaning=None if score is None else SCORE_MEANING)


def _request_ref(body: dict) -> str:
    return "sha256:" + hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()[:16]
