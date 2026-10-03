# Strategic planner and goal manager

| File | Contents |
| --- | --- |
| `src/dave_agent/control/goals.py` | `TargetMemory`, `goal_candidates`, `evaluate` (goal lifecycle), `GoalManager` (triggers, debounce, cap, fallback, graph routes) |
| `src/dave_agent/models/planner.py` | `GoalCandidate`, `PlanningRequest`, `PlanChoice`, `parse_plan`, the `StrategicPlanner` protocol, `RuleMockPlanner`, `ScriptedPlanner` |
| `src/dave_agent/models/azure.py` | `AzureChatClient` (httpx, transport retries) and `AzurePlanner` (system prompt, strict JSON schema) |

The planner chooses **what** to do next. The tactical controller (mock for now; LLM in Phase 7, Jev in Phase 8) chooses **how**, one skill at a time. Every arm runs the same goal-manager code with the same `planning:` settings. Graph-enabled arms differ in one way only: Python adds learned-route summaries to the candidates and turns the chosen goal into a route waypoint. A test checks this.

## Candidate goals: the planner chooses, Python validates

The planner picks one id from a list that trusted code builds out of observed facts. It never names a target itself, so "validated against known entities and regions" is a membership check, and an unknown id is rejected.

**`TargetMemory`** is per episode and identical for every arm. It holds:
- the trophy, items, loot and doors seen so far on the current level, each with its tile and whether it was still there when last in view;
- the hazard tiles seen;
- the range of columns seen.

It is the "discovered-map memory" from `docs/feasibility.md` §6: only observations, never learned routes. It resets on a level change.

**Candidates**, in fixed priority order:

| Id | Offered when | Achieved when (`success_predicate`) |
| --- | --- | --- |
| `reach:door:cC:rR` | a door is known **and the trophy is held**, since the door does nothing without it (verified) | `level_complete` |
| `collect:trophy:cC:rR` | the trophy (`required_item`) is known and present | `item_collected_at`: an `item_collected` event at that tile |
| `collect:gun:…` / `collect:jetpack:…` | an `item` is known and present | `item_collected_at` |
| `collect:loot:…` (Dave) / `collect:gem:…` (fixture) | the `planning.nearest_collectibles` (3) nearest known score items | `item_collected_at` |
| `explore:right` | always; the map width is unknown under `local_observed` | `area_discovered` reaching the target column |
| `explore:left` | only while col 0 has not been seen | `area_discovered` |
| `recover:safe` | after a death, stuck, repeated-failure or goal-failure trigger | `standing_at`: standing within 1 column of the end tile of the last skill that ended standing |

- **Description:** each candidate says, in game terms, what it is and how far away (e.g. "Collect the trophy at tile (11,3); 9 tiles right, 6 rows up").
- **Constraints:** `hazard_near_target` (a known hazard within 1 tile) and `target_out_of_view`.
- **Goal:** the chosen candidate becomes a `schemas.Goal` with:
  - `goal_id` `g1`, `g2`, …, and `target_ref` the candidate id;
  - `deadline_frame = frame + planning.goal_timeout_frames`;
  - the final target tile, kept in a `target:col,row` constraint, so `next_waypoint` can move along a route on graph arms.

## Lifecycle

absent → planned (a validated choice or fallback) → active (`WorkingMemory.set_goal`, `goal_set` event) → one of:
- **achieved** (`goal_achieved`);
- **failed** (`goal_failed`), for one of these reasons:
  - `target_invalid`: the target tile is in view and the item is gone;
  - `level_changed`;
- **expired** (`goal_failed` with `status=expired`, because the schema has no separate expiry event).

Goals are checked after every skill, never per frame. Event payloads carry `goal_id`, `target_ref`, `status` and `reason`. Every decision records the active `goal_id`.

## Shared planning triggers

| Trigger | Kind | Source |
| --- | --- | --- |
| `no_goal` | hard | episode start, a level change, or no active goal |
| `goal_achieved`, `goal_failed`, `goal_expired` | hard | lifecycle |
| `death` | soft, latched | `death` event |
| `stuck` | soft | `Progress.stuck`: no progress for `planning.no_progress_frames` (a new goal restarts the clock) |
| `repeated_failures` | soft | `planning.repeated_skill_failures` consecutive failed or interrupted skills **since the last plan** |
| `inventory_changed` | soft, latched | the set of held items changed (not fuel draining or score) |
| `route_invalidated` | soft, latched | graph arms: a route that had been found became unreachable |

- **Hard triggers** plan immediately, within the call cap.
- **Soft triggers** wait until `planning.min_frames_between_calls` (60) frames after the previous call. Latched triggers survive the wait.
- **No planning while Dave is `burning` or `dead`.** Input is ignored then. Triggers stay latched until the respawn.
- **Stable execution makes no calls.** A plan clears the latched triggers, resets the failure count and restarts the no-progress clock (tested).

### Calls, retries and fallback

