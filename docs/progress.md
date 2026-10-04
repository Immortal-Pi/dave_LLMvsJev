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
| 9: Benchmark protocol and reproducible runner | **Done** offline (mock acceptance gate on the fixture, mock benchmarks on Dave). No paid benchmark run yet |
| 10–11 | Not started |

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

## Phase 9: completed (offline)

Details are in `docs/benchmark.md`.

- **`runner/session.py`:** the episode setup moved out of `cli.play` (`build_models`, `run_trial`, `episode_summary`). `play` output is unchanged.
- **`runner/benchmark.py` `BenchmarkRunner`:**
  - **Trials:** paired trials (every arm on the same scenario with seed `benchmark.seed + k`) and a seeded arm-order shuffle per (scenario, trial).
  - **Isolation:** a fresh adapter, models and memory per episode, and one store run per episode.
  - **Memory regimes:** cold (an empty graph per trial, learning within the episode only, saved per arm and trial for inspection) and warm (a per-arm frozen checkpoint whose sha256 is checked after the run). Cross-arm checkpoints are refused unless `--shared-checkpoint`, and same-level learning is labeled.
  - **Checks before any spend:** arms, regime, prices, credentials, output directory, and one reset per (scenario, seed), which gives the scenario hashes.
  - **Manifest:** written before the first episode and updated after each one.
  - **Records:** `episodes.jsonl` is appended per episode. Errors become `outcome: error` records and the run goes on. Ctrl-C leaves an `interrupted` manifest listing what did not run.
  - **Ceiling:** the paid-run ceiling (`benchmark.paid_run_budget_usd`) stops scheduling, with the reason `budget_stopped`.
  - **Also here:** `train_memory` (builds warm checkpoints) and `resummarize`.
- **`evaluation/`:**
  - `metrics.episode_record`: the primary metrics and the secondary metrics, including the fixed repeated-failure key `(start tile, skill, failure type)`;
  - `statistics` (stdlib): percentiles, Wilson, seeded bootstrap, exact McNemar;
  - `summary.summarize`: groups that never pool regime, mode or environment; paired arm comparisons matched on (trial, seed).
- **Cost:**
  - **Azure estimates.** `models.planner.price` and `models.tactical_llm.price` (null by default) give Azure calls an *estimated* `cost_usd`, with the price source and date recorded.
  - **Unknown cost** stays null and is counted, never zero.
  - **Unpriced live arms** are refused unless `--allow-unpriced` is passed.
- **Runner:** `EpisodeResult` gains `wall_seconds` (monotonic), `decision_latency_ms` (all calls for one decision, re-asks included), `decision_calls` and `execution_starts`. A run that raises carries its partial result on the exception (`episode_result`).
- **CLI:** `benchmark`, `summarize` and `train-memory`.
- **Config:**
  - `benchmark.seed`, `confidence` and `bootstrap_samples`;
  - `models.*.price`;
  - new `configs/benchmark_dave.yaml`, with Dave-scale frame settings taken from the measured skill durations (`scripts/skill_frames.py`).
- **Tests:**
  - `test_statistics.py` (4);
  - `test_benchmark_metrics.py` (8): cost aggregation, unknown cost, latency split, failure inclusion, the repeated-failure key, pairing, no pooling, the Azure estimate;
  - `tests/integration/test_benchmark_mock.py` (10): reproducibility, schedule, per-arm and per-trial graphs, the warm regime and its refusals, the ceiling, errors, interrupt, unpriced refusal, the reach-hints switch, the CLI.

## After Phase 9: learning from deaths (2026-10-03)

The problem: in live level-2 runs, Dave died and then repeated the same moves. Jev answers the same request the same way, and after a respawn nothing in the request said what had killed him. Arm C's graph recorded the deaths but only steered route waypoints, and the demos were arm B.

- **Experience notes on candidates** (`control/experience.py`, `docs/memory.md` "Experience notes"):
  - every arm: `this episode from here: 2x, 2 died (burned), last end [3,9]`, from working memory's new per-(tile, skill) experience table, which survives respawns;
  - graph arms: `past runs from this platform: 5x, 3 ok, 2 fatal, lands row 7 cols 4-9`, from `WorldGraph.skill_evidence` on the graph as it was at episode start (frozen warm checkpoints too, through `run_episode(evidence=…)`).
  - Notes only inform; nothing is masked.
