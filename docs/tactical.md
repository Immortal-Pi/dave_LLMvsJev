# Tactical controllers: the shared request, retry, fallback and budgets

| File | Contents |
| --- | --- |
| `src/dave_agent/models/tactical.py` | `TacticalRequest` and `tactical_request` (what every model sees), `parse_tactical`, the `TacticalModel` protocol, `ModelController` (retry, fallback, budgets), `BudgetExhausted`, `SeededMockModel`, `ScriptedTacticalModel` |
| `src/dave_agent/models/azure.py` | `AzureTacticalModel` (arm A): the system prompt and a strict JSON-schema choice |
| `src/dave_agent/models/jev.py` | `JevTacticalModel` (arms B and C): one Jev `choice` question via the OpenRouter decisions route; `JevClient`, `JevSettings`, the response contract (`JevResponse`, `parse_choice`) |
| `src/dave_agent/models/http.py` | `post_json`: the transport retry loop shared by the Azure and Jev clients |
| `src/dave_agent/models/base.py` | `TacticalController.decide(observation, goal, candidates, memory) -> (Decision, calls)` |

The tactical controller chooses **how** to pursue the planner's goal: one bounded skill at a time, from the candidates trusted code offers (`docs/skills.md`). Every model-backed arm goes through `ModelController`, so arms A, B and C share the request, validation, retry, fallback and budget code. Arm A uses the Azure model and arms B and C the Jev model (they differ only by the learned graph).

## The request (`TacticalRequest`)

`tactical_request(observation, goal, candidates, memory.context())` builds the request deterministically. It holds only current and recent structured state:

| Field | Contents |
| --- | --- |
| `player` | tile, pixel position, movement state, grounded, facing, velocity |
| `lives`, `inventory`, `score` | as observed |
| `view` | `origin` (top-left tile) and one string per row of the local view. The legend is `GRID_LEGEND`: `#` solid, `X` hazard, `$` loot, `T` trophy, `D` door, `\|` climbable, `I` gun/jetpack, `M` monster, `*` plasma/bullet, `@` Dave, `.` empty. The grid re-encodes `Observation.tiles` and `entities`; nothing is added. |
| `entities` | visible monsters and shots with tile and velocity |
| `goal` | goal type, target id, success predicate, `waypoint` and `waypoint_offset` (tiles from Dave), constraints, frames left |
| `progress`, `recent` | `MemoryContext.progress` and the last `memory.context_entries` skills (outcome, reason, events, end tile) |
| `candidates` | id, description and frame limit of every offered skill. A skill tried before from here first gets experience notes (this episode for every arm, past runs for graph arms; `docs/memory.md`, "Experience notes"). On Dave the description then ends with the skill's estimated end tile from `control/reach.py` (see `docs/planner.md`, "Reachability waypoints"); the waypoint is the next landing spot on the estimated route, or the planner's next waypoint. A skill predicted to touch plasma, a monster or a hazard is led by `danger: touches … in N ticks`; such skills are removed before the request is built, unless every skill has a contact (`docs/skills.md`, "Threat prediction and the candidate screen"). The others end with `no threat predicted` |

There are no tools, no free-form planning and no growing transcript: each call sees one request.

The provider-neutral text lives in `models/tactical.py` and is sent to every provider: `GAME_RULES` (shared with the planner), `INPUT_GUIDE` (what each field means, including the grid legend) and `TACTICAL_TASK`.

**Context digest.** `context_digest(request)` is a sha256 prefix of the canonical request JSON, without the episode id. `ModelController` puts it on every decision (`Decision.context_digest`, stored in `decisions.context_digest`; NULL for forced decisions). Two arms with equal digests at a decision were shown identical context. This is how A/B/C parity shows in the logs (tested in `test_arm_parity.py`).

## Output, retry and fallback