- **Validation:** `parse_plan` requires `{"goal": <offered id>, "rationale": str}` and nothing else.
- **Rejected output:** a malformed or unknown answer is marked `invalid_output` and retried with the validation error as feedback, at most `models.max_retries` (1) times. Each attempt counts toward `planning.max_calls_per_episode` (30). A failed transport call is marked `error` or `timeout`. Every rejected or failed attempt emits a `model_failure` event with `purpose=planner`.
- **No valid answer, or the cap is reached:**
  - on a soft trigger, the current goal is kept;
  - otherwise a **deterministic fallback** picks the first candidate by the fixed priority, skipping a goal that just failed. It is logged with `fallback=true` and `fallback_reason` (`planner_failed` or `call_cap`).
- **Same rule offline:** the offline `RuleMockPlanner` uses the same priority, so mock runs are reproducible.

## Planner input (`PlanningRequest`)

- **Triggers.**
- **Player:** state, grounded, facing, lives, inventory and score.
- **View columns.**
- **Nearby:** visible hazards within 3 tiles and visible monsters, at most 10.
- **Recent skills:** the last `planning.recent_events` (8), with each skill's outcome, reason, events and end tile.
- **Goals:** how the previous goal ended, and the current goal (on soft triggers).
- **Candidates.**
- **Graph arms only:** each candidate's `route` summary.

Arms A and B send identical requests (tested on the fixture and on the real game).

## Azure planner (`--planner live`)

- **Endpoint:** Chat Completions at `{AZURE_OPENAI_ENDPOINT}/openai/deployments/{AZURE_OPENAI_CHAT_DEPLOYMENT}/chat/completions?api-version={AZURE_OPENAI_API_VERSION}`, with an `api-key` header. Settings are checked before the game starts.
- **Prompt:** the system prompt states only the rules verified in `docs/feasibility.md` §3, the planner's role and the output format, and asks for a brief rationale, not step-by-step reasoning. The user message is the `PlanningRequest` as JSON.
- **Output:** `response_format` is a strict `json_schema` whose `goal` field is an enum of the offered ids. `reasoning_effort: low`, `max_completion_tokens: 2000`.
- **Transport retries:** timeouts, 408, 429 and 5xx are retried with exponential backoff up to `models.max_retries`. 4xx responses are not retried, and their messages are redacted.
- **Logging:** usage tokens (prompt, completion, reasoning) are recorded. `cost_usd` stays null, because no price table is assumed. The `request_ref` is a hash of the body, and the `response_ref` is the response id.
- **Verified 2026-10-03:** `gpt-5.4-mini`, api-version `2024-12-01-preview`. The strict schema and `reasoning_effort` are accepted, and a call takes about 1.3–1.6 s with about 600–1000 prompt tokens. The sanitized response is `tests/fixtures/azure/planner_response.json` (`scripts/probe_azure.py`).

## Graph-enabled arms: Python computes the route

- **Route search:** when a goal is set, the target tile is mapped to its segment (`WorldGraph.node_at`; explore targets map to the cheapest reachable segment with an open side in that direction), and `find_route` runs from the segment Dave stands on.
  - **Found:** `next_waypoint` is the next route node's cell nearest the final target, and a `RouteTracker` follows it.
  - **Unreachable:** the waypoint is the target tile itself. The candidate's route summary gives the reason (`no_verified_route`, `requires:trophy`, `unknown_start`, `target_not_on_known_segment`) and up to 3 frontier segments, so the planner can choose exploration or recovery explicitly.
- **Following the route, with no LLM call:**
  - after each skill the tracker advances;
  - on `topology_changed`, `off_route`, `edge_failed` or `inventory_changed` (or while no route exists) the route is recomputed in Python, and the waypoint is updated without restarting the no-progress clock;
  - only a route lost after being found raises `route_invalidated`.
- **Timing:** route search is timed separately from model latency (`route_ms` and `model_ms` in the `goal_set` payload and the run summary).
- **Graph-disabled arms** get the same goal schema and candidates, with no `route` and the waypoint equal to the target tile.

## Storage

- Planner calls are stored as `model_calls` rows with `purpose=planner`. `EpisodeRecorder.record_planning` writes them together with the goal events, with no `decision_seq` because they are not tied to one skill.
- The `goal_set` payload records the triggers, candidate ids, choice, rationale, fallback, attempts, waypoint, route summary and timings.
- Replay is unaffected, since it re-executes the recorded skill choices.

## Known limitations

- `stuck` uses distance to the waypoint. A mock tactical controller ignores goals, so mock runs mostly show `stuck` and expiry triggers. Goal-directed play starts with the Phase 7 LLM controller.
- At fixture scale (41-frame episodes) the 60-frame debounce hides death triggers. Tests set it to 0. Calibrate all `planning:` values on Dave in Phase 9.
- A planner re-choosing the same target on a soft trigger restarts its deadline. The call cap bounds this.
- Explore targets are a direction and a column, not a verified reachable location.