- **Ctrl-C keeps the evidence.** `run_episode` finishes the episode as `truncated` / `interrupted` and writes the batched rows. `play` still saves a graph arm's checkpoint, prints `{"interrupted": true, …}` and exits 130. Before this change, `demo-B-L2` kept only its episode start.
- **Level 2 needs no new skills.** `scripts/search_route.py` (breadth-first search on the real game, snapshot restore, fatal skills pruned) found a 37-skill route in 566 s (505 states). It begins with a zig-zag climb (`jump_right move_right_1 jump_left move_left_1 jump_right …`), picks up the trophy on the 9th skill (score 100 -> 1100), backtracks, and ends at the door at (46,2). `try_skills.py` replays it to `level_complete` in 1928 frames. The route is in `artifacts/search/level2-tile.json`. On level 1 the search finds an 8-skill route.
- **Tests:** experience table (`test_working_memory.py`), notes and cap (`test_experience.py`, 2), `skill_evidence` (`test_graph.py`), past-run notes only on graph arms and read-only (`test_arm_parity.py`), interrupted `play` keeps its episode and graph (`test_cli_smoke.py`).

## Decision inspector and viewer (2026-10-03)

Details are in `docs/inspector.md`.

- **`dave-agent inspect` (`runner/inspect.py`):**
  - replays a recorded episode with its recorded choices (`ReplayPlanner` from the `goal_set` events, `ReplayController` from the decisions);
  - graph arms start from the checkpoint before the run (the `.bak` when the file already includes it);
  - every rebuilt tactical request must match the recorded `context_digest`, or the inspection stops;
  - each decision's bundle holds the exact request, the exact Jev and Azure bodies, the recorded answer, the planner request and choice, a screenshot, and what every offered candidate really does from that state;
  - `--ask jev|azure` (paid, needs `--decisions`) adds live answers to the same request.
