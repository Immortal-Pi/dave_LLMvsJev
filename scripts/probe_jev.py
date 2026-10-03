"""Minimal live probe of the Jev decisions contract via OpenRouter.

Sends one legal-choice request and saves a sanitized response fixture.
Costs one small paid call; requires OPENROUTER_API_KEY in the environment or .env.

Usage: uv run python scripts/probe_jev.py [--model typesafe/jev-1.13] [--no-save]
"""

import argparse
import json
import os
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx
from dotenv import load_dotenv

ENDPOINT = "https://openrouter.ai/api/alpha/decisions"
DEFAULT_MODEL = "typesafe/jev-1.13"
FIXTURE_PATH = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "jev" / "choice_response.json"

# Header names whose values must never be written to fixtures.
SENSITIVE_HEADER_PARTS = ("authorization", "cookie", "key", "token")


def build_payload(model: str) -> dict:
    return {
        "model": model,
        "state": {
            "player": {"tile": [3, 9], "grounded": True, "has_trophy": False},
            "goal": "collect the trophy at tile [7, 9]",
            "nearby": [{"type": "fire", "tile": [5, 10]}],
        },
        "questions": {
            "action": {
                "type": "choice",
                "instructions": "Which candidate skill best advances the goal right now without touching fire?",
                "criteria": {
                    "c0_move_right": "Walk right one step along the floor",
                    "c1_jump_right": "Jump up and to the right",
                    "c2_move_left": "Walk left one step",
                    "c3_wait": "Stay in place",
                },
            }
        },
    }


def sanitize_headers(headers: httpx.Headers) -> dict[str, str]:
    return {
        name: value
        for name, value in headers.items()
        if not any(part in name.lower() for part in SENSITIVE_HEADER_PARTS)
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--no-save", action="store_true", help="print only; do not write the fixture")
    args = parser.parse_args()

    load_dotenv()
    api_key = os.environ.get("OPENROUTER_API_KEY")
    if not api_key:
        print("OPENROUTER_API_KEY is not set; add it to .env (see .env.example).", file=sys.stderr)
        return 2

    payload = build_payload(args.model)
    started = time.monotonic()
    response = httpx.post(
        ENDPOINT,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json=payload,
        timeout=30.0,
    )
    latency_ms = round((time.monotonic() - started) * 1000, 1)

    try:
        body = response.json()
    except ValueError:
        body = {"_non_json_body": response.text[:2000]}

    print(f"HTTP {response.status_code} in {latency_ms} ms")
    print(json.dumps(body, indent=2))
    if response.status_code != 200:
        return 1

    if not args.no_save:
        fixture = {
            "_meta": {
                "endpoint": ENDPOINT,
                "captured_at": datetime.now(UTC).isoformat(timespec="seconds"),
                "http_status": response.status_code,
                "latency_ms": latency_ms,
                "response_headers": sanitize_headers(response.headers),
                "note": "Sanitized live response from scripts/probe_jev.py; no credentials included.",
            },
            "request": payload,
            "response": body,
        }
        FIXTURE_PATH.parent.mkdir(parents=True, exist_ok=True)
        FIXTURE_PATH.write_text(json.dumps(fixture, indent=2) + "\n", encoding="utf-8")
        print(f"Saved fixture to {FIXTURE_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
