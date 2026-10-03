"""Shared JSON POST with transport retries for the model providers.

Timeouts, transport errors and the given retryable HTTP statuses are retried up to
``max_retries`` times with exponential backoff (2, 4, 8 s cap). Error text is redacted.
Output validation is not done here; it belongs to the callers' policy code.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass

import httpx

from dave_agent.logging_setup import redact

RETRY_STATUS = frozenset({408, 429, 500, 502, 503, 504})


@dataclass(frozen=True)
class HttpResult:
    status: str  # ok | error | timeout
    data: dict | None
    latency_ms: float
    retries: int
    error: str | None = None


def ms_since(started: float) -> float:
    return round((time.monotonic() - started) * 1000, 1)


def post_json(client: httpx.Client, url: str, *, headers: dict[str, str], body: dict,
              max_retries: int, retry_status: frozenset[int] = RETRY_STATUS,
              params: dict[str, str] | None = None, sleep: Callable[[float], None] = time.sleep) -> HttpResult:
    started = time.monotonic()
    attempt, error, status = 0, None, "error"
    while True:
        try:
            response = client.post(url, params=params, headers=headers, json=body)
        except httpx.TimeoutException as exc:
            status, error = "timeout", f"timeout: {type(exc).__name__}"
        except httpx.HTTPError as exc:
            status, error = "error", f"transport: {type(exc).__name__}"
        else:
            if response.status_code == 200:
                try:
                    data = response.json()
                except ValueError:
                    return HttpResult("error", None, ms_since(started), attempt, "HTTP 200 with a non-JSON body")
                return HttpResult("ok", data, ms_since(started), attempt)
            status = "error"
            error = redact(f"HTTP {response.status_code}: {response.text[:300]}")
            if response.status_code not in retry_status:
                break
        if attempt >= max_retries:
            break
        attempt += 1
        sleep(min(2.0 ** attempt, 8.0))
    return HttpResult(status, None, ms_since(started), attempt, error)
