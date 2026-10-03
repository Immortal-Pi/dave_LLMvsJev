"""Azure OpenAI Chat Completions client, the LLM strategic planner and the LLM tactical model.

Plain httpx; credentials come from the environment (``.env``, see ``.env.example``) and are
never logged. Transport failures (timeouts, 429, 5xx) are retried up to ``max_retries`` with
exponential backoff; output validation and its retry belong to the goal manager (planner)
and ``models.tactical.ModelController`` (tactical).
No price table is assumed, so ``cost_usd`` stays None; token usage is recorded as reported.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from collections.abc import Callable
from dataclasses import dataclass, field

import httpx
from dotenv import load_dotenv

from dave_agent.config import ConfigError
from dave_agent.models.http import post_json
from dave_agent.models.planner import PlanningRequest
from dave_agent.models.tactical import GAME_RULES, INPUT_GUIDE, TACTICAL_TASK, TacticalChoice, TacticalRequest, \
    parse_tactical
from dave_agent.schemas import ModelCallRecord

log = logging.getLogger(__name__)



@dataclass(frozen=True)
class AzureSettings:
    endpoint: str
    api_key: str = field(repr=False)
    api_version: str
    deployment: str

    @classmethod
    def from_env(cls, deployment_env: str) -> AzureSettings:
        load_dotenv()
        names = {"endpoint": "AZURE_OPENAI_ENDPOINT", "api_key": "AZURE_OPENAI_API_KEY",
                 "api_version": "AZURE_OPENAI_API_VERSION", "deployment": deployment_env}
        values = {k: os.environ.get(v, "").strip() for k, v in names.items()}
        missing = [names[k] for k, v in values.items() if not v]
        if missing:
            raise ConfigError(f"Azure OpenAI is not configured: set {', '.join(missing)} in .env (see .env.example)")
        return cls(**values)


@dataclass(frozen=True)
class ChatResult:
    status: str  # ok | error | timeout
    text: str | None
    latency_ms: float
    retries: int
    usage: dict[str, float] | None = None
    response_id: str | None = None
    finish_reason: str | None = None
    error: str | None = None


def _usage(raw: dict | None) -> dict[str, float] | None:
    if not raw:
        return None
    usage = {k: float(raw[k]) for k in ("prompt_tokens", "completion_tokens", "total_tokens") if k in raw}
    reasoning = (raw.get("completion_tokens_details") or {}).get("reasoning_tokens")
    if reasoning is not None:
        usage["reasoning_tokens"] = float(reasoning)
    return usage or None


class AzureChatClient:
    def __init__(self, settings: AzureSettings, timeout_seconds: float, max_retries: int,
                 transport: httpx.BaseTransport | None = None, sleep: Callable[[float], None] = time.sleep) -> None:
        self.settings, self.max_retries, self._sleep = settings, max_retries, sleep
        self.timeout_seconds = timeout_seconds
        self._client = httpx.Client(timeout=timeout_seconds, transport=transport)

    @property
    def url(self) -> str:
        s = self.settings
        return f"{s.endpoint.rstrip('/')}/openai/deployments/{s.deployment}/chat/completions"

    def complete(self, body: dict) -> ChatResult:
        result = post_json(self._client, self.url, params={"api-version": self.settings.api_version},
                           headers={"api-key": self.settings.api_key}, body=body,
                           max_retries=self.max_retries, sleep=self._sleep)
        if result.status != "ok":
            log.warning("azure chat call failed: %s", result.error)
            return ChatResult(status=result.status, text=None, latency_ms=result.latency_ms,
                              retries=result.retries, error=result.error)
        data = result.data or {}
        choice = (data.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        return ChatResult(status="ok", text=message.get("content"), latency_ms=result.latency_ms,
                          retries=result.retries, usage=_usage(data.get("usage")), response_id=data.get("id"),
                          finish_reason=choice.get("finish_reason"), error=message.get("refusal"))

    def close(self) -> None:
        self._client.close()



SYSTEM_PROMPT = f"""You are the strategic planner for an agent playing Dangerous Dave (the deadly-dave reimplementation).
You choose the agent's next objective. A separate tactical controller executes short movement skills toward it.

{GAME_RULES}

