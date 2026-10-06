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
| Experience | `experience(tile)`: per skill, what starting it from that tile did **this episode**: attempts, deaths (a death event, or `hazard_contact`), `burned`, `no_move` (`moved_px == [0, 0]`) and the last end tile. It is not windowed and survives respawns, so after a death Dave's next visit to the tile shows what killed him. `reset` clears it. It reaches the models only as candidate notes (see "Experience notes"). |

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

### Experience notes

Before every model decision (`control/experience.py`, called from `run_episode`), each offered candidate that was tried before gets notes appended to its description. The notes come before the reach estimate:
- **every arm:** `this episode from here: 2x, 2 died (burned), last end [3,9]` (working-memory experience for Dave's tile);
- **graph-enabled arms:** `past runs from this platform: 5x, 3 ok, 2 fatal, lands row 7 cols 4-9` (`WorldGraph.skill_evidence`). It is read from a copy of the graph taken at episode start, so this episode is never counted twice. A frozen warm checkpoint gives the same notes in every trial.

- **graph-enabled arms:** `past goals like this one from this platform: 3/4 reached (9/12 tries closer)`: the learned goal credit toward the active goal's target (below).

Rules:
- An untried skill is unchanged. Descriptions are capped at `DESCRIPTION_MAX` (320 characters, `schemas.py`; 200 before the credit notes).
- Notes only inform: nothing is masked, and the controller still chooses.
- The offered set and its digest are computed before the notes, so replay and parity checks are unaffected.
- Mock controllers ignore descriptions, so the offline traces are unchanged.

Why: Jev answers the same request the same way. Without the notes, Dave returning to a tile after a respawn saw exactly the request that led to his death, and repeated the fatal move.

Limits: the 120-frame window is fixture-scale. A Dave jump takes 94 frames, so on Dave the window holds only a few entries. Tune it with the planner (Phase 6).

### Goal credit

The learned graph scores a move a success when Dave lands alive on another platform, so a jump back and forth between two platforms looks perfect even when it leads nowhere. In a live level 3 run, `jump_right` c2→c6 showed 37/37 and the jump back 29/29, and the gun was never taken. Goal credit measures progress toward the goal instead (`control/credit.py`):

- **Each skill is scored** after it runs (`GoalManager._credit_step`). It compares the reach estimate's remaining cost to the goal's target (`ReachMap.cost`; for explore goals, to the explored map's edge that way) from the standing cell where the skill started (working memory's newest entry) and from the cell where it ended. The result is `closer`, `farther`, a `loop` (back on a cell already visited while going for this target) or `died`. Skills started in the air and level changes are not scored.
- **This episode, every arm:** `GoalCredit` keeps the counts per target and per (cell, skill). It survives respawns and is shown on the options as `for this goal from here: 6x, never closer, 5x back where Dave had already been`.
- **Across runs, graph-enabled arms:** when a goal ends (achieved, failed, expired or replaced), its steps go to the learned graph in `PlanningStep.credit`. `run_episode` records them only while the graph is learning (`WorldGraph.record_credit`), so a frozen warm checkpoint is never changed. Each start platform gets `credit["<skill>|<target_ref>"] = {tries, closer, goals, reached}`. The past-run note above reads it, and the learned route search weighs moves with it (`docs/graph.md`).

Notes and route costs only: nothing is masked, and Jev still chooses. The A, B and C requests stay identical apart from the graph arm's past-run notes and routes.

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
