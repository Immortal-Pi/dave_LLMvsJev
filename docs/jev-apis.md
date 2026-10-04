# TypeSafe Jev APIs

TypeSafe's **System One models** (Jev) are reached through OpenRouter. Two access paths are in use.

## Chat completions (OpenRouter SDK)

- Client: `openrouter` SDK — `OpenRouter(api_key=...).chat.send(...)`
- Model: `typesafe/jev-router`
- Used for multimodal prompts, e.g. a `text` part plus an `image_url` part ("Describe this image.").
- Read the answer from `response.choices[0].message.content`.

## Decisions endpoint (raw HTTP)

- `POST https://openrouter.ai/api/alpha/decisions` via `requests`
- Model: `typesafe/jev-1.13`
- Headers: `Authorization: Bearer <OPENROUTER_API_KEY>`, `Content-Type: application/json`
- Payload:
  - `state` — free-text description of the current situation
  - `questions` — map of name → question. Each question has a `type` and `instructions`. Types seen so far:
    - `choice` — needs a `criteria` dict of option → description
    - `noul` — a yes/no-style judgment
- Verified response shape (2026-10-03, see `docs/feasibility.md` §4):
  `answers.<id> = {type, choice, probabilities{option: p}, confidence}` plus
  `usage{input_tokens, output_tokens, cost}`, `id`, `provider`. The response `model` is a
  dated version, e.g. `typesafe/jev-1.13-20260917`.
- `confidence` describes how concentrated `probabilities` is; it is not a correctness score.
- `state` may be a string, an object or an array. The harness sends an object (see `docs/tactical.md`).
- The harness client, parser and tactical model are in `src/dave_agent/models/jev.py`. `scripts/probe_jev.py` refreshes the minimal contract fixture `tests/fixtures/jev/choice_response.json`; `dave-agent probe-provider --provider jev --save-fixture` captures a real tactical request and response in `tests/fixtures/jev/tactical_response.json`.

```python
payload = {
    "model": "typesafe/jev-1.13",
    "state": "The player has 4 HP remaining and three enemies are approaching.",
    "questions": {
        "danger": {"type": "noul", "instructions": "Is the player currently in serious danger?"}
    },
}
```

## Configuration

Both paths read `OPENROUTER_API_KEY` from `.env` via `python-dotenv` (`load_dotenv()`).

## Further reference

For the full TypeSafe/Jev API surface, load the `typesafe-ai` skill. It is vendored at `.claude/skills/typesafe-ai/` (mirrored in `.agents/skills/`, tracked in `skills-lock.json`) and points to the live docs and cookbooks.