Choose exactly one goal id from `candidates`. Prefer progress toward completing the level, avoid repeating a goal that just failed, and consider the trigger that caused this planning call.
Reply with JSON {{"goal": "<candidate id>", "rationale": "<one short sentence, at most 200 characters>"}}. Give a brief rationale only, not step-by-step reasoning."""


def request_payload(request: PlanningRequest) -> dict:
    """The user-message JSON: the request without fields that only repeat the candidate ids."""
    return request.model_dump(mode="json", exclude={"observation_id"})


class AzurePlanner:
    provider = "azure_openai"

    def __init__(self, client: AzureChatClient, max_completion_tokens: int = 2000,
                 reasoning_effort: str | None = "low") -> None:
        self.client = client
        self.model = client.settings.deployment
        self.max_completion_tokens = max_completion_tokens
        self.reasoning_effort = reasoning_effort

    def settings(self) -> dict:
        return _effective(self.client, self.max_completion_tokens, self.reasoning_effort)

    def body(self, request: PlanningRequest, feedback: str | None = None) -> dict:
        messages = [{"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": json.dumps(request_payload(request), separators=(",", ":"))}]
        if feedback:
            messages.append({"role": "user", "content": f"Your previous answer was rejected: {feedback}. "
                                                        "Answer again with one offered goal id."})
        schema = {"type": "object", "additionalProperties": False, "required": ["goal", "rationale"],
                  "properties": {"goal": {"type": "string", "enum": list(request.candidate_ids)},
                                 "rationale": {"type": "string"}}}
        body = {"messages": messages, "max_completion_tokens": self.max_completion_tokens,
                "response_format": {"type": "json_schema",
                                    "json_schema": {"name": "plan_choice", "strict": True, "schema": schema}}}
        if self.reasoning_effort:
            body["reasoning_effort"] = self.reasoning_effort
        return body

    def propose(self, request: PlanningRequest, feedback: str | None = None) -> tuple[str | None, ModelCallRecord]:
        body = self.body(request, feedback)
        result = self.client.complete(body)
        record = ModelCallRecord(provider=self.provider, model=self.model, purpose="planner",
                                 latency_ms=result.latency_ms, retries=result.retries, status=result.status,
                                 usage=result.usage, request_ref=_request_ref(body), response_ref=result.response_id)
        log.debug("planner call %s status=%s finish=%s", record.request_ref, result.status, result.finish_reason)
        return (result.text if result.status == "ok" else None), record


def _effective(client: AzureChatClient, max_completion_tokens: int, reasoning_effort: str | None) -> dict:
    """Effective model settings recorded with every run (no credentials)."""
    return {"provider": "azure_openai", "deployment": client.settings.deployment,
            "api_version": client.settings.api_version, "max_completion_tokens": max_completion_tokens,
            "reasoning_effort": reasoning_effort, "timeout_seconds": client.timeout_seconds,
            "transport_retries": client.max_retries}


def _request_ref(body: dict) -> str:
    return "sha256:" + hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()[:16]


TACTICAL_PROMPT = f"""You are the tactical controller for an agent playing Dangerous Dave (the deadly-dave reimplementation).
The game is paused while you decide. You choose the next short skill; it runs to completion (or until interrupted by danger), then you choose again.

{GAME_RULES}

Input (JSON):
{INPUT_GUIDE}

{TACTICAL_TASK}
Reply with JSON {{"candidate_id": "<one offered id>"}} and nothing else."""


class AzureTacticalModel:
    """LLM tactical model (arm A): one offered candidate id per call, enforced by a strict schema."""

    provider = "azure_openai"

    def __init__(self, client: AzureChatClient, max_completion_tokens: int = 2000,
                 reasoning_effort: str | None = "low") -> None:
        self.client = client
        self.model = client.settings.deployment
        self.max_completion_tokens = max_completion_tokens
        self.reasoning_effort = reasoning_effort

    def settings(self) -> dict:
        return _effective(self.client, self.max_completion_tokens, self.reasoning_effort)

    def body(self, request: TacticalRequest, feedback: str | None = None) -> dict:
        payload = request.model_dump(mode="json", exclude={"episode_id", "observation_id"})
        messages = [{"role": "system", "content": TACTICAL_PROMPT},
                    {"role": "user", "content": json.dumps(payload, separators=(",", ":"))}]
        if feedback:
            messages.append({"role": "user", "content": f"Your previous answer was rejected: {feedback}. "
                                                        "Answer again with one offered candidate id."})
        schema = {"type": "object", "additionalProperties": False, "required": ["candidate_id"],
                  "properties": {"candidate_id": {"type": "string", "enum": list(request.candidate_ids)}}}
        body = {"messages": messages, "max_completion_tokens": self.max_completion_tokens,
                "response_format": {"type": "json_schema",
                                    "json_schema": {"name": "skill_choice", "strict": True, "schema": schema}}}
        if self.reasoning_effort:
            body["reasoning_effort"] = self.reasoning_effort
        return body

    def propose(self, request: TacticalRequest, feedback: str | None = None) -> tuple[str | None, ModelCallRecord]:
        body = self.body(request, feedback)
        result = self.client.complete(body)
        record = ModelCallRecord(provider=self.provider, model=self.model, purpose="tactical",
                                 latency_ms=result.latency_ms, retries=result.retries, status=result.status,
                                 usage=result.usage, request_ref=_request_ref(body), response_ref=result.response_id)
        log.debug("tactical call %s status=%s finish=%s", record.request_ref, result.status, result.finish_reason)
        return (result.text if result.status == "ok" else None), record

    def parse(self, text: str | None, allowed: tuple[str, ...]) -> TacticalChoice:
        return parse_tactical(text, allowed)
