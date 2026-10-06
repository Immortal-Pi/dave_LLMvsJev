# State mapping: deadly-dave → `Observation`

Source: deadly-dave commit `950d39d` plus `bridge/deadly-dave-bridge.patch`. The decoder is `src/dave_agent/adapters/dave.py`. The mappings were **verified 2026-10-03** by `scripts/probe_environment.py`, the `tests/integration/test_dave_bridge.py` tests, and screenshots: Dave's recorded (14,112) matched the drawn sprite, and map rows are 16 px each with no HUD offset in level coordinates. "Avail." means availability under `local_observed`.

| `Observation` field | deadly-dave source | Units / notes | Avail. |
| --- | --- | --- | --- |
| `adapter`, `build_id` | `"dave"`, `deadly-dave-bridge-p<protocol>-<sha256(exe)[:12]>` | | yes |
| `level_id` | `game->level` (level files 1–9 plus `level5_secret` in `res/levels/`) and `level_secret_state` | e.g. `L5`, `L5_secret` | yes |
| `frame` | bridge tick counter, including release ticks after reset and respawn (`frames_advanced = frames + auto_ticks`) | the game's own `game->tick` is `uint8_t` and wraps, so it is not used | yes |
| `player_position` | `dave->tile->x`, `dave->tile->y` | **pixels**, level coordinates (not screen); y wraps from >200 to -20 | yes |
| `player_velocity` | average pixels per tick between consecutive observations | `player_velocity_source="derived"`; unavailable right after a reset or respawn | derived |
| `grounded` | `dave_on_ground()` in `dave.c` (the patch removes `static`) | only `BRICK` tiles count as ground | yes |
| `player_state` | `dave->state` (`dave.h` `DAVE_STATE_*`): standing, walking, jumping, climbing, freefalling, jetpacking, burning, dead, blinking | `burning` = hazard touched; input is ignored until death | yes |
| `facing` | `dave->face_direction`: FRONT→`front`, FRONTR/RIGHT→`right`, FRONTL/LEFT→`left` (`game_do_bullets` fires the same way) | `front` cannot fire | yes |
| `lives` | `game->lives` | starts at 4 | yes |
| `score` | `game->score` | | yes |
| `inventory` | `dave->has_trophy`, `has_gun`, `jetpack_bars` | keys `trophy`, `gun`, `jetpack_fuel` (0–900) | yes |
| `tiles` | `map[col*12+row].mod` for drawn tiles (`sprites[0] != 0`) inside the viewport | `BRICK`→solid, `FIRE` (fire, water, vines)→hazard, `LOOT`→collectible, `TROPHY`→required_item, `DOOR`→exit, `CLIMB` (trees, trunks, stars)→climbable, `GUN`/`JETPACK`→item. `MOSS` is decorative and omitted. | yes (viewport only) |
| `entities` | `monsters[i]` where `is_alive` (type taken from the sprite index range: 89 spider, 93 swirl, 97 sun, 101 bones, 105 ufo, 109 guard), their `plasma`, and `bullet` | pixels; velocity derived from the previous observation | viewport only |
| `region` | `scroll_offset` .. `scroll_offset+19` columns, rows 0–11 | tiles | yes |
| `jump_tick` | `dave->jump_state` while jumping | ticks into the arc (0..94) | when jumping |
| `jump_cooldown` | `dave->jump_cooldown_count` (bridge field `jump_cooldown`, added 2026-10-04) | ticks before Up starts a jump; 5 after a landing. A standing Dave takes off `max(0, n - 1)` ticks later, plus the still start tick | yes (None from older bridges) |
| `terminal` | `G_STATE_GAMEOVER` → `game_over`; `G_STATE_WARP_START` with `WARP_RIGHT` → `level_complete`, with `WARP_DOWN` → `secret_exit` (level 5) | an episode is one level | yes |

Raw fields available through `DaveBridgeAdapter.raw_state()` for debugging only. These are **not** model input until reviewed:

- `on_fire`, `on_tree`, `jump_state`;
- `scrolling` (the last tick only scrolled the view; Dave was not ticked);
- `auto_ticks` (release ticks after reset or respawn).

Unknown or unverified items:

- monster contact damage is binary (any touch burns Dave), so there are no damage values;
- the death cause comes from contact at the first burning frame (`threats.contact_cause`): an entity type, `hazard`, or `unknown`; the bridge does not report what set `on_fire`.
- walking speed is 2 px per 3 held ticks. All Phase 3 calibration numbers are in `docs/skills.md`.
- the game's own debug `printf`s (for example `monster.c` "ROUTE RESET …") share the bridge's stdout. The adapter skips any non-JSON line and keeps the last 100 in `DaveBridgeAdapter.game_stdout`.
