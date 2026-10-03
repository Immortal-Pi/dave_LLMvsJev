# Phase 0 feasibility report

Verification date: **2026-10-03**. Everything below is either verified against source or a live call (marked **verified**), or is a design that has not yet been executed (marked **planned**).

## 1. Selected game edition

| Item | Value |
| --- | --- |
| Implementation | [skoperst/deadly-dave](https://github.com/skoperst/deadly-dave), an open-source C reimplementation of *Dangerous Dave* (1988, John Romero) |
| Commit inspected | `950d39da2912a166e067062bc06461245cc897ab` (2026-02-03) |
| Local clone | `external/deadly-dave/` (gitignored; not redistributed by this repo) |
| Language / deps | C, SDL2 2.30.x fetched by CMake `FetchContent`, OpenGL; a Makefile is also provided |
| Code license | GPL-3.0 (`LICENSE`) |
| Assets | `res/levels/*.ddt` (text tile maps), `res/tiles/*.bmp` (259 files), `res/font/*.bmp`, all bundled in the repo. The README credits Malvineous for unpacking the original resources from `dave.exe`, so the art and level designs come from the commercial original. **Their license status is unclear**, so this repo must not redistribute them. |
| Randomness | No `rand`/`srand` in the game code: the game is deterministic given its inputs (**verified** by source read) |

Clone command: `git clone --depth 1 https://github.com/skoperst/deadly-dave external/deadly-dave`.

### Build status: **working** (2026-10-03)

The build uses VS Build Tools 2022 (MSVC 14.44) with the CMake and Ninja that ship with it. `scripts\setup_dave.bat` clones the pinned commit with `core.autocrlf=false`, applies `bridge/deadly-dave-bridge.patch`, and calls `scripts\build_dave.bat`. That produces `deadly-dave.exe` (the stock game) and `deadly-dave-bridge.exe`. The first build downloads SDL2 2.30.x.

Upstream does **not** compile on Windows as-is: `game.c` includes `unistd.h` for `access()`. The patch maps that to `<io.h>`/`_access` on `_WIN32`. Both executables must run from the deadly-dave repo root, because levels are opened via relative `res/levels/...` paths.

## 2. Adapter approach

| Option | Assessment |
| --- | --- |
| A. Emulator RAM reading (DOSBox + original `DAVE.EXE`) | Rejected. Stock DOSBox has no remote RAM or frame-step API. It would need a commercial binary plus RAM addresses we cannot verify yet. |
| **B. Instrument deadly-dave source (chosen)** | All state is in plain C structs (`game_context_t`, `dave_t`, `monster_t`). The loop is one `gameloop` iteration per 14 ms tick, so frame-exact stepping is natural. |

### Bridge (Phase 2, **implemented and verified**)

`bridge/deadly-dave-bridge.patch` (about 290 lines) is applied to the external clone; the GPL source is not vendored. The harness talks to the patched game over a **process boundary** (JSON lines on stdin/stdout), so no GPL code is linked into the Python package.

The patch contains:

- **`bridge.c`**: a separate console executable, `deadly-dave-bridge`. It reuses `game.c`'s own routines (`init_game`, `game_level_load`, `game_level`, `game_level_blinking`). It renders into an in-memory buffer (no window) and uses SDL's dummy audio driver, with no `SDL_Delay`.
- **CMake changes:** the `/SUBSYSTEM:WINDOWS` flag now applies to the stock target only, and a `deadly-dave-bridge` target is added.
- **`dave.c`:** `dave_on_ground` is no longer `static`.
- **`game.c`:** the Windows `access()` portability fix.

Behavior:

- **Commands:** `reset <level>`, `step <keys> <ticks>` (keys from `LRJDFP` or `-`), `observe`, `screenshot <path>`, `quit`. One tick is one `game_level()` call with held keys, the same as `get_keys()` provides each 14 ms tick in the stock loop.
- **Viewer:** `-window` opens a 960×600 window that shows every tick (including respawn release ticks); `-delay <ms>` pauses after each tick (default 14). It is display only and never changes game logic. A watched mock episode was byte-identical to the headless run. Closing the window falls back to headless.
- **Jetpack** is edge-triggered in the original (an `SDL_KEYDOWN` LALT event), so the bridge applies `P` on the first tick of a step only.
- **Release after reset or respawn:** `G_STATE_LEVEL_BLINKING` freezes the game until any key is pressed. The bridge runs no-key ticks until the blink timer allows release, then presses `space`, which has no gameplay effect. These ticks are reported as `auto_ticks`, so they count toward simulation frames.
- **Scrolling:** ticks where `game_level()` only scrolls the view and does not tick Dave are reported via `scrolling`.
- **Snapshots** are **input-log replay** (reset, then re-issue the recorded steps), done in `adapters/dave.py`. The game has no randomness, so this is exact, and a replay that ends on a different tick raises an error. This avoids a fragile deep copy of the pointer-linked C state. Restore cost grows with episode length.
- The game context is allocated with `calloc`, because `init_game` leaves several fields uninitialized.

### Capability matrix

| Capability | fixture adapter | deadly-dave bridge | Evidence |
| --- | --- | --- | --- |
| reset | supported | **verified** | `test_level1_start_state`: Dave at (32,144), 4 lives |
| observe structured state | supported | **verified** | `docs/state_mapping.md`; screenshot cross-check |
| exact frame step | supported | **verified** | `test_multi_tick_step_matches_single_ticks` |
| input press/release | supported | **verified** (held keys per tick) | jump and pickup tests; jetpack not yet exercised |
| snapshot/restore | supported | **verified** (input replay) | `test_snapshot_restores_identical_future` |
| headless | supported | **verified** | no window is created; dummy audio |
| death/respawn signal | supported | **verified** | `test_fire_death_respawns_at_start` (level 2 fire floor) |
| determinism | by construction | **verified** | `test_deterministic_replay`; identical mock episodes |

**Phase 0 acceptance gate:** passed for the real game (`dave-agent probe --adapter dave --scenario level1`: (32,144) → (34,144) after one tick right).

## 3. Verified Dangerous Dave mechanics (deadly-dave source)

- **Controls** (`get_keys`): Left/Right arrows walk; **Up = jump**; Down is used while climbing; **LCtrl = fire** (only with `has_gun`; one bullet on screen at a time); **LAlt = jetpack toggle** (key-down event); Esc opens a quit popup. There is no duck action.
- **Units:** positions are pixels (`tile->x`, `tile->y`), with `TILE_SIZE 16`. The map is `TILEMAP_WIDTH 100` × `TILEMAP_HEIGHT 12` tiles. The visible scene is 20 tiles (320 px) wide. `scroll_offset` is measured in **tiles** (screen x = `scroll_offset*16`), with a maximum of 80.
- **Dave states:** standing, walking, jumping (`jump_velocity_table`, `jump_state` 0–94), climbing (trees, `on_tree`), freefalling, jetpacking, burning, dead, blinking.
- **Lives:** start at 4 (`init_game`). Death happens when Dave's state reaches `DAVE_STATE_DEAD` after burning. Burning is caused by FIRE-mod tiles (fire, water, vines), touching a live monster, or a monster's plasma. Dave then respawns at the level's start position. Game over happens at 0 lives. **There is no health bar.**
- **Exit:** the **trophy** (`TROPHY` mod) sets `has_trophy`. Touching a `DOOR` with the trophy starts the warp to the next level; without it, nothing happens.
- **Items:** loot gems (score), gun (`has_gun`), jetpack (sets `jetpack_bars = 900`, which drains while flying).
- **Falling off the bottom** (`y > 200`) wraps Dave to `y = -20`. It is not a death.
- **Secret level:** level 5 has a warp-down secret, reached by walking off the map's left or right edge.
- **Monsters:** sun, spider, swirl, bones, UFO, guard. Each follows a fixed `route[]` and can shoot plasma. Bullets kill them.
- **Note:** the Dave–monster *body* collision loop only checks `monsters[0..4]` (`game_level`), while the plasma check covers all 10. This is recorded as an observed implementation detail, not something to correct.

## 4. Jev contract (OpenRouter decisions route)

| Item | Value | Source |
| --- | --- | --- |
| Endpoint | `POST https://openrouter.ai/api/alpha/decisions` | live probe (verified) |
| Auth | `Authorization: Bearer $OPENROUTER_API_KEY` | live probe |
| Model ID | request `typesafe/jev-1.13`; response reports `typesafe/jev-1.13-20260917` | live probe |
| Request | `{model, state (string, object or array), questions: {id: {type: "choice", instructions, criteria: {option: description}}}}` | [API docs](https://docs.typesafe.ai/api.md), [Choice](https://docs.typesafe.ai/primitives/choice.md) |
| Choice limit | up to 255 options | Choice docs |
| Response | `answers.<id>` = `{type, choice, probabilities{option: p}, confidence}`; also `usage{input_tokens, output_tokens, cost}`, `id`, `provider` | live probe (verified) |
| Confidence | "a number from 0 to 1 computed from how `probabilities` is spread". It describes the concentration of the distribution, not correctness. A threshold must be calibrated before use (Phase 11). | Choice docs |
| Errors | 401 bad key, 422 validation, 429 rate limit (use exponential backoff), 529 overloaded | API docs |
| Timeouts / rate limits | not documented | API docs |
| Probe result | HTTP 200, 545 ms, cost $1.86e-05 | `tests/fixtures/jev/choice_response.json` |

TypeSafe also offers its own endpoint, `POST https://api.typesafe.ai/v1/systemone` (model `jev-latest`), which needs a TypeSafe key. This project uses the OpenRouter route because that is the key available.

## 5. LLM provider

The planner and the LLM tactical controller use **Azure OpenAI**. Variable names are listed in `.env.example`, and the deployment is read from `AZURE_OPENAI_CHAT_DEPLOYMENT`. No Azure calls were made in Phase 0, so the contract will be verified in Phase 7.

## 6. Observation policy

The primary policy is `local_observed`. The agent sees the current 20×12-tile viewport (`scroll_offset` .. `scroll_offset+19`) plus a discovered-map memory. Tiles outside the viewport and monsters off screen must be dropped inside the adapter, before memory or any agent sees them. `oracle` (the full 100×12 map) is optional and reported separately. The fixture adapter implements the same rule with a ±`view_half_width` column window, and tests check that it does not leak tiles.
