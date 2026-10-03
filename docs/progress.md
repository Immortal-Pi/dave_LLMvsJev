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
| 6: Strategic planner and goal manager | **Done** offline. Live Azure planner verified (planner only; tactical still mock) |
| 7: LLM tactical baseline | Next |
| 8–11 | Not started |

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

## Verification (run 2026-10-03)

```bash
export UV_PROJECT_ENVIRONMENT=jev   # PowerShell: $env:UV_PROJECT_ENVIRONMENT="jev"
scripts\setup_dave.bat              # clone + patch + build (cmd/PowerShell)
uv run pytest                       # 178 passed, 1 skipped (live; RUN_LIVE=1). 24 drive the real game (-m dave)
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
```

Screenshot cross-check: after the pickup, Dave is drawn at about (15,112) against the recorded (14,112), the gem at tile (1,7) is gone, and the HUD shows score 100. Map rows line up at 16 px per tile.

These runs use **mock tactical controllers**. The only live evidence is the Azure *planner* (labeled `mode=live-planner`). No LLM or Jev tactical gameplay results exist yet, so live A/B integration is **not** complete.

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
- **Item-superset assumption:** an edge observed with items S is usable whenever S is held, even alongside other items. This is not verified for jetpack mode.
- **Episode keys are `run_id/episode_id`,** because adapter episode ids (`fixture_l1-s0-e1`) repeat across runs.
- **Truncation is checked between skills,** so an episode can overrun `max_episode_frames` by at most one skill (≤ 238 frames).

- **Process boundary for the GPL game.** The harness never links deadly-dave code. Our changes live in this repo only as `bridge/deadly-dave-bridge.patch`.
- **Snapshots = input replay**, not a C deep copy. This is exact because the game is deterministic (tested), and a replay that ends on a different tick raises an error. Cost grows with episode length.
- **Auto-release after reset or respawn:** the bridge presses a neutral `space` key once the blink timer allows. The ticks are counted (`auto_ticks`). This is identical for every arm.
- **Death cause `unknown`:** the bridge does not yet expose what set `on_fire`.
- **Fixture jump rule:** a jump triggers on a fresh press only. This applies to the fixture only.
- **Same seeded mock for both arms** in offline mode, so A and B trajectories are identical by construction.
- **Jev via OpenRouter**, and **Azure deployment from `AZURE_OPENAI_CHAT_DEPLOYMENT`**. No Azure calls yet.

## Open issues

- Asset licensing: deadly-dave's `res/` art and levels come from the original game. Use them locally only; do not commit them here.
- Jetpack (`P`) is wired but not exercised. Fire is verified (Phase 3).
- Climbing needs an input sequence that has not been measured yet. Trees are observed as `climbable` tiles, but no skill uses them.
- The landing cooldown (5 ticks) is hidden state. A jump requested right after landing spends up to 5 ticks in its first phase.
- The working-memory window (120 frames) is fixture-scale and holds only a few Dave skills. Calibrate it, and every `planning:` value, on Dave in Phase 9.
- Mock tactical controllers ignore goals, so mock runs mostly show `stuck` and expiry triggers. At fixture scale (41-frame episodes) the 60-frame debounce hides death triggers.
- `explore` targets are a direction and a column, not a verified reachable location.
- Azure cost is not computed (no price table); token usage is recorded per call.
- The graph's segment rule marks a "platform" above the top brick row (level 1, row 0). Dave reaches it only by wrapping, so it mostly shows up as frontier.
- Edge keys ignore the start position within a segment, so a failure whose target is ambiguous stays on the node.
- `EpisodeResult` still keeps every per-frame `StepResult` in memory. That is fine at the current episode lengths, but revisit it before 18000-frame benchmark episodes.

## Next steps (Phase 7)

1. LLM tactical controller (`models/`, Azure): chooses one `candidate_id` from the same candidates, observation and `MemoryContext` (including the goal and waypoint), with a strict JSON schema and the same validation and retry pattern as the planner.
2. Live run modes in `play` (drop the `--mock` requirement for arm A): validate prerequisites and show the configured budget before any paid call.
3. Record latency, tokens and failures per tactical call. Arm A smoke runs on Dave level 1, labeled live.
4. Keep arms B and C on mock tactical until Phase 8 (Jev client).
