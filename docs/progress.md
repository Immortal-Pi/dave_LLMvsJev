# Progress

Spec: `implementation/` (phases 0–11). Status as of **2026-10-03**.

| Phase | Status |
| --- | --- |
| 0: Feasibility and scope gate | **Done**, including the real game |
| 1: Skeleton, schemas, offline contracts | **Done** |
| 2: Real structured-state adapter | **Done**: deadly-dave bridge verified |
| 3: Action catalog and deterministic execution | **Done**: calibrated on the real game |
| 4: Working memory and event history | Next |
| 5–11 | Not started |

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

## Verification (run 2026-10-03)

```bash
export UV_PROJECT_ENVIRONMENT=jev   # PowerShell: $env:UV_PROJECT_ENVIRONMENT="jev"
scripts\setup_dave.bat              # clone + patch + build (cmd/PowerShell)
uv run pytest                       # 90 passed (21 drive the real game, -m dave; they skip if the bridge is not built)
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
```

Screenshot cross-check: after the pickup, Dave is drawn at about (15,112) against the recorded (14,112), the gem at tile (1,7) is gone, and the HUD shows score 100. Map rows line up at 16 px per tile.

These runs use **mock controllers**. No LLM or Jev gameplay results exist yet.

## Decisions

- **Skills release Up as soon as a jump starts.** Holding it re-jumps on landing (measured).
- **`hazard_contact` interrupt:** burning means death is certain and input is ignored, so a skill stops at ignition instead of running about 200 more ticks. While Dave burns, only `wait_long` is legal, and it is forced with no model call.
- **`new_hazard_nearby`** (48 px) is the only state-reactive interrupt. It just stops the skill and never chooses an action, so there is no reflex, and it applies to all arms.
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

## Next steps (Phase 4)

1. Working memory: latest observation, a frame-indexed deque of recent observations and actions (bounded, configurable), current goal and skill status, derived motion, and stuck and repetition counters. It is cleared on reset.
2. SQLite tables `runs`, `episodes`, `events`, `decisions`, `skill_executions` and `model_calls`, with foreign keys, a schema version, batched writes and JSONL export. Executions link to their start and end observations (`ExecutionResult` already carries them).
3. Death cause stays `unknown` unless evidence establishes it.
4. Tests: bounded history, cleared on reset, persistence survives a restart, stuck detection on fixture sequences, and identical logging across arms.
