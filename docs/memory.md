# Memory: working memory and the episode store

The code is in `src/dave_agent/memory/`:

| File | Contents |
| --- | --- |
| `working.py` | `WorkingMemory` and `MemoryContext` |
| `detector.py` | `EventDetector` |
| `episodes.py` | `EpisodeStore` and `EpisodeRecorder` |

Replay lives in `src/dave_agent/runner/replay.py`. Learned graph memory is Phase 5 and is not covered here.

## Working memory (in-process, per episode)

`WorkingMemory` is a typed Python object built only from observations and executions, with no database access. The runner calls:
- `reset(observation)` at every episode start;
- `record(decision, execution)` after every skill (the episode recorder's `record` also takes every model call made for the decision, retries included; see `docs/tactical.md`);
- `context()` before every model call.

The goal manager (`docs/planner.md`) calls `set_goal(goal)` for a new goal, which restarts the no-progress clock. It calls `set_goal(goal, restart_clock=False)` when only the route waypoint moves.

Every arm builds the identical memory, which `tests/unit/test_arm_parity.py` asserts.

| Part | What it holds |
| --- | --- |
| Current state | `latest` observation, `goal` (and `goal.next_waypoint`), `last_decision`. The current skill status is `context().last`. |
| Recent history | A deque of `HistoryEntry`, one per executed skill. Each entry holds: start/end frame and observation ids, candidate, skill, forced, outcome, reason, start/end position, end state, and the event types during the skill. Entries older than `memory.recent_history_frames` before the latest frame are dropped, and the deque never exceeds `memory.max_history_entries`. |
| Derived motion | `motion()`: the pixel displacement from the window start, or from the last death or respawn, to now, plus the adapter's `player_velocity` |
| Progress | `progress()`: see the next table |

| `progress()` field | Meaning |
| --- | --- |
| `no_progress_frames` | Frames since the last progress. Progress means one of three things: the score or inventory changed; with a waypoint, the best tile distance to it improved; without a waypoint, Dave entered a tile not visited before in this episode. Respawning onto the start tile is not progress. |
| `stuck` | `no_progress_frames ≥ planning.no_progress_frames` (180) |
| `consecutive_failures` | Consecutive skills that ended `failed` or `interrupted` |
| `repeated_failures` | `consecutive_failures ≥ planning.repeated_skill_failures` (3) |
| `skill_repeats` | Consecutive runs of the latest skill within the window |
| `tile_revisits` | Earlier window entries that ended on the current tile |
| `tiles_visited` | Size of the episode's visited-tile set, which is bounded by the level size |

- Progress is checked on **every frame** of a skill, so passing through a new tile mid-jump counts.
- The player's tile is the tile under the centre of the 16 px sprite box: `((x+8)//16, (y+8)//16)`.

**`MemoryContext`** is the only memory a controller receives. It is passed as `decide(observation, goal, candidates, memory)`.
- It is deterministic and bounded: the latest entry, motion, progress and the last `memory.context_entries` (8) entries.
- It never contains a transcript or anything from the episode store.
- Mock controllers ignore it. The live LLM and Jev models get it through the shared tactical request (`docs/tactical.md`).

Limits: the 120-frame window is fixture-scale. A Dave jump takes 94 frames, so on Dave the window holds only a few entries. Tune it with the planner (Phase 6).

## Derived events (`EventDetector`)

These are computed per frame from consecutive observations, with `certainty="observed"`:
- **`inventory_changed`:** payload `{"changes": {key: [old, new]}}`. A score-only pickup such as a gem gives `item_collected` but no inventory change.
- **`area_discovered`:** the view contains map columns not seen before in this episode. Payload: `level_id`, `min_col`, `max_col`, `new_cols`.

The goal manager emits `goal_set`, `goal_achieved` and `goal_failed` (`certainty="derived"`); expiry is `goal_failed` with `status=expired`. See `docs/planner.md`.

**Death causes are never inferred.**
- Dave deaths are logged with `{"cause": "unknown"}`, because the bridge does not report what ignited Dave. A nearby enemy is not evidence.
- Fixture deaths carry the exact cause defined by the fixture rules.

## Episode store (SQLite)

The store lives at `memory.episode_store` (default `artifacts/events.sqlite`, gitignored). It uses only the standard-library `sqlite3`, with `PRAGMA foreign_keys = ON`. The schema version is held in `PRAGMA user_version` (currently 2; version 2 added `decisions.context_digest` and `model_calls.output_json`):
- a store with any other version is refused with an actionable error;
- so is a non-store SQLite file.

| Table | Key | Links |
| --- | --- | --- |
| `runs` | `run_id` | `mode` (`mock` / `live`), command, full config JSON |
| `episodes` | `episode_key` = `run_id/episode_id` | → runs. It holds arm, controller label, adapter, build, scenario, seed, outcome, `termination_reason`, frames, score, lives, deaths, decisions and model calls. `outcome` stays NULL if the process dies mid-episode. |
| `observations` | (episode, `observation_id`) | the full `Observation` JSON. Only decision-point observations (the start and end of each skill) are stored. |
| `model_calls` | (episode, seq) | provider, model, purpose, status, latency, retries, usage, cost and cost source, sanitized request/response refs, and `output` (provider answer details as reported, e.g. Jev probabilities and confidence; NULL otherwise) |
| `decisions` | (episode, seq) | → the observation it was made on, and → its model call (NULL when forced). It also holds the offered candidate ids, the mask, the candidate digest and the `context_digest` of the tactical request (NULL when forced). |
| `skill_executions` | (episode, decision seq) | → its decision, and → its start and end observations. It holds outcome, reason, frames and input ticks. |
| `events` | (episode, seq) | every adapter, executor and derived event in order, → the skill execution it occurred during (NULL for episode-level events) |

- **Termination reasons:** `terminal:level_complete|game_over|secret_exit`, `max_frames:<n>`, or `error:<Type>: <message>`. On an error the evidence logged so far is kept, then the exception is re-raised.
- **Model failures:** a call whose status is not `ok` also emits a `model_failure` event.
- **Write-only from the loop:** `EpisodeRecorder` buffers rows and writes them at decision boundaries, never inside the frame loop. It writes in one transaction, parents first, whenever `memory.store_batch_size` rows have accumulated, and at episode end. `record_planning(calls, events)` stores planner calls (`model_calls.purpose = planner`) and goal events at decision boundaries; they have no `decision_seq`. The loop never reads the store, so logging gives no arm extra planning context. `test_episode_logging_identical_across_arms` shows that two arms' logs are identical except for labels and timestamps.

## JSONL export and replay

```bash
uv run dave-agent play --arm A --mock --run-id demo               # logs to memory.episode_store
uv run dave-agent export --run-id demo --out artifacts/demo.jsonl # one JSON record per line
uv run dave-agent replay --jsonl artifacts/demo.jsonl             # exit 0 when every episode reproduces
```

- The export writes, in order:
  - the `run` record;
  - for each episode: its `episode` row, then `observation`, `model_call`, `decision`, `skill_execution` and `event` records.

  Each record carries a `record` tag, and its JSON columns are decoded.
- Replay needs no controller. It resets the adapter to the recorded scenario and seed, then re-executes each recorded `candidate_id`. For each decision it checks the offered candidates and digest, the skill outcome and reason, and the end observation, comparing everything except the per-process `observation_id` and `episode_id`.
- Replay uses the **current** skill catalog, so a catalog change shows up as a mismatch.