- **`frontend/`:** a local, read-only Next.js 16 viewer:
  - a run list;
  - a run page (decision heat map, goal timeline, decisions with back-and-forth loops marked);
  - a decision page (the request's tile grid with Dave's pixel position, the waypoint, and the estimated versus real outcome of each option; the screenshot; options with Jev's probabilities; goal and planner; memory; exact payload tabs).
- **Tests:** `tests/integration/test_inspect.py` (7):
  - exact rebuild and bundle contents;
  - determinism and selection;
  - a corrupted digest stops it;
  - graph checkpoints from before the run, and refusal otherwise;
  - `--ask` refusals;
  - the CLI;
  - a Dave level-2 rebuild with screenshots.

**What the inspector showed on `demo-C-L2-1`** (arm C, Jev, level 2; 92 model decisions, all digests verified):
- **The reach estimates are off by one column.** 386 of 640 estimated end tiles differ from where the skill really ends. The estimator locates Dave one column left of `player.tile`. At (7,7) the waits say "estimated end tile [6, 7] (no movement)", so the request contradicts itself.
- **The fatal step was a hint error.** At decision 94 on the ledge (4,5), the estimates said `jump_right` lands on [4,3] (up and right, which Jev rated 0.27); it really lands on [6,7]. `move_right_1` was estimated at [4,7]; it really goes to [5,6], and the next walk ends in the fire.
- **The explore goal drove the back-and-forth walking.** The planner chose `explore:right` 6 times, with waypoint (20,7) or (20,9) beyond the pillar, so the waypoint offset said "go right" on a floor that ends at the pillar. 77 of 110 decisions were inside back-and-forth loops.
- **Level-1 nodes in level-2 routes.** Arm C's level-2 route summaries list level-1 frontier nodes (`level1:r0:c0`, …).

## Per-level graphs, threat screen and the planner's map (2026-10-03)

The problems:
- One learned graph served every level.
- Nothing projected plasma or Dave's own path forward.
- The planner never saw the layout, so it could not steer Dave around a wall or a fire pit, for example on level 2 by climbing the left ledges and crossing along the top.

**One graph per level** (`docs/graph.md`):
- `GraphStore` keeps one `WorldGraph` per level, stored as `artifacts/graphs/arm-C/<adapter>/<level>.json`.
- A legacy combined `dave.json` is split by level on load.
- A skill that changes level is never an edge, so level-1 nodes no longer appear in level-2 routes.
- Deaths leave `incidents` (cause, tile, skill) on their start platform, and planner route summaries list them.
- The inspector rebuilds each level from before the run.

**Threat prediction and the candidate screen** (`control/threats.py`, `docs/skills.md`):
- Every skill's path is simulated tick by tick and checked against the game's collision boxes for plasma (2 px/tick until a brick), monsters (linear) and hazard cells.
- Candidates with a predicted contact are removed unless all have one, and the rest carry `danger: …` / `no threat predicted`.
- A new interrupt, `threat_incoming`, stops walks and waits when a new threat is about to touch a standing Dave.

Checking the simulation against the real game found and fixed six physics gaps (dave.c):
- the ceiling test points;
- a jump slipping past a ledge corner when the held direction clears it;
- the side-test rows;
- air control off ledges;
- free-fall drift, which starts once a key turns Dave in the air;
- falls continuing after a skill ends.

Result on 300 random skills on levels 1–3:
- all 8 burns predicted, with 0 false alarms;
- 88% of end positions within 2 px.

Long mock runs (arm C, `configs/watch.yaml`, 18,000 frames):

| Level | Before | After |
| --- | --- | --- |
| 2 | game over at frame 5,194 (4 deaths) | 0 deaths |
| 3 | 2 deaths | 0 deaths |

The inspector verified 392 request digests on a replay.

**The planner sees the explored map and can give waypoints** (`docs/planner.md`, "Map and waypoints"):
- `PlanningRequest.map` holds every screen of the level seen so far (unseen cells `?`), with row numbers and a column ruler.
- The Azure planner may return up to 5 standing tiles as `waypoints`, and is asked to do so when stuck. Python checks them against the map (invalid ones go back as retry feedback), and Jev follows them one by one ahead of the route and reach waypoints.
- The map is the same for every arm.
- **Not yet run live** (paid): the offline planners give no waypoints.

**Tests:** `test_graph_store.py` (7), `test_threats.py` (11), `test_planner_map.py` (5). The goal-route, parity, benchmark, CLI and inspect tests were updated for stores. 280 pass, including the real-game tests.

**Replay note:** the screen changes what the tactical models see, so runs recorded before it no longer match their digests in `inspect`.

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

```bash
# Phase 9
uv run pytest                                                  # 245 passed, 1 skipped
uv run dave-agent benchmark --arms A,B,C --trials 3 --id repro --out R1   # and again with --out R2
#   fixture (labeled "synthetic test platformer, not Dangerous Dave"), 9 episodes, status complete;
#   manifest, episodes.jsonl, summary.json and pairs.csv identical across R1/R2 apart from VOLATILE_FIELDS;
#   A/B/C identical (same seeded mock): 0/3 completions each, Wilson CI [0, 0.5615], paired diff 0, McNemar p 1
uv run python scripts/skill_frames.py artifacts/watch.sqlite artifacts/p8-live.sqlite artifacts/events.sqlite
#   1707 Dave executions: walks 24/72, long jumps p50 44, short jumps p50 85-95, max 138 frames
uv run dave-agent benchmark --config configs/benchmark_dave.yaml --adapter dave --scenarios level1 --arms A,B,C --trials 3
#   mock on Dave: 9 episodes in 36 s, all truncated at max_frames:3600 (the random mock never finishes level 1)
uv run dave-agent train-memory --config configs/benchmark_dave.yaml --adapter dave --scenarios level1 --arm C --episodes 3 --out CK.json
#   3 x 3600-frame episodes: 15 nodes, 9 visited, 24 edges (48 attempts, all successes); the 600-frame live run learned 0 edges
uv run dave-agent benchmark --config configs/benchmark_dave.yaml --adapter dave --scenarios level1 --arms B,C --trials 2 \
    --memory-regime warm --checkpoint C=CK.json
#   complete; checkpoint sha256 76daaa84... unchanged; memory B none, C frozen-checkpoint; same_level_learning [level1]
uv run dave-agent benchmark ... --arms A --trials 1 --reach-hints off   # ablation: manifest and records say reach_hints false
uv run dave-agent play --arm A|B|C ; play --adapter dave --scenario level1|level2
#   unchanged: fixture 41/17 38b901ad24333f4f; level 1 97b1dc188deb88ba; level 2 2a57449186e2a6b3

# After Phase 9 (experience notes, interrupt, route search)
uv run pytest                                                  # 251 passed, 1 skipped
uv run dave-agent play ...                                     # traces unchanged (mocks ignore descriptions)
uv run python scripts/search_route.py --scenario level1        # found, 8 skills, 226 states, 103 s
uv run python scripts/search_route.py --scenario level2 --max-states 4000 --max-depth 60     --out artifacts/search/level2-tile.json                    # found, 37 skills, 505 states, 566 s
uv run python scripts/try_skills.py --config configs/benchmark_dave.yaml --scenario level2 <route>
#   level_complete, 1928 frames

# Decision inspector
uv run pytest                                                  # 258 passed, 1 skipped
uv run dave-agent inspect --store artifacts/benchmark-dave.sqlite --run-id demo-C-L2-1
#   92 of 92 model decisions rebuilt with matching digests (graph from arm-C/dave.json.bak), 92 bundles with
#   outcomes and screenshots, 5 min 20 s
cd frontend && npm run lint && npx tsc --noEmit && npm run build   # clean, no warnings
npx next start   # /, /runs/demo-C-L2-1, /runs/demo-C-L2-1/94, /api/shot/demo-C-L2-1/94 -> 200; unknown run,
                 # unknown decision and a traversal id -> 404
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
- **A web viewer after all (user decision, 2026-10-03).** The spec says "do not build a web application" for V1 (`implementation/01-project-overview.md`). On request, `frontend/` is a local, read-only Next.js viewer of inspection bundles: no deploy, no model calls, no writes, and no game assets in `public/`.
- **The inspector rebuilds rather than logs request text.** The store already holds a digest per request. A replay checked against those digests gives the exact text without growing every episode store, and proves the replay is exact.
- **Level 2 is completable with the catalog alone too** (`scripts/search_route.py`, 37 skills, 1928 frames). So level-2 failures are decision and memory problems as well, not missing skills.
- **Experience reaches the models as candidate notes, never as masks.** Masking a move that killed Dave would be a hand-written policy doing the model's job. Notes give every arm the same facts, and graph arms add their past-run evidence, which keeps memory the variable between B and C.
- **Level 1 is completable with the catalog alone** (`scripts/try_skills.py`, 10 skills, 554 frames; see `docs/skills.md`). Failures to finish are decision and information problems, not missing skills.
- **Reachability waypoints and estimated end tiles** (every arm, Dave only). With the waypoint alone, Jev still jumped into the ceiling at (7,9) 310 times. With the estimated end tile on each candidate, live Jev finished level 1: 11 decisions, 470 frames, $0.001 (run `diag-jev-3`). Without either, it never left the floor in 400 decisions (`diag-jev-1`). The models now mostly choose among estimated outcomes; Phase 9 should report this as the setup, and an ablation without estimates is worth keeping.
- **Episode-level statistics, stdlib only.** No pandas or numpy dependency: a Wilson interval for completion, a seeded percentile bootstrap over whole pairs for paired differences, exact McNemar for discordant pairs. Frames are never samples.
- **A fresh adapter process per benchmark episode.** Adapter episode counters and any hidden state cannot depend on arm order. On Dave this costs one bridge start per episode.
- **Unknown cost is null, offline mocks are zero.** A paid provider's call with no reported or estimated cost makes the episode's `cost_usd` null and is counted; summaries then give no cost per attempt instead of an underestimate.
- **Warm checkpoints are reloaded every episode and never saved.** Nothing a goal manager does in memory can carry between trials, and the file hash is verified at the end.
- **Benchmark output directories are never reused.** A non-empty `--out` is refused, so records from two runs cannot mix.
- **No cost budget for now:** it could only ever fire for Jev, which would make the arms asymmetric. Jev costs about $0.0001 per decision.

## Open issues

- **Reach estimates are one column off** (found with the inspector; see above). Candidate notes and waypoints can point to the wrong tile. Fix and re-check against real outcomes (`inspect` writes both) before the next live run.
- `explore` goals put the waypoint on Dave's row at the level's right edge, even behind a pillar. This drives back-and-forth walking. Planner waypoints over the explored map are meant to fix this, but have not run live yet.
- The threat screen applies to standing and falling Dave, not mid-jump. Monster motion is linear extrapolation. In mock runs the remaining risk is random walks off ledges that the screen keeps because every option is equally risky.

- Asset licensing: deadly-dave's `res/` art and levels come from the original game. Use them locally only; do not commit them here.
- Jetpack (`P`) is wired but not exercised. Fire is verified (Phase 3).
- Climbing needs an input sequence that has not been measured yet. Trees are observed as `climbable` tiles, but no skill uses them.
- The landing cooldown (5 ticks) is hidden state. A jump requested right after landing spends up to 5 ticks in its first phase.
- `configs/benchmark_dave.yaml` values (memory window 600, stuck 240, debounce 150, goal timeout 1200, episode 3600 frames) come from skill durations and the level-1 route length, not from benchmark outcomes. Revisit them after the smoke trials. `experiments.yaml` keeps the fixture-scale values.
- Mock tactical controllers ignore goals, so mock runs mostly show `stuck` and expiry triggers.
- `explore` targets are a direction and a column, not a verified reachable location.
- Azure cost is not computed (no price table); token usage is recorded per call, so `max_cost_usd_per_episode` cannot fire for Azure.
- Live Azure tactical calls average about 2.5–2.8 s, mostly reasoning tokens (about 380–430 per call at `reasoning_effort: low`). Lower settings were not tested on this deployment. Jev calls average about 0.2 s, but the game is paused during decisions, so latency does not affect play yet (Phase 11).
- Jev's chosen-candidate probabilities on Dave were mostly 0.24–0.52, spread across walking and jumping skills.
- The planner (live and rule-based) always prioritises the trophy, so Dave no longer wanders into coins as the random mock did. This is intended: coins only add score.
- The route-relevant facts the models lack are which jump lands where (narrow pillars), and the hidden landing cooldown, which `wait_short` covers but which is not observable.
- In the 600-frame live C run, the graph learned 15 nodes but no edges. With 3600-frame episodes, 3 mock training episodes learned 24 edges (Phase 9). Whether those routes help a live arm C is untested.
- The store schema changed to v2. Export old stores to JSONL before deleting them.
- No live benchmark has run. The paid smoke run is below; Azure prices must be filled in first (or `--allow-unpriced`).
- Continual learning (graph updates across evaluation episodes, with a learning curve) is not implemented; the spec marks it optional.
- The graph's segment rule marks a "platform" above the top brick row (level 1, row 0). Dave reaches it only by wrapping, so it mostly shows up as frontier.
- Edge keys ignore the start position within a segment, so a failure whose target is ambiguous stays on the node.
- `EpisodeResult` still keeps every per-frame `StepResult` in memory. That is fine at the current episode lengths, but revisit it before 18000-frame benchmark episodes.

## Next steps

**Paid smoke run (not yet run; your call).** First set `models.planner.price` and `models.tactical_llm.price` in `configs/models.yaml` to your Azure deployment's prices, with the source and date. Then:

```bash
uv run dave-agent benchmark --config configs/benchmark_dave.yaml --adapter dave --scenarios level1 \
    --arms A,B,C --trials 3 --planner live --tactical live
```

- **Hard caps:** 9 episodes, worst case 3600 tactical calls (400 per episode) and 270 planner calls (30 per episode). The $5 ceiling (`benchmark.paid_run_budget_usd`) is checked before each episode.
- **Expected:** far less. A level-1 completion took 11 decisions. Jev cost about $0.0001 per decision. An Azure call used about 1.5k tokens (planner and tactical) and took about 2.5–2.8 s.
- **Then:** about 30 paired trials per scenario, with `--reach-hints off` as the ablation, and a warm regime for arm C from `train-memory`.

**Phase 10** (`implementation/12-phase-10-reporting-replay-and-human-inspection.md`):
- Markdown/HTML reports built on `summary.json`, `pairs.csv` and `episodes.jsonl`: config table and limitations, completion with intervals, cost and latency, deaths and termination reasons, learning curves for warm and continual regimes, controller, fallback and planner breakdowns, trace examples, and a graph diagram.
- A step inspector (observation, goal, candidates, decision, outcome at one step).
- Replay that tells input replay apart from re-querying models.