- **Output:** exactly `{"candidate_id": "<offered id>"}`. `parse_tactical` rejects non-JSON, extra keys and ids that were not offered (`TacticalOutputError`).
- **Retry:** an invalid answer is marked `invalid_output` and asked again with the validation error as feedback. A failed call (`timeout`, `error`, after the client's own transport retries) is asked again without feedback. There are at most `models.max_retries` (1) re-asks per decision: the same policy as the planner.
- **Deterministic legal fallback:** after the last attempt, the decision is the first offered skill named in `tactical.fallback_skills` (`[wait_short, wait]`), else the first offered candidate. It is logged with `Decision.fallback = true`, `fallback_reason` (`invalid_output`, `call_timeout`, `call_error` or `budget:<name>`) and a `decision_fallback` event.
- **Counting:** a fallback is **never** a model decision. The `play` summary reports `model_decisions`, `fallback_decisions`, `fallback_reasons`, `forced_decisions` (one legal candidate, no call), `tactical_calls`, `tactical_failures` (by status) and latency.

## Budgets

| Budget | Setting | When exhausted |
| --- | --- | --- |
| Tactical calls per episode (retries included) | `tactical.max_calls_per_episode` (400) | `tactical.on_budget_exhausted` |
| Tokens per episode (reported `total_tokens`) | `tactical.max_tokens_per_episode` (1,000,000; null disables) | same |
| Cost per episode (only when the provider reports cost) | `tactical.max_cost_usd_per_episode` (null) | same |
| Simulation frames | `benchmark.max_episode_frames` | episode `truncated`, `max_frames:<n>` |
| Wall time | `benchmark.max_episode_wall_seconds` | episode `truncated`, `budget:wall_time:<s>s` |
| Planner calls | `planning.max_calls_per_episode` | fallback goal (`docs/planner.md`) |

- Model budgets are checked **before** every call, and the counters reset when a new episode id is seen.
- With `on_budget_exhausted: terminate` (the default), the controller raises `BudgetExhausted`. The runner logs the calls already made for that decision, ends the episode as `truncated` with `termination_reason = budget:<name>`, and emits an `episode_truncated` event whose payload names the budget.
- With `fallback`, play continues with fallback decisions and no further calls.
- Azure does not report cost. Tokens are recorded per call. Without a price table `cost_usd` stays null; with `models.<role>.price` set (Phase 9), each Azure call gets an *estimated* `cost_usd` (`cost_source: estimated`, price source and date in `output`). See `docs/benchmark.md`.

## Traceability

- Every executed skill has exactly one decision (`decisions.seq` = `skill_executions.decision_seq`).
- `EpisodeRecorder.record(..., calls, ...)` stores every call for the decision (retries included) as `model_calls` rows. `decisions.model_call_seq` points to the last call.
- `model_failure` events (with `purpose=tactical`) are emitted for every non-ok call.
- Each run stores its effective settings (deployment, api-version, `reasoning_effort`, `max_completion_tokens`, timeout, transport retries, the `tactical:` policy, budgets) under `effective_settings` in `runs.config_json`. The `play` summary prints them under `settings`. No credentials are included.

## Azure tactical model (`--tactical live`, arm A)

- Same client, endpoint and transport retries as the planner (`docs/planner.md`). Settings come from `models.tactical_llm` (`max_completion_tokens`, `reasoning_effort`).
- The system prompt states the verified rules (`GAME_RULES`, shared with the planner), the input fields, the grid legend and the output format. `response_format` is a strict `json_schema` whose `candidate_id` is an enum of the offered ids.
- `play --tactical live` is allowed only for arms with `tactical: llm`. It checks the Azure settings before the game starts, and prints the paid-run budget to stderr before any call. Run modes are `live-tactical`, or `live` with `--planner live`.
- `probe-provider --provider azure --purpose tactical` makes one call on the fixture start observation.

### Verified 2026-10-03 (`gpt-5.4-mini`, `reasoning_effort: low`)

- **Probe:** status ok, 1420 ms, 765 prompt + 83 completion tokens (61 reasoning), valid choice.
- **Live arm A smoke, Dave level 1** (`play --arm A --planner live --tactical live --adapter dave --scenario level1`, run `p7-live-smoke-1`):
  - 9 tactical calls, all ok, 0 fallbacks; tactical latency mean 2.8 s (p50 2.7 s, max 4.2 s); 14.5k tactical tokens (10.5k prompt, 4.1k completion of which 3.8k reasoning);
  - 2 planner calls (no_goal, stuck), both ok, both choosing the trophy;
  - truncated at 600 frames (skills take 24–99 frames, so 9 decisions); score 0, no deaths. Dave walked right toward the trophy, then repeated `jump_up` below it;
  - the exported episode replays exactly.

This is a smoke test of the integration, not a performance result.

## Jev tactical model (`--tactical live`, arms B and C)

Payload sent to `POST https://openrouter.ai/api/alpha/decisions` (`models.jev`):

| Field | Contents |
| --- | --- |
| `model` | `models.jev.model_id` (`typesafe/jev-1.13`) |
| `state` | exactly the JSON arm A gets as its user message (the request without `episode_id` and `observation_id`), plus `rules` (`GAME_RULES`) and `input_guide` (`INPUT_GUIDE`), which arm A gets in its system prompt |
| `questions.skill` | `type: choice`; `instructions` = `TACTICAL_TASK` plus "Which offered candidate skill should Dave run next?"; `criteria` = `{candidate_id: "<description> (at most N frames)"}` for every offered candidate |

- **Answer:** `answers.skill.choice` must be an offered id (`parse_choice`). Anything else, or a malformed response, is `invalid_output`, then retry, then fallback, as for arm A. The retry does not send feedback: Jev is stateless and its answer is limited to the criteria.
- **`provider_score`:** `probabilities[choice]`, meaning "Jev choice probability of the chosen candidate over the offered candidates (provider-reported); not a correctness estimate". It is None if the response has no distribution. `confidence` is not used to make decisions; calibrating it is Phase 11 work.
- **Preserved provider fields:**
  - `ModelCallRecord.output` (`model_calls.output_json`) keeps `probabilities`, `confidence`, the dated response `model` and `provider`;
  - `model` on the call record is the dated version (e.g. `typesafe/jev-1.13-20260917`);
  - `usage` is `input_tokens` and `output_tokens` as reported, plus a derived `total_tokens` (their sum, only when both are reported) so the shared token budget counts Jev like Azure;
  - `cost_usd` is the reported cost (`cost_source: provider_reported`).
  Absent fields stay None, never 0.
- **Transport:** the same timeout and transport retries as Azure (`models.timeout_seconds`, `models.max_retries`). 529 (documented "overloaded") is retried too.
- **Credentials:** `OPENROUTER_API_KEY` is checked before the game starts. It is sent only in the `Authorization` header and never logged or recorded (tested).
- **Probe:** `probe-provider --provider jev [--save-fixture]` makes one tactical call on the fixture start observation. `--save-fixture` writes the request and response to `tests/fixtures/jev/tactical_response.json`; the request body holds no credentials.
- The cost budget stays null, because it could only ever fire for Jev (Azure reports no cost). The call and token budgets apply to every arm.

### Verified 2026-10-03 (`typesafe/jev-1.13-20260917`)

- **Probe:** ok, 343 ms, 1245 input + 78 output tokens, $5.23e-05. Choice `c1_move_right`, p 0.73 (next `c4_jump_right` 0.15), confidence 0.68.
- **Live smoke, Dave level 1, seed 0** (`play --arm X --planner live --tactical live --adapter dave --scenario level1`, store `artifacts/p8-live.sqlite`, runs `p8-live-A|B|C`, all labeled `mode=live`):

| Arm | Decisions (model / fallback) | Tactical latency mean (p50, max) | Tactical tokens | Cost | Planner calls | Frames, outcome |
| --- | --- | --- | --- | --- | --- | --- |
| A (Azure) | 10 / 0 | 2535 ms (2648, 3777) | 15.7k (3.8k reasoning) | not reported | 2 (no_goal, stuck) | 635, truncated |
| B (Jev) | 12 / 0 | 236 ms (163, 720) | 26.1k | $0.00102 (Jev calls; Azure planner cost not reported) | 3 (no_goal, stuck x2) | 622, truncated |
| C (Jev + graph) | 9 / 0 | 177 ms (159, 306) | 19.3k | $0.00076 | 3 (no_goal, stuck x2) | 682, truncated |

  - No arm scored or died; every skill completed. Each run is one smoke episode, **not** a performance result.
  - **Parity:** A and B have the same first-decision `context_digest` (`sha256:7b21dadb6e2ea12d`); they then diverge because the models chose differently. C's first digest differs because its live planner chose `explore:right` (graph arms also see route summaries) where A and B chose the trophy, and the goal is part of the request.
  - Jev's chosen-candidate probabilities were mostly 0.24–0.52: the distribution is spread across walking and jumping skills.
  - All three exports replay with no mismatches, and contain no credentials.

- **After the reachability change** (run `diag-jev-3`, `configs/watch.yaml`, Jev tactical with the rule planner): **level 1 completed** with 11 decisions, 470 frames, $0.001, no deaths, score 1300. The two runs before it (`diag-jev-1` with no waypoint, `diag-jev-2` with the waypoint only) each used all 400 calls without leaving the floor.

## Known limitations

- Reasoning tokens are most of the completion (about 430 per call), which roughly doubles latency against the probe. `reasoning_effort` values below `low` were not tested on this deployment.
- The fixed `max_episode_frames: 600` is fixture scale and allows only about 9 Dave skills. Calibrate it in Phase 9.
- The LLM sees the view grid and the goal, but nothing about which jumps reach which ledges. The learned graph (arm C) is the intended source of that.
- Jev's state is a nested JSON object with a text grid. Whether another rendering (for example per-candidate outcome summaries) helps Jev is untested; any change must go to both arms.
