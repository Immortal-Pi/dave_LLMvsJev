# Strategic planner and goal manager

| File | Contents |
| --- | --- |
| `src/dave_agent/control/goals.py` | `TargetMemory`, `goal_candidates`, `evaluate` (goal lifecycle), `GoalManager` (triggers, debounce, cap, fallback, graph routes, planner waypoints, the threat screen) |
| `src/dave_agent/control/level_map.py` | `render`: the explored level map the planner reads |
| `src/dave_agent/models/planner.py` | `GoalCandidate`, `PlanningRequest`, `PlanChoice`, `parse_plan`, the `StrategicPlanner` protocol, `RuleMockPlanner`, `ScriptedPlanner` |
| `src/dave_agent/models/azure.py` | `AzureChatClient` (httpx, transport retries) and `AzurePlanner` (system prompt, strict JSON schema) |

The planner chooses **what** to do next. The tactical controller (mock offline, live LLM for arm A, live Jev for arms B and C; see `docs/tactical.md`) chooses **how**, one skill at a time. Every arm runs the same goal-manager code with the same `planning:` settings. Graph-enabled arms differ in one way only: Python adds learned-route summaries to the candidates and turns the chosen goal into a route waypoint. A test checks this.

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
| `death` | soft, latched | `death` event. A death also **ends the active goal** (`goal_failed`, reason `death`), because Dave respawns at the level start. That hard trigger replans at the respawn, inside any debounce window, so no stale goal or waypoint survives a death. |
| `stuck` | soft | `Progress.stuck`: no progress for `planning.no_progress_frames` (a new goal restarts the clock) |
| `repeated_failures` | soft | `planning.repeated_skill_failures` consecutive failed or interrupted skills **since the last plan** |
| `inventory_changed` | soft, latched | the set of held items changed (not fuel draining or score) |
| `route_invalidated` | soft, latched | graph arms: a route that had been found became unreachable |

- **Hard triggers** plan immediately, within the call cap.
- **Soft triggers** wait until `planning.min_frames_between_calls` (60) frames after the previous call. Latched triggers survive the wait.
- **No planning while Dave is `burning` or `dead`.** Input is ignored then. Triggers stay latched until the respawn.
- **Stable execution makes no calls.** A plan clears the latched triggers, resets the failure count and restarts the no-progress clock (tested).

### Calls, retries and fallback

- **Validation:** `parse_plan` requires `{"goal": <offered id>, "rationale": str, "waypoints": [[col, row], ...]}`, where `waypoints` is optional with at most 5. The goal manager then checks every waypoint against the explored map (see "Map and waypoints").
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
- **Map:** the explored level map (`map`) and the planner's waypoints still ahead (`waypoints`). See "Map and waypoints".
- **Platforms and paths** (adapters with a reach envelope, i.e. Dave): `platforms`, each candidate's `path`. See "Platforms: the map as places to stand".
- **What was tried:** `attempts` and `failed_links` on this level this episode. See "Feedback: what already failed".
- **Graph arms only:** each candidate's `route` summary, with the deaths recorded on the route's platforms (`incidents`: cause, tile, skill; the newest 5).

Arms A and B send identical requests (tested on the fixture and on the real game).

## Map and waypoints

