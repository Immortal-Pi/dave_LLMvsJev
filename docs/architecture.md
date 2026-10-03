# Harness architecture

The full target design and phase plan are in `implementation/01-project-overview.md`. This page describes what exists now.

## Control flow

```
adapter.reset ─► GoalManager.reset ─► (planner on trigger) ─► WorkingMemory.set_goal
      │
      ▼
   Observation ─► generate_candidates ─► controller.decide(obs, goal, candidates, memory.context())
      ▲              (latest)       (legal mask)   (skipped if forced; ModelController: │ validate_decision
      │                                             retry, fallback, budgets)          │
      │                                                                             ▼
      │                         execute: revalidate, then adapter.step(phase buttons, 1) per frame,
      │                         polling for interrupts, until phases end or the cap is reached
      │                                                                             │
      └── final observation ◄── EventDetector (derived events) ─► WorkingMemory.record ─► EpisodeRecorder (write-only)
                                                               ├► WorldGraph.record_execution (graph arms only)
                                                               └► GoalManager.update: end due goals, triggers,
                                                                  planner call or fallback, route waypoint (graph arms)
```

`runner/episode.py::run_episode` owns this loop. It stops when the observation is terminal, or as `truncated` when `benchmark.max_episode_frames`, the wall-time budget or a tactical model budget runs out (`docs/tactical.md`); an exception is logged as `error` and then re-raised. The goal manager (Phase 6) runs at decision boundaries only: after reset and after each skill. It ends goals that are due, evaluates the shared triggers and, when needed, asks the strategic planner to choose among candidate goals. On graph-enabled arms it reads the learned graph (Python route search) to set the goal's next waypoint. The tactical controller receives the goal in `memory.context().goal`. The observation, memory, candidate and executor path stays shared by every arm. See `docs/planner.md`.

## Modules (`src/dave_agent/`)

| Module | Role |
| --- | --- |
| `schemas.py` | Pydantic contracts: `Observation`, `Entity`, `Goal`, `SkillCandidate`, `Decision`, `StepResult`, `Event`, `ModelCallRecord`, `AdapterCapabilities`, `SnapshotRef`. They are frozen and use `extra="forbid"`. `PixelPos` and `TilePos` are separate types. Optional observation fields must be `None` **and** listed in `unavailable_fields`. `validate_decision` rejects unoffered or stale choices. |
| `adapters/base.py` | `GameAdapter` Protocol, exactly as in the spec. Agents never hold an adapter. |
| `adapters/fixture.py` | Synthetic deterministic tile platformer (levels in `tests/fixtures/levels/*.yaml`) with `local_observed` filtering. Always labeled `adapter=fixture`. |
| `adapters/dave.py` | `DaveBridgeAdapter`: runs `external/deadly-dave/deadly-dave-bridge.exe` as a subprocess (JSON lines), decodes and filters state to the viewport, derives velocities and events, and implements snapshots by replaying inputs. Adds `screenshot()` and `raw_state()` for debugging only. |
| `adapters/__init__.py` | `create_adapter(name, environment_config)`. |
| `control/skills.py` | `generate_candidates` builds a `CandidateSet` (legal candidates plus a mask and its reasons) from the adapter's catalog. `execute` revalidates, then steps phases one frame at a time with interrupt rules and a hard cap, and returns an `ExecutionResult`. `stale_fallback` is the real-time fallback. See `docs/skills.md`. |
| `control/goals.py` | `GoalManager`: candidate goals from observed targets (`TargetMemory`), the goal lifecycle, shared debounced and capped planning triggers, validation, retry and deterministic fallback, and the route waypoint on graph arms. See `docs/planner.md`. |
| `control/predicates.py` | A registry of named `Observation` predicates used for preconditions and phase `until` conditions. |
| `memory/working.py` | `WorkingMemory`: the latest observation, goal, a bounded recent-history deque, derived motion, and progress/stuck/repetition counters. `context()` is the deterministic `MemoryContext` given to controllers. See `docs/memory.md`. |
| `memory/detector.py` | `EventDetector`: `inventory_changed` and `area_discovered` events from consecutive observations. |
| `memory/episodes.py` | `EpisodeStore`: the versioned SQLite schema with foreign keys and the JSONL export. `EpisodeRecorder`: batched, write-only episode logging. |
| `memory/graph.py` | `WorldGraph`: a NetworkX `MultiDiGraph` of platform segments (deterministic segmentation of observed tiles) and observed skill transitions, with raw success, failure and fatal counts and evidence. See `docs/graph.md`. |
| `control/reach.py` | Estimated reachability over the observed tiles: a tick-by-tick simulation of the catalog's jump shapes, walks and falls with measured movement (`skills.reach`), a cheapest path to the goal, the next landing spot (the goal waypoint) and each candidate's estimated end tile. It is an estimate, checked against real-game landings, not a learned route. |
| `memory/routes.py` | `find_route`: deterministic Dijkstra with inventory filtering, the trophy-gated exit, and an unreachable/frontier result. `RouteTracker` gives replan triggers. |
| `memory/persistence.py` | Versioned JSON graph checkpoints with atomic save and `.bak`, compatibility rejection, and a YAML export. |
| `models/base.py` | `TacticalController` protocol: `decide(observation, goal, candidates, memory) -> (Decision, calls)`, every model call made for the decision (retries included). |
| `models/tactical.py` | `tactical_request` (the one request every tactical model sees), the provider-neutral prompt text, `context_digest`, `parse_tactical`, `ModelController` (shared retry, deterministic legal fallback, per-episode call/token/cost budgets), `SeededMockModel` and `ScriptedTacticalModel`. See `docs/tactical.md`. |
| `models/planner.py` | `StrategicPlanner` protocol (`propose(request, feedback) -> (text, ModelCallRecord)`), `PlanningRequest`, `GoalCandidate`, `parse_plan`, plus the offline `RuleMockPlanner` and the test `ScriptedPlanner`. |
| `models/azure.py` | `AzureChatClient` (Chat Completions over httpx), `AzurePlanner` and `AzureTacticalModel` (verified prompt rules, strict JSON-schema choices). |
| `models/http.py` | `post_json`: the transport retry loop (timeouts, 429, 5xx) shared by the Azure and Jev clients, with redacted errors. |
| `models/mock.py` | `SeededMockController`, a seeded uniform mock used directly by tests. The CLI uses the equivalent `SeededMockModel` inside `ModelController`; arms A and B use the same seed, so offline trajectories match. |
| `models/jev.py` | The Jev decisions contract (`JevResponse`, `parse_choice`), `JevClient` (OpenRouter `alpha/decisions`) and `JevTacticalModel` (arms B and C: the arm A request as Jev state, one `choice` question over the offered ids). |
| `runner/episode.py` | `run_episode`: the shared loop. It records a `CandidateRecord` (IDs, mask and digest) and an `ExecutionResult` for every decision, forces single-candidate decisions without a model call, updates working memory, runs the optional goal manager (`result.planning`) and feeds the optional recorder. |
| `runner/replay.py` | `replay_episode`: re-executes an exported episode and reports any mismatch with the recorded candidates, outcomes or observations. |
| `config.py` | Loads `configs/experiments.yaml` and its sibling files (`environment`, `models`, `skills`) into one validated `AppConfig`. Errors name the file and key. |
| `logging_setup.py` | Redacts API keys and bearer tokens from log output. |
| `cli.py` | `dave-agent probe` and `play` (each with `--adapter fixture\|dave` and `--scenario`; `play` also takes `--store`, `--run-id`, `--graph`, `--planner mock\|live` and `--tactical mock\|live`), plus `probe-provider --provider azure\|jev [--purpose planner\|tactical] [--save-fixture]`, `export`, `replay` and `graph`. |

