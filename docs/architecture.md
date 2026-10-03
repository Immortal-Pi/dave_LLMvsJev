# Harness architecture

The full target design and phase plan are in `implementation/01-project-overview.md`. This page describes what exists now.

## Control flow

```
adapter.reset ─► Observation ─► generate_candidates ─► controller.decide ─► validate_decision
      ▲              (latest)       (legal mask)          (skipped if forced)        │
      │                                                                             ▼
      └── final observation ◄── execute: revalidate, then adapter.step(phase buttons, 1) per frame,
                                 polling for interrupts, until phases end or the cap is reached
```

`runner/episode.py::run_episode` owns this loop. It stops when the observation is terminal or when `benchmark.max_episode_frames` is reached (`truncated`). Planner, memory and graph will slot in around this loop in Phases 4–6, without changing the shared observation, candidate and executor path that arms A and B use.

## Modules (`src/dave_agent/`)

| Module | Role |
| --- | --- |
| `schemas.py` | Pydantic contracts: `Observation`, `Entity`, `Goal`, `SkillCandidate`, `Decision`, `StepResult`, `Event`, `ModelCallRecord`, `AdapterCapabilities`, `SnapshotRef`. They are frozen and use `extra="forbid"`. `PixelPos` and `TilePos` are separate types. Optional observation fields must be `None` **and** listed in `unavailable_fields`. `validate_decision` rejects unoffered or stale choices. |
| `adapters/base.py` | `GameAdapter` Protocol, exactly as in the spec. Agents never hold an adapter. |
| `adapters/fixture.py` | Synthetic deterministic tile platformer (levels in `tests/fixtures/levels/*.yaml`) with `local_observed` filtering. Always labeled `adapter=fixture`. |
| `adapters/dave.py` | `DaveBridgeAdapter`: runs `external/deadly-dave/deadly-dave-bridge.exe` as a subprocess (JSON lines), decodes and filters state to the viewport, derives velocities and events, and implements snapshots by replaying inputs. Adds `screenshot()` and `raw_state()` for debugging only. |
| `adapters/__init__.py` | `create_adapter(name, environment_config)`. |
| `control/skills.py` | `generate_candidates` builds a `CandidateSet` (legal candidates plus a mask and its reasons) from the adapter's catalog. `execute` revalidates, then steps phases one frame at a time with interrupt rules and a hard cap, and returns an `ExecutionResult`. `stale_fallback` is the real-time fallback. See `docs/skills.md`. |
| `control/predicates.py` | A registry of named `Observation` predicates used for preconditions and phase `until` conditions. |
| `models/base.py` | `TacticalController` protocol: `decide(observation, goal, candidates) -> (Decision, ModelCallRecord)`. |
| `models/mock.py` | A seeded uniform mock. Arms A and B use the same seed, so offline trajectories match. |
| `models/jev.py` | Parses the Jev decisions response (verified shape). The live client comes in Phase 8. |
| `runner/episode.py` | `run_episode`: the shared loop. It records a `CandidateRecord` (IDs, mask and digest) and an `ExecutionResult` for every decision, and forces single-candidate decisions without a model call. |
| `config.py` | Loads `configs/experiments.yaml` and its sibling files (`environment`, `models`, `skills`) into one validated `AppConfig`. Errors name the file and key. |
| `logging_setup.py` | Redacts API keys and bearer tokens from log output. |
| `cli.py` | `dave-agent probe` and `dave-agent play --mock`, each with `--adapter fixture\|dave` and `--scenario`. |

## Game bridge (outside the package)

- `bridge/deadly-dave-bridge.patch` adds `bridge.c`, a CMake target, and two small portability or visibility edits to deadly-dave.
- `scripts/setup_dave.bat` clones the pinned commit, applies the patch and builds via `scripts/build_dave.bat`.
- The protocol and its design choices are in `docs/feasibility.md` §2.

## Invariants enforced by tests

- LLM and Jev arms receive identical candidate lists and observations (`tests/unit/test_arm_parity.py`).
- Skills never exceed their phase-budget cap, and they apply exactly their phase buttons, one frame per step.
- Controllers can only pick a `candidate_id` that trusted code offered for the latest observation.
- Unavailable fields are never set to zero.
- The local-observed region never contains tiles outside the view window.
- Episodes on both adapters are deterministic per (scenario, seed, controller seed). Snapshots restore an identical future.
- Dave observations include only viewport tiles and on-screen entities.