**The map is what a human player has seen.** `control/level_map.py` renders every cell observed on the current level this episode (the goal manager's cell memory, reset on a level change): the current screen plus every screen seen before. Parts of the level not yet scrolled into view are `?`. The full map in game memory is never used, so the `local_observed` policy holds.

```
   00000000001111111111
   01234567890123456789
06 #.##.....#...T#.####
07 #...@....#.#..#.....
10 ###XXXXXX#XXXX#XXXXX
```

- **Fields:**
  - `rows`: one string per map row, prefixed with its row number;
  - `col_ruler`: two lines giving each column's tens and units digits;
  - `origin`;
  - `screen_cols`: the part on screen now;
  - `legend`.
- **Drawing:** the tactical legend, with Dave `@`, visible monsters `M` and shots `*` drawn over it. The planner's waypoints still ahead are drawn as `1`–`5`.
- **The map is identical for every arm.**

**Waypoints.** The planner may add up to 5 intermediate waypoints to its choice, in order: platform ids (`"c8r4"`, preferred; resolved to the platform's cell nearest the goal) or standing tiles `[col, row]`. The system prompt asks for them when the path is not a direct walk or single jump, and always on `stuck` or `repeated_failures`, for example "climb the left ledges first, then go right along the top".
- **Validation, with the game's physics:**
  - each waypoint must be an explored empty cell directly above a `#`;
  - with a reach envelope, each must be reachable (`ReachMap.path`) from the one before, and the first from Dave's cell;
  - the goal must be reachable from the last waypoint whenever it is reachable from Dave (checked for item goals, not explore).
  - A problem is sent back as retry feedback (`invalid_output`) that names it and the options, e.g. `waypoint [16, 5] is not reachable from [10, 4] (a wall, a gap or too high); from c8r4 Dave can reach: c11r6 (fall right), c13r3 (jump right), …`, or `… the goal is reachable this way: c1r9 -jump right-> c4r7 …`.
  - When the retries run out, the goal stands with the valid waypoints before the first bad one.
- **Following:** the goal's `next_waypoint` is the next landing on the estimated path to the first planner waypoint (`_step_toward`), not the waypoint itself, ahead of the graph route and the reach estimate. A far waypoint given to the tactical model directly sent it straight at it (level 2: after waypoint (34,4), "heading to (47,2)" walked Dave off the ledge instead of jumping up to the corridor). Without a reach envelope it is the waypoint itself. When Dave stands on a waypoint's row within 1 column, it and any before it are dropped, and the next one takes over. When none are left, the route or reach waypoint resumes.
- **Lifetime:** the queue clears when the goal ends (including on death or a level change). The no-progress clock measures distance to the current waypoint, so `stuck` follows the queue.
- **Record:** `goal_set` records the accepted waypoints, and the inspector replays them.

## Platforms: the map as places to stand

An LLM reads a character grid poorly, especially which column a wall is in (on level 2 the planner sent Dave along row 5 through two brick pillars). So, with a reach envelope, the request also holds the same explored cells as platforms (`control/platforms.py`, identical for every arm):

- **Platform:** a maximal run of standable cells on one row (row 0, above the level's top wall, is left out). Its id is `c<left col>r<row>`, e.g. `c8r4`.
- **`exits`:** the platforms one move away, from `ReachMap.moves` over each of its cells: `{"to": "c8r4", "by": "jump right", "from_col": 4}`, the cheapest per destination. A wall or a gap is a missing exit. A move that failed this level carries `note: "failed 2x this level"`.
- **`reachable` / `hops`:** a breadth-first search from Dave's platform.
- **`items`:** the candidate goals taken from it; **`open`:** an end next to unexplored cells; **`danger`:** `died here this episode 2x (fire)`, and on graph arms `died here in past runs …` from the learned graph's incidents.
- At most 40 are sent: reachable ones first (fewest hops), then the rest.
- **Explore candidates:** the unexplored target itself never has a path, so their `path` leads to the nearest reachable platform with an unexplored end that way (`... -> c16r5 (its right end is unexplored)`), and the engine heads there one landing at a time (`frontier`, `next_landing` in `control/reach.py`).
- **The rule planner** (mock and the deterministic fallback) ranks goals with a known `path` first, then by its fixed priority.
- **Each candidate's `path`:** the estimated chain, e.g. `c1r9 -jump right-> c4r7 -jump left-> c2r5 -jump right-> c4r3 -jump right-> c8r4 -jump right-> c13r8` (the level 2 trophy, the climb over the left ledges), or `no known path over the explored platforms; nearest reachable platform: c16r5`.

**Ledge-end jumps.** On the real game Dave stands overhanging a platform's end while one foot point is over a brick, and a jump from there carries further: on level 2, from x 72 on the one-tile ledge at (4,3) `jump_right` lands at (8,4), but from mid-cell it falls short. `ReachMap.moves` therefore also launches jumps from `edge_x`, the furthest standing pixel, at a platform's true end (no floor in the next cell). Without it, the estimate found no way from the level 2 start area to the trophy.

**Estimated end tiles** on tactical candidates come from Dave's actual pixel position (`estimate_end_at`, the threat screen's `trace_skill`), no longer from his cell: on the real 37-skill level 2 route it is right for 28 of 34 standing starts, against 18 for the cell-based estimate (`move_right_1` at a ledge end stays on the overhang instead of "falling").

## Feedback: what already failed

The planner is stateless between calls. `control/attempts.py` keeps, for the current level of this episode (reset on a level change), identical for every arm:

- **`attempts`:** the last 6 goals: goal, waypoints, `outcome` (`achieved`, `failed`, `expired`, `replaced`, or `active` for the current one), `reason` (`death`, `deadline`, `replanned: stuck`, …), `waypoints_reached` (`1/3`), `furthest` tile and `frames`.
- **`failed_links`:** (from cell → to cell) moves that failed: the first jump or fall of the estimated path toward the waypoint Dave was heading for, recorded when the planner is called on `stuck` or `repeated_failures`, or when Dave dies of fire, water or an unknown cause on it (a shot or a monster is not the move's fault). Only estimated moves are recorded, never a line from Dave to a target with no known path, and a move is cleared once Dave makes it (standing on its launch cell, then on its landing). `times`, `how`, and `avoid` once it failed twice.
- **Cost, not a ban:** each failure adds `FAILED_MOVE_COST` (6, about three jumps) to that move in the reach estimate, so the engine's own route and the planner's `path`s go another way when there is one, but still use a move that is the only way (a weak executor fails right moves too: with hard bans, mock runs on level 2 lost the only route to the trophy).
- **`deaths`:** cause and tile, shown as platform `danger` and on the viewer.

This is per-episode memory, not the learned graph: only graph arms remember across runs, through the graph (route summaries with `incidents`, and `died here in past runs` on platforms).

## Azure planner (`--planner live`)

- **Endpoint:** Chat Completions at `{AZURE_OPENAI_ENDPOINT}/openai/deployments/{AZURE_OPENAI_CHAT_DEPLOYMENT}/chat/completions?api-version={AZURE_OPENAI_API_VERSION}`, with an `api-key` header. Settings are checked before the game starts.
- **Prompt:** the system prompt states:
  - only the rules verified in `docs/feasibility.md` §3;
  - the planner's role;
  - how to read `platforms` (exits, reachability; walls are missing exits), candidate `path`s and the map;
  - that `attempts` and `failed_links` already failed, and must not be repeated;
  - when to give waypoints (platform ids preferred; each reachable from the one before);
  - the output format.

  It asks for a brief rationale, not step-by-step reasoning. The user message is the `PlanningRequest` as JSON.
- **Output:** `response_format` is a strict `json_schema`: `goal` is an enum of the offered ids, and `waypoints` is an array of platform ids or integer pairs (`anyOf`; empty when not needed). `reasoning_effort: low`, `max_completion_tokens: 2000`.
- **Transport retries:** timeouts, 408, 429 and 5xx are retried with exponential backoff up to `models.max_retries`. 4xx responses are not retried, and their messages are redacted.
- **Logging:** usage tokens (prompt, completion, reasoning) are recorded. `cost_usd` stays null, because no price table is assumed. The `request_ref` is a hash of the body, and the `response_ref` is the response id.
- **Verified 2026-10-03:** `gpt-5.4-mini`, api-version `2024-12-01-preview`. The strict schema and `reasoning_effort` are accepted, and a call takes about 1.3–1.6 s with about 600–1000 prompt tokens. The sanitized response is `tests/fixtures/azure/planner_response.json` (`scripts/probe_azure.py`).

## Graph-enabled arms: Python computes the route

- **Route search:** when a goal is set, the target tile is mapped to its segment on the current level's graph (`GraphStore.for_level(level).node_at`;  explore targets map to the cheapest reachable segment with an open side in that direction), and `find_route` runs from the segment Dave stands on.
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
- The `goal_set` payload records the triggers, candidate ids, choice, rationale, fallback, attempts, waypoint, route summary and timings, plus the estimated `path` (Dave → waypoints → goal as `[col, row, kind]`, kind `walk`/`fall`/`jump`, `unknown` for a leg with no known way) and the level's `deaths`.
- Replay is unaffected, since it re-executes the recorded skill choices.

## Known limitations

- `stuck` uses distance to the waypoint. A mock tactical controller ignores goals, so mock runs mostly show `stuck` and expiry triggers. The live LLM controller (`--tactical live`) receives the goal and waypoint.
- At fixture scale (41-frame episodes) the 60-frame debounce hides death triggers. Tests set it to 0. Calibrate all `planning:` values on Dave in Phase 9.
- A planner re-choosing the same target on a soft trigger restarts its deadline. The call cap bounds this.
- Explore targets are a direction and a column, not a verified reachable location.

## Reachability waypoints (every arm)

When the adapter has a `skills.reach` entry (Dave), the goal's `next_waypoint` is the **next landing spot** on an estimated route over the tiles observed this level (`control/reach.py`), not the far target. Graph-enabled arms keep their learned route whenever it has one. The waypoint is recomputed after every skill, and it is the target itself when no route is known or only walking is left. Each offered candidate's description also gets its estimated end tile, e.g. `estimated end tile [11, 7]`, or `(no movement)` / `estimated: no safe landing`. Every arm gets the same annotations, and the recorded candidate set and its digest are unchanged.

The estimate simulates the catalog's own jump shapes with measured movement: the 95-tick jump arc, 1 px/tick air control and fall, 2 px per 3 ticks walking, and the foot and body offsets. It reproduces every real-game landing in `test_reach.py`. On level 1 its route (trophy via (4,7), (6,7), (9,5), (11,3); door via (16,7) and the gap at (17,9)) completes the level in the real game.
