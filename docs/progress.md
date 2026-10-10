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

## Live viewer (2026-10-04)

`dave-agent live` and the `/live` page in `frontend/` (`docs/live.md`) let you choose a level and arm in the browser, then watch:
- the real game (bridge frames, about 20 per second);
- every tactical decision as it is made: Jev's probabilities, the skills the threat screen removed and why, and the outcome;
- the planner's explored map, goal, waypoints and reasons.

How it works:
- **Recorded like `play`:** a live run goes through `run_trial`, so it is recorded like `play` and can be inspected.
- **Write-only hook:** the stream comes from the new `on_event` hook in `run_episode`, which changes no decision.
- **Paid runs** need `--allow-paid`.
- **Setup shared with `play`:** graph setup moved to `session.open_graph`, used by both `play` and `live`.

Checks:
- tests: `test_live.py` (6);
- checked on level 2 in headless Chrome;
- a stopped live run replayed in `inspect` with its digests verified.

## Planner: platforms, physics-checked waypoints, feedback (2026-10-04)

Watching level 2 live showed the Azure planner sending Dave through brick pillars and over a brick corner, and re-proposing a route that had just stalled. Changes (`docs/planner.md`):
- **Platforms:** the request lists the explored level as platforms with their exits and reachability (`control/platforms.py`), and each candidate gets its estimated platform chain (`path`). Walls show up as missing exits.
- **Waypoints checked with the physics:** each leg must be reachable from the one before, and the goal from the last; the feedback names the exits. Waypoints may be platform ids (`"c8r4"`).
- **Feedback:** `attempts` (goals tried on this level and how they ended) and `failed_links` (moves that left Dave stuck or killed him; each failure makes the move cost more in the reach estimate).
- **Reach fix:** jumps from a platform's end (Dave overhanging the edge). Before it, the estimate found no way from the level 2 start area to the trophy; now it finds the real climb (verified with `scripts/try_skills.py`).
- **Tactical end-tile notes** from Dave's pixel position: 28 of 34 right on the real level 2 route, against 18 before.
- **Projectiles:** not sent to the planner (a planner call takes seconds, a shot crosses the screen in about 2 s; the per-decision threat screen dodges). The viewer draws each visible threat's predicted path.
- **Viewer:** the estimated moves (jumps as arcs, unknown legs red), reachable platforms, failed moves, deaths, threat paths; "Path estimate", "Tried this level" and "Failed moves" rows.

Checks: `tests/unit/test_platforms.py` (11); 297 tests pass; mock runs on levels 2 and 3 (18,000 frames, 0 deaths; the mock tactical model chooses at random, so its score says nothing about planning); the `/live` page checked in headless Chrome. Not run: a paid Azure planner run with the new request.

## Shots, mid-jump and take-offs (2026-10-04)

