"""Azure OpenAI Chat Completions client and the LLM strategic planner.

Plain httpx; credentials come from the environment (``.env``, see ``.env.example``) and are
never logged. Transport failures (timeouts, 429, 5xx) are retried up to ``max_retries`` with
exponential backoff; output validation and its retry belong to the goal manager.
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
from dave_agent.logging_setup import redact
from dave_agent.models.planner import PlanningRequest
from dave_agent.schemas import ModelCallRecord

log = logging.getLogger(__name__)

RETRY_STATUS = frozenset({408, 429, 500, 502, 503, 504})


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
        self._client = httpx.Client(timeout=timeout_seconds, transport=transport)

    @property
    def url(self) -> str:
        s = self.settings
        return f"{s.endpoint.rstrip('/')}/openai/deployments/{s.deployment}/chat/completions"

    def complete(self, body: dict) -> ChatResult:
        started = time.monotonic()
        attempt, error, status = 0, None, "error"
        while True:
            try:
                response = self._client.post(self.url, params={"api-version": self.settings.api_version},
                                             headers={"api-key": self.settings.api_key}, json=body)
            except httpx.TimeoutException as exc:
                status, error = "timeout", f"timeout: {type(exc).__name__}"
            except httpx.HTTPError as exc:
                status, error = "error", f"transport: {type(exc).__name__}"
            else:
                if response.status_code == 200:
                    return self._parse(response.json(), started, attempt)
                status = "error"
                error = redact(f"HTTP {response.status_code}: {response.text[:300]}")
                if response.status_code not in RETRY_STATUS:
                    break
            if attempt >= self.max_retries:
                break
            attempt += 1
            self._sleep(min(2.0 ** attempt, 8.0))
        log.warning("azure chat call failed: %s", error)
        return ChatResult(status=status, text=None, latency_ms=_ms(started), retries=attempt, error=error)

    def _parse(self, data: dict, started: float, attempt: int) -> ChatResult:
        choice = (data.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        return ChatResult(status="ok", text=message.get("content"), latency_ms=_ms(started), retries=attempt,
                          usage=_usage(data.get("usage")), response_id=data.get("id"),
                          finish_reason=choice.get("finish_reason"),
                          error=message.get("refusal"))

    def close(self) -> None:
        self._client.close()


def _ms(started: float) -> float:
    return round((time.monotonic() - started) * 1000, 1)


# Only rules verified in docs/feasibility.md section 3 (deadly-dave source) are stated here.
SYSTEM_PROMPT = """You are the strategic planner for an agent playing Dangerous Dave (the deadly-dave reimplementation).
You choose the agent's next objective. A separate tactical controller executes short movement skills toward it.

Game rules (verified):
- Dave walks left/right, jumps, and can fire the gun only after collecting it. There is no ducking and no health bar: Dave has lives.
- The level is completed by touching the door while holding the trophy. Touching the door without the trophy does nothing.
- Fire, water and vines set Dave burning; touching a monster or its plasma does too. Burning ends in death, then Dave respawns at the level start. The game is over at 0 lives.
- Loot (gems etc.) only adds score. The gun lets Dave shoot monsters. Falling off the bottom of the screen wraps to the top; it is not a death.
- Coordinates are tile (col, row); row 0 is the top. Only the local view and what was seen earlier this episode are known.

Choose exactly one goal id from `candidates`. Prefer progress toward completing the level, avoid repeating a goal that just failed, and consider the trigger that caused this planning call.
Reply with JSON {"goal": "<candidate id>", "rationale": "<one short sentence, at most 200 characters>"}. Give a brief rationale only, not step-by-step reasoning."""


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
        request_ref = "sha256:" + hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()[:16]
        record = ModelCallRecord(provider=self.provider, model=self.model, purpose="planner",
                                 latency_ms=result.latency_ms, retries=result.retries, status=result.status,
                                 usage=result.usage, request_ref=request_ref, response_ref=result.response_id)
        log.debug("planner call %s status=%s finish=%s", request_ref, result.status, result.finish_reason)
        return (result.text if result.status == "ok" else None), record
