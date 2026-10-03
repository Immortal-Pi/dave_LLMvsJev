# Progress

Spec: `implementation/` (phases 0–11). Status as of **2026-10-03**.

| Phase | Status |
| --- | --- |
| 0: Feasibility and scope gate | **Done**, including the real game |
| 1: Skeleton, schemas, offline contracts | **Done** |
| 2: Real structured-state adapter | **Done**: deadly-dave bridge verified |
| 3: Action catalog and deterministic execution | **Done**: calibrated on the real game |
| 4: Working memory and event history | **Done** |
| 5: Learned world graph and persistence | **Done** |
| 6: Strategic planner and goal manager | **Done** offline. Live Azure planner verified |
| 7: LLM tactical baseline | **Done** offline. Live arm A smoke run on Dave level 1 (planner and tactical live) |
| 8: Jev tactical controller and hybrid loop | **Done** offline. Live A/B/C smoke run on Dave level 1 (planner and tactical live) |
| 9–11 | Not started |

## Phase 0: completed

- Selected [skoperst/deadly-dave](https://github.com/skoperst/deadly-dave) at commit `950d39d` (GPL-3.0). It is cloned to `external/deadly-dave/`, which is gitignored.
- Verified controls and mechanics (trophy-gated door, 4 lives, fire/water/vines and monsters cause burning and then death, gun, jetpack toggle, tree climbing, fall-wrap, level 5 secret) by reading the source. Details are in `docs/feasibility.md` §3.
- Verified the live Jev contract via OpenRouter `alpha/decisions`. The sanitized fixture is `tests/fixtures/jev/choice_response.json`.

## Phase 1: completed

- Converted the repo to a uv project: `pyproject.toml`, `uv.lock`, venv `jev/`, console script `dave-agent`.
- `src/dave_agent/` contains the schemas, the `GameAdapter` protocol, the fixture platformer, the config loader, candidate generation and the bounded executor, the mock controller, the Jev response parser, the episode runner, redacted logging and the CLI.

## Phase 2: completed

- Built with VS Build Tools 2022 (MSVC 14.44, Ninja). Upstream needed a Windows portability fix (`unistd.h`).
- `bridge/deadly-dave-bridge.patch` adds the headless `deadly-dave-bridge.exe`, which speaks JSON lines on stdin/stdout and runs one `game_level()` call per tick with held keys. `scripts/setup_dave.bat` clones the pinned commit, applies the patch and builds. The patch was verified to apply cleanly to a fresh pinned clone.
- `src/dave_agent/adapters/dave.py` (`DaveBridgeAdapter`):
  - decodes state and filters it to the 20-column viewport (`local_observed`);
  - derives player and entity velocity;
  - emits pickup, death, respawn and terminal events, with death cause `unknown`;
  - implements snapshots as input-log replay.
- `scripts/probe_environment.py` runs the acceptance checks and saves screenshots to `artifacts/probe/`.
- Schema changes: `Observation.player_velocity_source`, and terminal status `secret_exit`.
- Watch mode: `--watch [--watch-delay MS]` on `dave-agent probe`/`play` and `scripts/probe_environment.py` shows the game live. A watched episode matched the headless run exactly (600 frames, 296 decisions, 1 death).

## Phase 3: completed

Details are in `docs/skills.md`.

- `scripts/calibrate_skills.py` measured walking, jumping, the landing cooldown, air control, the gun and burning on the real game. Every measurement repeats exactly from a snapshot, and 12 checks compare them against the catalog.
  - **Not verified:** climbing (Up on a tree starts a jump) and the jetpack. Both stay out of the catalog.
- `configs/skills.yaml` now holds one catalog per adapter (`skills.catalogs.fixture` and `.dave`) plus executor settings. A skill is a list of phased button holds, either a fixed number of `ticks` or `until` a predicate holding (with `max_ticks`). It also lists its preconditions and its `interrupt_on` rules.
- The Dave catalog: `move_{left,right}_{1,3}`, `jump_up`, `jump_{left,right}`, `jump_{left,right}_short`, `shoot`, `wait_short`, `wait_long`. The fixture catalog is unchanged.
- `control/predicates.py` holds the named `Observation` predicates. An unavailable field masks the skill.
- `control/skills.py`:
  - `CandidateSet`: legal candidates, masked skills with reasons, and a digest;
  - `revalidate`, which runs before any input;
  - a frame-by-frame phase executor with interrupts (death, terminal, hazard_contact, new_hazard_nearby), phase timeouts (`failed`) and a hard cap;
  - `skill_started` and `skill_finished` events;
  - `stale_fallback`, the documented wait for real-time mode.
- Runner:
  - records a candidate set and an execution for every decision;
  - forces single-candidate decisions with no model call (`Decision.forced`);
  - the CLI summary adds the skill outcomes, interruptions and a `candidate_trace` hash.
- Schema: `Observation.player_state` and `facing` (from `dave->state` and `face_direction`; the fixture derives them), and `SkillCandidate.description`.
- Bug fix: deadly-dave's debug `printf`s (`monster.c` "ROUTE RESET …" after 251 ticks on monster levels) corrupted the JSON stream. The adapter now skips non-JSON lines (`game_stdout`).

## Phase 4: completed

Details are in `docs/memory.md`.

- **`memory/working.py` `WorkingMemory`:**
  - holds the latest observation, goal/waypoint, last decision and last skill status;
  - keeps a recent-history deque (one entry per skill) bounded by `memory.recent_history_frames` (120) and `memory.max_history_entries` (64);
  - derives motion since the window start or the last respawn;
  - tracks progress and stuck/repetition counters: `no_progress_frames`, `stuck`, `consecutive_failures`, `repeated_failures`, `skill_repeats`, `tile_revisits`;
  - is cleared by `reset()` at each episode start.
- **`MemoryContext`** (the last 8 entries plus counters) is now the 4th argument of `TacticalController.decide`. It is deterministic and bounded, and identical for every arm.
- **`memory/detector.py`** derives `inventory_changed` and `area_discovered` events. The event types `goal_set`, `goal_achieved` and `goal_failed` were added for Phase 6.
- **`memory/episodes.py` `EpisodeStore`:**
  - SQLite tables `runs`, `episodes`, `observations`, `model_calls`, `decisions`, `skill_executions` and `events`, with composite foreign keys;
  - the schema version is kept in `PRAGMA user_version`, and unknown versions or files are refused;
  - batched, write-only `EpisodeRecorder` (`memory.store_batch_size`);
  - termination reasons (`terminal:*`, `max_frames:N`, `error:*`), with partial evidence kept on error;
  - `model_failure` events;
  - JSONL export.
- **`runner/replay.py`** re-executes an exported episode and reports mismatches in the candidates, outcome and reason, or the end observation.
- **CLI:**
  - `play` logs every episode (`--store`, `--run-id`), and its summary adds `run_id`, `episode_key` and `termination_reason`;
  - new `export` and `replay` commands.
- **Death causes:** Dave deaths are stored as `{"cause": "unknown"}`. Nothing infers a cause.

## Phase 5: completed

Details are in `docs/graph.md`. Adds the `networkx` dependency (3.7).

- **`memory/graph.py` `WorldGraph`**, a `MultiDiGraph`:
  - **Nodes** are platform segments from a deterministic segmentation of observed tiles. A standable cell is not solid or hazard and sits above solid. Nodes have open sides at the view edge, merge as the view scrolls (the older id is kept and the merged one aliased), and carry the items as last seen, visited state and evidence.
  - **Edges** come only from completed skills that went from one segment to another, keyed `skill|inventory_context`. They store raw attempts, successes, failures, fatal outcomes and frames, and success uses `(s+1)/(n+2)`.
  - **Failures** attach to an edge only when the target is identifiable; otherwise they stay on the node.
  - Other outcomes go to counters: `stays`, `inconclusive` and `unanchored`.
  - Model suggestions are kept apart from topology.
- **`memory/routes.py`:**
  - deterministic Dijkstra over the normalized cost, after filtering out edges that need items not held;
  - the trophy-gated exit gives `requires:trophy`;
  - an unreachable result comes with an exploration frontier;
  - `RouteTracker` gives replan triggers.
- **`memory/persistence.py`:**
  - versioned JSON with lineage and parent sha256;
  - atomic save (temp, fsync, `.bak`, `os.replace`);
  - rejects a schema, adapter, build or observation-policy mismatch;
  - YAML export.
- **Wiring:**
  - `run_episode(graph=...)` learns from every step for graph-enabled arms only, and nothing reads the graph during the episode yet;
  - `play --arm C` uses a per-arm checkpoint `artifacts/graphs/arm-C/<adapter>.json` (or `--graph`);
  - new `dave-agent graph` command;
  - config section `graph:` (route-cost weights and limits, not calibrated).

## Phase 6: completed

Details are in `docs/planner.md`.

- **`control/goals.py` `GoalManager`**, the same code and `planning:` settings for every arm:
  - **Candidate goals** come from `TargetMemory`, the per-episode record of observed targets. They are: `reach` the door (offered only while the trophy is held), `collect` the trophy, items or the nearest 3 loot, `explore` right or left, and `recover:safe`. The planner picks an id, and an unknown id is rejected.
  - **Lifecycle:** absent → planned → active → achieved, failed (`target_invalid`, `level_changed`) or expired (`goal_timeout_frames`). It emits `goal_set`, `goal_achieved` and `goal_failed` events.
  - **Triggers:**
    - hard: `no_goal` and goal ended;
    - soft and debounced (`min_frames_between_calls`): `death`, `stuck`, `repeated_failures` (counted since the last plan), `inventory_changed` and `route_invalidated`;
    - no planning while burning or dead;
    - capped by `max_calls_per_episode`.
  - **Bad output:** a malformed or unknown answer gets one retry with feedback (`models.max_retries`), then a deterministic fallback in priority order. Every failed attempt emits a `model_failure` event.
  - **Graph arms:** Python route search adds route summaries (status, cost, skills or frontier) to the candidates and turns the goal into the next route waypoint. Rerouting costs no planner call. `route_ms` is logged separately from `model_ms`.
- **`models/planner.py`:** the `StrategicPlanner` protocol, `PlanningRequest`, `GoalCandidate`, `parse_plan`, `RuleMockPlanner` (the offline default) and `ScriptedPlanner` (tests).
- **`models/azure.py`:**
  - `AzureChatClient`: httpx Chat Completions with transport retries and redacted errors;
  - `AzurePlanner`: the prompt states only the verified rules, the output is a strict JSON schema with the goal as an enum of candidate ids, and `reasoning_effort: low`;
  - usage tokens are recorded, and `cost_usd` stays null because no price table is assumed.
- **Wiring:**
  - `run_episode(goals=...)` keeps planning records in `EpisodeResult.planning`;
  - `EpisodeRecorder.record_planning` stores planner calls (`purpose=planner`) and goal events;
  - `WorkingMemory.set_goal(goal, restart_clock=...)`.
- **CLI:**
  - `play --planner mock|live`; live is labeled `mode=live-planner` and checks the Azure settings before the game starts;
  - the summary adds `planner_calls`, `goals`, `planning_triggers`, `fallback_goals`, `goal_trace` and `route_ms`;
  - new `probe-provider --provider azure`.
- **Config:** `planning.min_frames_between_calls` (60), `goal_timeout_frames` (480), `recent_events` (8) and `nearest_collectibles` (3). These are not calibrated.
- **New:** `scripts/probe_azure.py` and the fixture `tests/fixtures/azure/planner_response.json`.

## Phase 7: completed

Details are in `docs/tactical.md`.

- **`models/tactical.py`:**
  - `tactical_request`: the single request every tactical model sees (player, lives, inventory and score, the local view as a character grid, visible entities, the goal with waypoint and offset, progress, recent skills, candidates);
  - `parse_tactical`: exactly `{"candidate_id": <offered id>}`;
  - `ModelController`, the shared policy for every model-backed arm:
    - one re-ask (`models.max_retries`), with feedback after invalid output;
    - then the deterministic legal fallback (`tactical.fallback_skills`, else the first candidate), logged with `Decision.fallback`, `fallback_reason` and a `decision_fallback` event;
    - per-episode call, token and cost budgets, checked before every call (`BudgetExhausted` with `on_budget_exhausted: terminate`, or fallback decisions);
  - `SeededMockModel` (the offline default, the same choices as `SeededMockController`) and `ScriptedTacticalModel` (tests).
- **`models/azure.py`:**
  - `AzureTacticalModel`: strict schema with an enum of the offered ids, settings from `models.tactical_llm`;
  - the verified rules text is shared with the planner (`GAME_RULES`); the planner's settings now come from `models.planner`.
- **Runner:**
  - `decide` returns every call made for the decision;
  - `model_failure` per non-ok call;
  - the wall-time budget (`benchmark.max_episode_wall_seconds`);
  - budget stops end the episode as `truncated` with `budget:<name>`.
- **Store:** `EpisodeRecorder.record` stores all calls for a decision, and `model_call_seq` points to the last one. Each run stores `effective_settings` in `runs.config_json`. No schema version change.
- **Schema:** `Decision.fallback_reason`; event type `decision_fallback`.
- **CLI:**
  - `play --tactical mock|live`; `--mock` is now optional (the default) and conflicts only with `--tactical live`;
  - live runs print their budget to stderr before any call; modes `live-tactical` and `live`;
  - the summary adds `model_decisions`, `fallback_decisions`, `fallback_reasons`, `tactical_calls`, `tactical_failures`, `tactical_latency_ms`, `tokens`, `cost_usd` and `settings`;
  - `probe-provider --purpose tactical`.
- **Config:** a `tactical:` section (`max_calls_per_episode` 400, `max_tokens_per_episode` 1,000,000, `max_cost_usd_per_episode` null, `on_budget_exhausted: terminate`, `fallback_skills [wait_short, wait]`). `models.planner` and `models.tactical_llm` gained `max_completion_tokens` (2000) and `reasoning_effort` (low).

## Phase 8: completed

Details are in `docs/tactical.md` (Jev section).

- **`models/jev.py`:** `JevSettings` (key from `models.jev.api_key_env`, checked before the game starts), `JevClient` (OpenRouter `alpha/decisions`; 529 retried too) and `JevTacticalModel`:
  - `state` = arm A's user-message JSON plus the same rules and input guide that arm A gets in its system prompt;
  - one `choice` question whose criteria are the offered candidate ids with their descriptions;
  - `provider_score` = the chosen candidate's probability, with its meaning recorded;
  - dated model, token usage and reported cost preserved; absent fields stay None.
- **`models/http.py`:** the transport retry loop, now shared by Azure and Jev (the Azure behaviour is unchanged).
- **`models/tactical.py`:** the provider-neutral text (`GAME_RULES`, `INPUT_GUIDE`, `TACTICAL_TASK`) moved here; the Azure prompts are byte-identical. `context_digest` is set on every model decision.
- **Schema and store:**
  - `Decision.context_digest` and `ModelCallRecord.output` (provider answer details);
  - store schema **v2** (`decisions.context_digest`, `model_calls.output_json`). A v1 store (such as an existing `artifacts/events.sqlite`) is refused with the "export to JSONL and start a new store file" error.
- **Hybrid loop (`control/goals.py`):** a `death` event now ends the active goal (`goal_failed`, reason `death`). The tactical model never chases the old waypoint while Dave burns, and the hard trigger replans at the respawn even inside the debounce window. Planning stays event-driven; the `play` summary adds `decisions_per_planning`.
- **CLI:**
  - `play --tactical live` for arms B and C (Jev); arm A keeps Azure;
  - `probe-provider --provider jev [--save-fixture]`.
- **Tests:**
  - `test_jev_tactical.py` (11): payload parity with arm A, the sanitized live fixtures, None for absent fields, invalid choice → retry → fallback, 429/503/529 retries, timeout and 401 fallbacks without key leaks, missing key;
  - an A/B/C `context_digest` parity test;
  - the death/respawn goal test;
  - the Jev CLI refusals.

## Verification (run 2026-10-03)

```bash
export UV_PROJECT_ENVIRONMENT=jev   # PowerShell: $env:UV_PROJECT_ENVIRONMENT="jev"
scripts\setup_dave.bat              # clone + patch + build (cmd/PowerShell)
uv run pytest                       # 217 passed, 1 skipped (live; RUN_LIVE=1). 24 drive the real game (-m dave)
uv run python scripts/calibrate_skills.py
#   walk 2 px / 3 ticks (16 px at 24, 48 px at 72); jump apex 32 px, lands after 94 ticks
#   holding Up re-jumps after 5 ticks; landing cooldown 5 ticks; long jump dx 94, short dx 32
#   gun reachable on level 3, bullet +8 px, one at a time; Up on a tree -> jumping; burn lasts 200 ticks
#   12/12 checks ok, all deterministic
uv run python scripts/probe_environment.py
#   movement (32,144) -> (52,144) after 30 ticks right        ok
#   jump: y 144 -> 124 after 20 ticks, airborne               ok
#   gem pickup: loot at tile (1,7), frame 31, score 100       ok
#   snapshot at frame 52 restores an identical 25-tick future ok
#   level 2 fire floor: death -> respawn at (16,144), lives 4 -> 3, cause unknown   ok
#   local_observed region cols 0-19                           ok
uv run dave-agent probe --adapter dave --scenario level1
#   (32,144) frame 11 -> one tick right -> (34,144) frame 12
uv run dave-agent play --arm A --mock --adapter dave --scenario level2   # and --arm B
#   both: truncated at 615 frames, 14 decisions (9 forced while burning, 5 model calls), 1 death,
#   interruptions {hazard_contact: 2, death: 1}, candidate_trace 2a57449186e2a6b3 (identical for A and B)
uv run dave-agent play --arm A --mock
#   fixture: game_over, 41 frames, 17 decisions, 3 deaths (unchanged from Phase 1)
uv run dave-agent play --arm A --mock --adapter dave --scenario level2 --store S.sqlite --run-id dave-A   # and B
uv run dave-agent export --store S.sqlite --out S.jsonl     # 168 records (2 episodes)
uv run dave-agent replay --jsonl S.jsonl                    # both episodes: 14 decisions, ok
#   stored: termination_reason max_frames:600; death payload {"cause": "unknown"}; area_discovered cols 0-19
uv run dave-agent play --arm C --mock --adapter dave --scenario level1 --graph G.json --seed 1   # then --seed 2
#   15 segment nodes (3 visited); edges 2 -> 3 after the second run, all observed successes; lineage 2, parent sha set
uv run dave-agent graph --checkpoint G.json --route level1:r9:c2 level1:r9:c12
#   unreachable, reason requires:trophy (door segment), frontier listed
#   arms A/B on level2: unchanged (candidate_trace 2a57449186e2a6b3), graph null, no checkpoint written
uv run dave-agent play --arm A --mock --adapter dave --scenario level1      # and --arm B
#   both: 3 planner calls (no_goal, stuck x2), goal_trace collect:trophy:c11:r3 x3, candidate_trace 97b1dc188deb88ba
uv run dave-agent play --arm A --mock --adapter dave --scenario level2      # and --arm B
#   both: 2 planner calls (no_goal; then death+stuck after respawn), candidate_trace still 2a57449186e2a6b3
uv run dave-agent play --arm C --mock --adapter dave --scenario level1 --graph G.json --seed 1   # then --seed 2
#   goal routes: collect:trophy unreachable (no_verified_route) with frontier [level1:r0:c0, level1:r3:c11, ...];
#   one goal expired at 480 frames and was replanned; route_ms 0.7-0.8 per episode, logged apart from model time
uv run dave-agent probe-provider --provider azure                  # LIVE (paid)
#   status ok, gpt-5.4-mini, 1434 ms, 589 prompt + 69 completion tokens (26 reasoning), valid choice collect:gem:c2:r2
uv run dave-agent play --arm A --mock --planner live --adapter dave --scenario level1   # LIVE planner, mock tactical
#   mode live-planner, 2 planner calls, both ok (1264 ms / 1553 ms; 920 / 1028 total tokens), 0 failures
#   goals: collect:trophy:c11:r3 ("required to finish the level"), then on stuck explore:right
uv run dave-agent play --arm A                                       # Phase 7: fixture, via ModelController
#   unchanged: game_over, 41 frames, 17 decisions (17 model decisions, 0 fallbacks, 17 tactical calls)
uv run dave-agent play --arm A --adapter dave --scenario level2
#   unchanged: candidate_trace 2a57449186e2a6b3, 14 decisions (9 forced, 5 model)
uv run dave-agent probe-provider --provider azure --purpose tactical          # LIVE (paid)
#   status ok, gpt-5.4-mini, 1420 ms, 765 prompt + 83 completion tokens (61 reasoning), valid choice c1_move_right
uv run dave-agent play --arm A --planner live --tactical live --adapter dave --scenario level1 --run-id p7-live-smoke-1   # LIVE
#   mode live; budget printed first; truncated max_frames:600 (690 frames); 9 decisions, all model decisions,
#   0 fallbacks, 0 tactical failures; tactical latency mean 2832 ms (p50 2741, max 4203);
#   tokens: tactical 14542 (10472 prompt, 4070 completion, 3844 reasoning), planner 1919 (2 calls, both ok)
#   play: move_right_3 x2, jump_right_short x2, then jump_up / step left-right under the trophy (c11 r3); score 0, no deaths
uv run dave-agent export --run-id p7-live-smoke-1 --out L.jsonl && uv run dave-agent replay --jsonl L.jsonl
#   64 records; replay ok (9 decisions); only env var *names* appear in the export, no credentials
uv run dave-agent play --arm A|B|C                                # Phase 8: fixture, mock
#   unchanged: 41 frames, 17 decisions, candidate_trace 38b901ad24333f4f for all three arms
uv run dave-agent play --arm A --adapter dave --scenario level2
#   unchanged: candidate_trace 2a57449186e2a6b3, 14 decisions
uv run dave-agent probe-provider --provider jev --save-fixture             # LIVE (paid)
#   ok, typesafe/jev-1.13-20260917, 343 ms, 1245 in + 78 out tokens, $5.23e-05;
#   choice c1_move_right p 0.73 (c4_jump_right 0.15), confidence 0.68; fixture saved, no credentials
uv run dave-agent play --arm B --planner live --tactical live --adapter dave --scenario level1 \
    --store artifacts/p8-live.sqlite --run-id p8-live-B                    # LIVE; then C, then A
#   B (Jev):   12 decisions, all model, 0 fallbacks; tactical latency mean 236 ms (p50 163, max 720);
#              26.1k tactical tokens; $0.00102; 3 planner calls (no_goal, stuck x2); 622 frames, truncated
#   C (Jev+graph): 9 decisions, all model, 0 fallbacks; mean 177 ms (p50 159, max 306); 19.3k tokens;
#              $0.00076; 3 planner calls; 682 frames, truncated; graph checkpoint written (15 nodes)
#   A (Azure): 10 decisions, all model, 0 fallbacks; mean 2535 ms (p50 2648, max 3777); 15.7k tokens;
#              2 planner calls; 635 frames, truncated
#   all: score 0, no deaths, every skill completed
#   first-decision context_digest: A = B = sha256:7b21dadb6e2ea12d; C differs (its live planner chose explore:right)
uv run dave-agent export --store artifacts/p8-live.sqlite --run-id p8-live-X --out artifacts/p8-live-X.jsonl
uv run dave-agent replay --jsonl artifacts/p8-live-X.jsonl                 # A, B, C: no mismatches
#   no API key, "Bearer", "sk-or-" or "api-key" in any export
```

Screenshot cross-check: after the pickup, Dave is drawn at about (15,112) against the recorded (14,112), the gem at tile (1,7) is gone, and the HUD shows score 100. Map rows line up at 16 px per tile.

Live evidence covers **arms A, B and C** (labeled `mode=live`): one smoke episode each on Dave level 1, seed 0. This shows the live integration works end to end; it is **not** a performance comparison. That is Phase 9 (benchmark protocol, calibrated episode length, repeated trials).

## Decisions

- **Skills release Up as soon as a jump starts.** Holding it re-jumps on landing (measured).
- **`hazard_contact` interrupt:** burning means death is certain and input is ignored, so a skill stops at ignition instead of running about 200 more ticks. While Dave burns, only `wait_long` is legal, and it is forced with no model call.
- **`new_hazard_nearby`** (48 px) is the only state-reactive interrupt. It just stops the skill and never chooses an action, so there is no reflex, and it applies to all arms.
- **Working memory is per-skill, not per-frame.** One history entry per executed skill, while progress is checked on every frame. Controllers get only `MemoryContext`, never a transcript.
- **The episode store is write-only from the loop.** It is written at decision boundaries in batches and read only by export, replay and tests. It stores only decision-point observations (each skill's start and end), not every frame.
- **The graph is write-only in Phase 5.** Graph arms learn, but controllers get identical inputs (tested) until the Phase 6 planner reads routes.
- **Completed skills that end airborne are `inconclusive`,** not failures. No transition was observed, and nothing went wrong.
- **The planner chooses, Python validates:** goals are candidate ids built from observed targets, not free-form targets. The door is offered only with the trophy (a verified rule).
- **Planner retries are bounded and shared:** every planner (mock, scripted, Azure) returns raw text through the same parse, retry and fallback path, so arms A and B cannot differ in recovery behaviour.
- **Fallback priority** (reach door > trophy > item > loot > explore right > explore left > recover) is also the offline rule planner's policy, so mock runs are reproducible.
- **Rerouting on graph arms is Python only.** Only a route lost after being found asks the planner (`route_invalidated`).
- **Expiry is a `goal_failed` event with `status=expired`;** the schema has no separate event type.
- **One tactical policy for every arm:** `ModelController` owns retry, fallback and budgets; providers only produce raw output and parse it. Offline, the CLI runs the seeded mock through the same wrapper.
- **Transport failures are re-asked too** (after the client's own transport retries), like the planner. The fallback is `wait_short` / `wait`: safe and deterministic, but it makes no progress.
- **Budget exhaustion terminates by default** (`truncated`, `budget:<name>`), so a paid run never silently continues as a fallback-only episode.
- **Item-superset assumption:** an edge observed with items S is usable whenever S is held, even alongside other items. This is not verified for jetpack mode.
- **Episode keys are `run_id/episode_id`,** because adapter episode ids (`fixture_l1-s0-e1`) repeat across runs.
- **Truncation is checked between skills,** so an episode can overrun `max_episode_frames` by at most one skill (≤ 238 frames).

- **Process boundary for the GPL game.** The harness never links deadly-dave code. Our changes live in this repo only as `bridge/deadly-dave-bridge.patch`.
- **Snapshots = input replay**, not a C deep copy. This is exact because the game is deterministic (tested), and a replay that ends on a different tick raises an error. Cost grows with episode length.
- **Auto-release after reset or respawn:** the bridge presses a neutral `space` key once the blink timer allows. The ticks are counted (`auto_ticks`). This is identical for every arm.
- **Death cause `unknown`:** the bridge does not yet expose what set `on_fire`.
- **Fixture jump rule:** a jump triggers on a fresh press only. This applies to the fixture only.
- **Same seeded mock for both arms** in offline mode, so A and B trajectories are identical by construction.
- **Jev via OpenRouter**, and **Azure deployment from `AZURE_OPENAI_CHAT_DEPLOYMENT`** (verified live in Phases 6 and 7).
- **Jev gets arm A's exact content:** the same request JSON as `state`, plus the same rules, input guide and task text that arm A gets in its system prompt. Only the transport differs: a choice question with criteria for Jev, a strict JSON-schema enum for Azure.
- **A death ends the goal.** The respawn puts Dave back at the level start, so a goal's route and waypoint are stale; ending it makes the replan a hard trigger.
- **`provider_score` is Jev's probability for the chosen candidate.** `confidence` is recorded but not used to decide anything until it is calibrated (Phase 11).
- **Sideways jumps press the direction only after takeoff** (2026-10-03, after Phase 8). Diagnosing a live Jev level-1 run showed jumps right after a landing walking Dave off 1-tile pillars during the hidden 5-tick cooldown. Flat-ground measurements are unchanged (calibration 12/12), and the offline traces are unchanged (`97b1dc188deb88ba` level 1, `2a57449186e2a6b3` level 2, fixture 41/17).
- **`moved_px` in recent history** (observed displacement), the same for every arm. In the live Jev run, 5 of 12 skills were walks into a wall that reported `completed` with no visible "nothing happened" signal.
- **Level 1 is completable with the catalog alone** (`scripts/try_skills.py`, 10 skills, 554 frames; see `docs/skills.md`). Failures to finish are decision and information problems, not missing skills.
- **Reachability waypoints and estimated end tiles** (every arm, Dave only). With the waypoint alone, Jev still jumped into the ceiling at (7,9) 310 times. With the estimated end tile on each candidate, live Jev finished level 1: 11 decisions, 470 frames, $0.001 (run `diag-jev-3`). Without either, it never left the floor in 400 decisions (`diag-jev-1`). The models now mostly choose among estimated outcomes; Phase 9 should report this as the setup, and an ablation without estimates is worth keeping.
- **No cost budget for now:** it could only ever fire for Jev, which would make the arms asymmetric. Jev costs about $0.0001 per decision.

## Open issues

- Asset licensing: deadly-dave's `res/` art and levels come from the original game. Use them locally only; do not commit them here.
- Jetpack (`P`) is wired but not exercised. Fire is verified (Phase 3).
- Climbing needs an input sequence that has not been measured yet. Trees are observed as `climbable` tiles, but no skill uses them.
- The landing cooldown (5 ticks) is hidden state. A jump requested right after landing spends up to 5 ticks in its first phase.
- The working-memory window (120 frames) is fixture-scale and holds only a few Dave skills. Calibrate it, and every `planning:` value, on Dave in Phase 9.
- Mock tactical controllers ignore goals, so mock runs mostly show `stuck` and expiry triggers.
- `explore` targets are a direction and a column, not a verified reachable location.
- Azure cost is not computed (no price table); token usage is recorded per call, so `max_cost_usd_per_episode` cannot fire for Azure.
- Live Azure tactical calls average about 2.5–2.8 s, mostly reasoning tokens (about 380–430 per call at `reasoning_effort: low`). Lower settings were not tested on this deployment. Jev calls average about 0.2 s, but the game is paused during decisions, so latency does not affect play yet (Phase 11).
- Jev's chosen-candidate probabilities on Dave were mostly 0.24–0.52, spread across walking and jumping skills.
- The planner (live and rule-based) always prioritises the trophy, so Dave no longer wanders into coins as the random mock did. This is intended: coins only add score.
- The route-relevant facts the models lack are which jump lands where (narrow pillars), and the hidden landing cooldown, which `wait_short` covers but which is not observable.
- In the live C run, the graph learned 15 nodes but no edges (one visited segment), so routes were not yet informative. That needs longer episodes or a warm checkpoint (Phase 9).
- The store schema changed to v2. Export old stores to JSONL before deleting them.
- `max_episode_frames: 600` allows only about 9 Dave skills. Calibrate it in Phase 9.
- The graph's segment rule marks a "platform" above the top brick row (level 1, row 0). Dave reaches it only by wrapping, so it mostly shows up as frontier.
- Edge keys ignore the start position within a segment, so a failure whose target is ambiguous stays on the node.
- `EpisodeResult` still keeps every per-frame `StepResult` in memory. That is fine at the current episode lengths, but revisit it before 18000-frame benchmark episodes.

## Next steps (Phase 9)

From `implementation/11-phase-09-benchmark-protocol-and-reproducible-runner.md`:

1. A manifest-driven benchmark CLI that records the game and adapter version, scenario hashes, seeds, observation policy, model ids and settings, prompt and config hashes, code revision, graph checkpoint hashes, mode, budgets and fallback behaviour.
2. Paired initial scenarios with randomized arm order. Cold and warm (frozen graph checkpoint) memory regimes are reported separately, with no checkpoint shared across arms.
3. 3–5 smoke trials, then about 30 paired pilot trials, under a monetary ceiling (`paid_run_budget_usd`). Before that, calibrate `max_episode_frames` (600 frames is only about 10 Dave skills), the working-memory window and the `planning:` values on Dave.
4. Primary metrics (completion rate and paired difference, deaths, frames to completion, wall time, decision latency p50/p95, cost per attempt) and the secondary metrics, computed from the episode store, with mock and live results kept apart.