Live level 3 and level 4 runs: Dave stuck between two vine clumps, the threat line drawn through walls, and the level 4 swirl's shots killing Dave.
- **Monsters and shots predicted exactly:** bridge protocol 2 exports each monster's route, step cooldown and shot countdown; `control/threats.py` replays monster.c, including the shots not fired yet. Real game: exact over 120 ticks on level 4. The viewer draws routes (through walls, as the game moves them) and shots (stopping at walls).
- **Mid-jump decisions are screened:** the observation's `jump_tick` gives the rest of the arc; a walk chosen in the air keeps walking after landing.
- **Take-off search** up to 3 walks deep (level 3's vine tunnel), and route notes skip skills the screen removes.
- **Line of fire** on platforms for the planner, and as extra cost in the reach estimate.
- **Jump trajectories** on the map are the simulated flights.

Route follower on the real game (rule planner): levels 1 and 2 still complete with no deaths; level 3 deaths 4 -> 1; level 4: 2 deaths in 18,000 frames under the swirl. 305 tests pass.

**Rebuilt bridge (resolved):** the build id used to include the bridge executable's hash, so arm C's learned graphs (`artifacts/graphs/arm-C/dave/`) were refused after the rebuild, although the physics did not change. The build id now hashes the game's sources and levels only (`docs/graph.md`), and the arm C store was re-keyed with `dave-agent graph --rekey dave` (backups: `*.json.prekey`; 5 levels, 123 edges kept).

## Jev follows the plan (2026-10-04)

Live Azure + Jev on level 2 made good plans that Jev did not carry out: after waypoint (34,4) Jev was told "heading to (47,2)" (the next planner waypoint, 12 columns right) and walked off the ledge.
- **Waypoints followed one landing at a time:** with planner waypoints, the goal's waypoint is the next landing toward the first of them.
- **`route:` notes** on the options that make the next move, or walk to its take-off (simulated); the tactical task says to prefer them. The viewer shows a `route` badge and "on route" / "off route" per decision.
- **Explore goals** head for the nearest reachable platform with an unexplored end; the rule planner prefers goals with a known path.
- **Failed moves** are only estimated moves (no more lines from Dave to off-map targets) and are cleared once made.

Checks: 300 tests pass. A scripted controller that always takes the `route:` option, with the rule planner, completes level 1 and level 2 on the real game with no deaths (level 3: gun, no trophy in 18,000 frames). Not run: a paid live Jev run with the notes.

## Live graph and run stats (2026-10-04)

The `/live` page gains two sections below the planner (`docs/live.md`):
- **Run stats** (`StatsPanel`): tactical decisions by Jev or the LLM, forced and fallback; planner calls; latency, tokens and cost per role; skill outcomes.
- **Learned graph** (`GraphPanel`, arm C): the level's platforms and learned moves, updated after every skill from a new `graph` event (`WorldGraph.view()`, read-only; emitted only when a viewer is attached). The hub keeps only the newest snapshot.
- Call views now carry `tokens`.

Checks: `test_live.py` (graph events once at start and per skill, none without a graph, hub keeps the newest), `test_graph_store.py` (`view()` reads only, matches `counts()`); `npm run lint`, `tsc`, `build`; level 2 arm C mock run checked in headless Chrome (45 edges, 606 attempts on the stored graph).

## The jetpack (2026-10-04)

Human play reached level 4's trophy and door, and level 3's door, only with the jetpack; the catalog had no jetpack skill. Added (`docs/skills.md`, `docs/planner.md`):
- `jetpack_on`, `jetpack_off`, `fly_<up|down|left|right>_<1|3>` and 2 px `fly_*_nudge` skills; walks need `not_jetpacking`;
- `reach.trace_flying` (dave.c rules), used by the threat screen while flying;
- flights in the reach estimate with the fuel Dave has, floating items as flight targets, and route notes for each step of a flight.

Found on the way: the route notes' take-off search took `fly_up_*` (up is the jump key) for jumps, and Dave paced between two walks on level 2. Only on-ground skills count there now.

Checks:
- `uv run pytest`: 326 passed, 1 skipped.
- Real game, `trace_flying` against the game tick by tick from level 3's jetpack: exact, including ceiling and wall stops.
- `follow_route.py`: level 3 **completes** (11924 frames, 3 deaths, 711 fuel left): it takes the jetpack and flies to the door. Level 1 completes in 478 frames, level 2 in 3934, no deaths.
- Level 4 (18000 frames): column 57, 1 death, the furthest any run got (paid run 2 reached 31). It did not reach the jetpack at (68,5) in time; the rule planner spent many goals on the trophy at (6,2), reachable only by flight.
- Not done: a walk loop between two take-off walks still shows on level 3 at (63,4)-(64,4) before the trophy (it ends after 8 walks).

## Shooting mid-air (2026-10-04)

The person shot level 3's spider from the air; the agent could not decide in the air at all. Added (`docs/skills.md`):
- the `threat_sighted` interrupt on walks and jumps: a monster or plasma coming into view anywhere stops the skill, mid-air included;
- mid-air options with landing tiles, shot notes and a `route:` note on the best landing (`GoalManager._air_notes`);
- a predicted kill counts: `shoot` is judged without the monster its bullet hits (no more shots from it);
- `shoot` description says bullets are unlimited.

Found on the way, both from the screen scroll freezing the game for 16 ticks (game.c):
- fixed-tick holds counted the frozen ticks, so `jump_right_5` across the screen edge let go early and dropped into the fire (2 of the 4 deaths). Frozen ticks no longer count (`ExecutionResult.frozen_ticks`);
- shots pressed during the scroll fired nothing: `shoot` needs `screen_still`.
- Route jumps whose flight enters an unseen cell are left out (`ReachMap.known_flight`): the long jump from (29,6) was simulated bouncing off the screen's edge and flew on into the fire.

Checks:
- `uv run pytest`: 326 passed, 1 skipped. The swirl forecast test walks with `threat_sighted` off (it checks the forecast, not interrupts).
- `follow_route.py --scenario level3 --frames 18000` (now also taking a shot whose note says it hits): before these changes it reached (29,6) and lost all 4 lives by frame 9742. Now it shoots mid-air, crosses all the pillars, takes the trophy at (67,9) and the jetpack at (66,9), and ends below the door at (69,2) with 1 life left (3 deaths, 2750 points). The door needs the jetpack, as in the person's run.
- Remaining deaths: a threat sighted mid-jump where every option is predicted to be hit (the scripted follower then takes the latest; a tactical model has the notes), and one shot that passed the spider (not checked yet).

## Human play and the 4- and 5-tile jumps (2026-10-04)

`scripts/record_play.py` records a person playing (keys per tick); `scripts/compare_play.py` replays the log and compares each move with the catalog (`docs/skills.md`, "Comparing with human play"). The person completed levels 3, 4 and 5 (logs in `artifacts/human/`). What the agent lacks, by level:
- **Level 3:** 25 of 32 jumps were 4-5 tiles (direction held 58-86 ticks, then dropped). The catalog had 2 and 6 tiles: from many pillars every forward jump was masked (the long one lands in the next fire pit). **Added** `jump_left/right_4` and `_5`. The person also fired during 7 jumps, shooting the spider ahead.
- **Level 4:** the trophy at (5,2) and the door at (97,2) were reached with the jetpack (picked up at (68,5)). The catalog has no jetpack skill, so level 4 cannot be completed; the planner's repeated `collect:trophy:c6:r2` goals expired for that reason. The ledge at (31,3) was passed by walking off its end to (32,6) and `jump_right` to (35,4): catalog moves.
- **Level 5:** the trophy at (46,2) was reached by climbing trees (516 ticks climbing; no climb skill), two gaps by jetpack, and 7 shots fired in the air.
- **All:** 7 + 3 + 1 mid-air reversals (one, on level 5 at (66,3), avoided a hazard every catalog jump lands in); many walks under 1 tile and waits of 12-47 ticks.
- The threat screen masks the exact jumps the person died on at the level 4 swirl (shot at 22 and 27 ticks).

Also fixed: `frontier` was empty when the explored edge is a fire pit, so `explore:right` gave no route on level 3 (`docs/planner.md`).

Checks:
- `uv run pytest`: 326 passed, 1 skipped. Route tests updated where a 4- or 5-tile jump is now the cheaper move.
- `follow_route.py`: level 2 completes in 3793 frames (6549), no deaths, the trophy taken in flight by `jump_right_5`. Level 3 takes the gun with `jump_right_4` then `jump_right`, crosses the screen edge, and reaches (29,6), using the new jumps along the pillars. It dies 3 times, each in a jump across the screen edge into the spider's plasma: the spider is unseen until the screen scrolls, mid-flight. The person died there too and then shot it from the air. `audit_threats.py`: `jump_right_4` from x 110 lands at x 174 as predicted.

Next: the jetpack (needed for level 4), climbing (level 5), decisions in the air (shooting, reversals).

## Level 4 timing and the screen edge (2026-10-04)

In live arm C run `live-20261004T183734-4fd083` (level 4), Dave died 3 times, each by the swirl's shots, and for decisions 236-311 he paced at x 490-506 on the ledge at (31,3) while the screen did not scroll. Every death was logged as `cause: unknown`.

The causes, found by replaying the recorded states and running the real game (`scripts/audit_threats.py`):
- **The predicted paths were off.** A jump was predicted one tick early, and up to five ticks after a landing (the game's jump cooldown, now `Observation.jump_cooldown` from the bridge). In the air the model moved Dave 1 px every tick; the game moves him 2 px every other tick. The wall test used Dave's whole body; the game tests only the leading edge. Ground was tested at x+4..x+8; the game uses x+4..x+9. Head bumps and walks were also timed differently. All now follow dave.c (`docs/skills.md`). Along the recorded path, Dave is now within 1-2 px and the swirl and its shots match exactly.
- **A dodge was cut short.** `new_hazard_nearby` stopped a jump chosen to dodge the swirl's next shot when that shot appeared. Predicted shots no longer interrupt.
- **Landing in a trap went unseen.** A move counted as safe if its own path was, even when it landed where the next shot hit before Dave could move, or where every next move was hit. Plasma is now checked 16 ticks past the landing, scripted monsters over the whole path, and a landing with no safe next move is a `trap` contact.
- **Waiting was never a plan.** A wait was judged by standing still for 48 ticks. Now it is safe when a move is safe after it, and the notes say when to go: `timing: go now, unsafe if started 18 or more ticks later`, `timing: standing is safe for 31 ticks; then safe: jump_left; later: jump_right from 24 ticks`.
- **The screen edge.** The game scrolls only past x 520 there, and Dave can stand no further than x 507. Unseen cells were treated as walls, so every move right was simulated bouncing into the fire below and masked. Now unseen cells are unknown: such moves are noted (`passes the screen edge ...`), not masked, and on an explore goal with no known way on they are marked `route: reveals the map to the right`.
- **Also:** `shoot` notes say whether the bullet hits (level 4 has no gun), and deaths by a monster's plasma are logged as `plasma`.

Checks:
- `uv run pytest`: 324 passed, 1 skipped.
- The recorded states: the fatal choices before each death (`jump_left` at frames 1814 and 3864, `jump_right_short` at 5446) are now masked. On the ledge, the moves right are offered with the screen-edge note.
- Real game, level 4, a controller that only takes what the screen keeps (`ledge.py` in the session scratchpad, not kept): 8 runs of 400 decisions around the swirl, 0 deaths (the recorded run had 3 there). It did not climb to the ledge; choosing the moment from the timing notes is the tactical model's job.
- `follow_route.py`: level 1 completes in 470 frames, level 2 in 6549 frames (it was 6941), level 3 takes the gun; no deaths. Level 2 first stalled at the right screen edge because the edge note came before the `route:` note; the route note now leads.
- The bridge prints `jump_cooldown`; the build id hashes the game sources and levels, not the bridge, so learned graphs stay valid.

Paid run (arm B, Jev tactical, rule planner, store outside `artifacts/`): 391 decisions, 12737 frames, **0 deaths** (the recorded run: 3 deaths in 14848 frames), $0.04, stopped by the 1M-token budget. Dave got no further than (26,3): the rule planner cycled between the loot at (27,5) and (29,2) for most of the run, and Jev spent it timing moves around the swirl.

Paid run 2 (`paid-l4-live`: arm B, Azure planner and Jev, token caps raised in a scratch config): 18011 frames (the episode limit), 477 decisions, **1 death**, Jev $0.05 (Azure cost not priced). Furthest point (31,6), under the ledge; the screen never scrolled. The planner chose loot goals for almost the whole run, and most expired on their deadline before the next was set; explore:right was set only twice.
- The death: a `jump_left` predicted safe was hit 48 ticks in by a shot the model did not foresee. The swirl's previous shot was flying right, off the screen, into the unseen wall at column 35; it died there and the next one came at Dave about 40 ticks early. Shots are now forecast both ways (`docs/skills.md`); the move reads `danger: the next shot hits in 46 ticks` and is masked. The death was logged `unknown` because the shot that hits Dave is gone on his first burning frame.

Not verified: whether the long jump from the ledge end at (31,3) lands in the gap at (35,4) (the level file suggests it); no run has reached the ledge since the change. Getting there is now a goal-choice problem more than a timing one.

## Level 3 gun and goal credit (2026-10-04)

The problem: in a live arm C level 3 run (Jev with the Azure planner), Dave spent 175 of 246 decisions shuffling between (7,6) and (8,6) and never took the gun at (10,4).
- **Not the physics.** Jumps right from (8,6) really burn on the vine (the threat screen was right), and the reach estimate's own path was right too: walk to (6,6), then a long jump that takes the gun in flight and lands on (12,6). The real game confirms it (`scripts/try_skills.py --scenario level3 jump_right move_left_1 move_left_1 jump_right`).
- **The route notes compared exact cells.** Dave lands at x 126, not a multiple of 16, so from his real take-off (x 94) the jump lands on (11,6) and not the planned (12,6). No option at (7,6) or (8,6) got a `route:` note (found by replaying the recorded choices).
- **Nothing measured progress.** The graph scored the c2↔c6 jumps 37/37 and 29/29.

Changes (`docs/planner.md` "Reachability waypoints", `docs/memory.md` "Goal credit", `docs/graph.md` "Route search"):
- **Route notes match landings by platform** (`ReachMap.same_platform`) and judge hazards with the game's contact test (`ReachMap.burns`).
- **Mid-air pickups:** `targets_for` adds the take-offs of jumps whose flight touches the item (`grab_takeoffs`); the option whose path takes the target gets `route: picks up the gun on the way`; the waypoint is the take-off while Dave walks to it. `DAVE_BOX` and `HAZARD_BOX` moved to `reach.py` (`threats.py` imports them).
- **Goal credit:** every arm gets `for this goal from here: …` notes from this episode. Arm C learns per-platform credit per target across runs (`past goals like this one from this platform: 3/4 reached …`), and its learned routes weigh moves by it (`graph.credit_bonus`, `graph.credit_penalty`). The viewer's graph panel shows the credit.
- **Descriptions** are capped at 320 characters (`DESCRIPTION_MAX`), up from 200, because the notes were being cut.
- **`scripts/follow_route.py`:** the real game with the rule planner and a route-following controller.

Checks:
- `uv run pytest`: 322 passed, 1 skipped, including `test_credit.py` (4), new level 3 cases in `test_platforms.py`, and `tests/integration/test_dave_credit.py` (real game: the gun on the 4th move, no deaths, `jump_right` credited).
- `follow_route.py`: level 3 takes the gun with no deaths (it then waits under `explore:right` from (1,5), where no option gets a route note); level 1 completes in 470 frames; level 2 completes in 6941 frames with no deaths (the rule planner re-picked `collect:loot:c36:r9` 20 times).
- Two in-memory level 3 runs on one graph: the second run's options carry the learned credit.
- Offline traces: fixture 41/17 `38b901ad24333f4f` and level 1 `97b1dc188deb88ba` are unchanged. Level 2 is `de71bd676c0b1599`, the same with the new features patched out, so it already changed with the threat screen; the `2a57449186e2a6b3` above predates it.
- Not run: a paid live Jev run with the new notes.

## Live viewer: pause off (2026-10-06)

Phase 11 item 1 has started, in the live viewer only. The `/live` toggle **Pause game while models think** (`POST /start` `pause: false`) runs one episode with `execution_mode: real_time`:
- Planner and tactical calls run on a worker thread. Meanwhile the game ticks on with no keys pressed.
- A late choice is revalidated on the latest observation. It is dropped and decided again (`decision_stale`) after a death or respawn, or when its skill is illegal or screened. Otherwise it runs, and `decision_latency` records the wait.
- Inspect and replay refuse these runs. `play` and `benchmark` are unchanged (paused). See `docs/live.md`.

Checks:
- `uv run pytest`: all pass, including `tests/unit/test_realtime.py` (4).
- Real game, level 1, rule planner, a mock controller sleeping 0.2 s (Jev-like), 1500 frames: 20 decisions, each waiting about 16 ticks, no stale choices, no deaths.
- Not run: a paid live Jev or Azure run with pause off.

## Planner: rule first, loot, prerequisites, fuel; graph memory per execution mode (2026-10-08)

Live runs showed the Azure planner choosing loot almost a whole level-4 run, sending waypoints through walls, and both planners spending goals on a flight-only trophy before Dave had the jetpack (`docs/planner.md`):
- **Rule first** (`planning.llm_calls: escalate`, `configs/watch.yaml`, `configs/benchmark_dave.yaml`): the rule priority chooses with no call; the planner model is called on `stuck`, `repeated_failures` or a death, or when the rule's choice already failed `rule_repeat_limit` (2) times on the level. `goal_set` records `planner: rule | llm | fallback`. `configs/experiments.yaml` (fixture scale, the tests' config) stays `always`.
- **Loot:** the prompt says the trophy and the door finish a level and loot is score only; loot with no known path is no longer offered.
- **`requires`:** an item or door goal reachable only by flight while Dave has no fuel requires the jetpack (its path says where it is), and the rule takes the jetpack first.
- **Azure planner:** no map grid when platforms are sent (`models.planner.send_map: false`), waypoints are platform ids only, `reasoning_effort_stuck: medium` on stuck triggers (`max_completion_tokens` 4000).
- **Fuel:** the shared game rules state the jetpack's fuel use; the planner sees `player.fuel` (`left`, `reserve`) and is told to keep fuel for a flight the trophy or door may need; the tactical task says to turn the jetpack on only on a `route:` note and land rather than hover; an off-route `jetpack_on` option says `uses fuel (N left): not needed for the planned route`.
- **`scripts/replan.py`:** re-asks the recorded planner requests of an inspection bundle with the current prompt (paid; not run yet).
- **Graph memory per execution mode:** checkpoints record `execution_mode` and a mismatch is refused; real-time play (live viewer, pause off) uses `artifacts/graphs/arm-<ARM>/<adapter>-realtime/`, paused play keeps `artifacts/graphs/arm-<ARM>/<adapter>/` (`docs/graph.md`). The live graph panel says "paused memory" or "real-time memory".
- **Graph reset:** arm C's store (`artifacts/graphs/arm-C/dave/`, levels 1-6, 15 live runs, one of them pause off) was deleted on request, so both memories start empty. Old arm-C runs can no longer be inspected exactly (`inspect` needs the store from before the run).

Checks: 375 tests pass. `follow_route.py` (rule planner, route follower): levels 1 and 2 give the same results as before (level 1 in 478 frames, level 2 in 3805, no deaths); the level 3 and 4 runs after the change were stopped before they finished. Before the change the follower completed level 3 (3 deaths) re-picking `collect:loot:c19:r4` about 27 times, and on level 4 it set the flight-only trophy 10 times, took the jetpack late and did not finish in 18,000 frames. Not run: a live planner run with the new prompt.

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
- `explore` goals put the waypoint on Dave's row at the level's right edge, even behind a pillar. Candidate `path`s now name the nearest reachable platform instead, and planner waypoints are checked with the physics, but neither has run with the live planner yet.
- Platform paths are estimates: collectibles touched only mid-air (e.g. the level 2 gem at (8,8) over the fire) show "no known path".
- The threat screen applies to standing and falling Dave, not mid-jump. Monster motion is linear extrapolation. In mock runs the remaining risk is random walks off ledges that the screen keeps because every option is equally risky.

- Asset licensing: deadly-dave's `res/` art and levels come from the original game. Use them locally only; do not commit them here.
- Jetpack (`P`) is wired but not exercised. Fire is verified (Phase 3).
- Climbing needs an input sequence that has not been measured yet. Trees are observed as `climbable` tiles, but no skill uses them.
- The landing cooldown (5 ticks) is hidden state. A jump requested right after landing spends up to 5 ticks in its first phase.
- `configs/benchmark_dave.yaml` values (memory window 600, stuck 240, debounce 150, goal timeout 1200, episode 3600 frames) come from skill durations and the level-1 route length, not from benchmark outcomes. Revisit them after the smoke trials. `experiments.yaml` keeps the fixture-scale values.
- Mock tactical controllers ignore goals, so mock runs mostly show `stuck` and expiry triggers.
- `explore` targets are a direction and a column, not a verified reachable location.
- Azure cost is not computed (no price table); token usage is recorded per call, so `max_cost_usd_per_episode` cannot fire for Azure.
- Live Azure tactical calls average about 2.5–2.8 s, mostly reasoning tokens (about 380–430 per call at `reasoning_effort: low`). Lower settings were not tested on this deployment. Jev calls average about 0.2 s. The game is paused during decisions everywhere except the live viewer with pause off (2026-10-06), so latency does not affect benchmark play yet (Phase 11).
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