## Game bridge (outside the package)

- `bridge/deadly-dave-bridge.patch` adds `bridge.c`, a CMake target, and two small portability or visibility edits to deadly-dave.
- `scripts/setup_dave.bat` clones the pinned commit, applies the patch and builds via `scripts/build_dave.bat`.
- The protocol and its design choices are in `docs/feasibility.md` §2.

## Invariants enforced by tests

- LLM and Jev arms receive identical candidate lists, observations and memory context, and they produce identical episode logs apart from labels. Every model decision logs the `context_digest` of the shared tactical request, and the digests match across A, B and C offline (`tests/unit/test_arm_parity.py`). The Jev state equals the LLM's user message plus the same rules text (`test_jev_tactical.py`).
- Arms A and B send identical planner requests and get identical goals. Graph arms differ only by route summaries and waypoints. The planner is called only on documented triggers, never during stable execution. Unknown targets are rejected, and malformed output gets one retry, then a deterministic fallback (`test_goals.py`, `test_goal_routes.py`, `test_planner.py`, `test_arm_parity.py`, `tests/integration/test_dave_planner.py`).
- The learned graph contains only observed columns, and edges only from successful observed transitions. Checkpoints round-trip byte-identically and reject incompatible builds or policies. Graph learning alone does not change controller inputs (`test_graph*.py`, `test_routes.py`, `tests/integration/test_dave_graph.py`).
- Working-memory history stays bounded, and a reset clears it. The episode store survives a restart, and its JSONL export replays exactly (`test_working_memory.py`, `test_episode_store.py`, `tests/integration/test_dave_store.py`).
- Skills never exceed their phase-budget cap, and they apply exactly their phase buttons, one frame per step.
- Controllers can only pick a `candidate_id` that trusted code offered for the latest observation. Invalid, unauthorized or failed tactical answers are retried once, then replaced by the deterministic legal fallback, which is never counted as a model decision. Exhausted budgets end the episode as `truncated` (or fall back, as configured), and every action traces to one decision (`test_tactical.py`).
- Unavailable fields are never set to zero.
- The local-observed region never contains tiles outside the view window.
- Episodes on both adapters are deterministic per (scenario, seed, controller seed). Snapshots restore an identical future.
- Dave observations include only viewport tiles and on-screen entities.
