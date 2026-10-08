# Live viewer

Watch an agent play while it decides.
- **Choose:** a level, an arm and the models in the browser.
- **Left:** the real game running.
- **Right:** every tactical decision as it is made: which skill Jev or the LLM chose and with what probability, what the threat screen removed and why, and what the skill then did.
- **Bottom:** the strategic planner's view: the explored map it read, the goal it set, the waypoints it gave the engine, and its reason.

Two local processes:

```powershell
$env:UV_PROJECT_ENVIRONMENT="jev"
uv run dave-agent live                      # http://127.0.0.1:8765 (free: mock models only)
cd frontend; npm run dev                    # then open http://localhost:3000/live
```

## Server: `dave-agent live` (`src/dave_agent/runner/live.py`)

It runs one episode at a time, exactly as `play` runs it (`run_trial`): the same models, goal manager, threat screen, episode store and graph store. A live run can be inspected afterwards with `dave-agent inspect`. Only two things are added, and neither changes a decision:
- **`LiveAdapter`** paces the game at `--tick-ms` per tick (default 14 ms, the game's own speed). It saves a frame every `--frame-every` ticks (default 3), and on every model decision.
  - By default the game stays paused while a model decides, as in every `play` and benchmark run, so the page shows "… is choosing among N skills (game paused)". The **Pause game while models think** toggle turns this off for one run (below).
  - Frames come from the bridge's `screenshot` command: 320×200 with the HUD, headless, no window needed.
- **`run_episode(on_event=…)`** is a write-only hook. A failing viewer is logged and never stops the episode. It reports:

  | Event | When | Holds |
  | --- | --- | --- |
  | `episode` | start, end (`started` / `finished`; `stopped` from the server) | frame, level, tile, state, score, lives |
  | `plan` | every planning step | triggers, chosen goal, rationale, waypoints, route summary (with incidents), fallback, calls, candidate goals (with their `path`), the explored `map`, `platforms`, `tried` (attempts), `failed_links`, the estimated `path` and the level's `deaths` |
  | `goal` | a goal ends | status and reason |
  | `deciding` | a model is asked | the number of options, the screened skills |
  | `decision` | the choice | every offered skill with its description and `score` (graph arms: the live move score `{q, regret, best, p_ok, attempts, fatal, land, mode}`, `docs/graph.md`), `screened` (removed skills and reasons), the chosen id, forced/fallback, Jev's per-skill probabilities, call latency, `threats` (each visible threat's predicted path) |
  | `outcome` | the skill ended | outcome, reason, frames, end tile, events (death, pickup, …) |
  | `graph` | graph-enabled arms: once at the start (the stored graph), then after every skill | the current level's learned graph (`WorldGraph.view()`: platforms, edges with attempts, successes, fatal, `p`, mean frames; counts), `source` (`learning` or `frozen`), and `last`: what the skill added (`record_execution`'s result) and the edge it touched |

  Every call view (`calls` in `decision` and `plan`) carries `tokens` (`total_tokens`, None when unreported).

  The server adds `run` (settings), `notice` (the budget of a paid run), `summary` (the `play` summary), `error` and `idle`.

The hub keeps only the newest `graph` event (each is a full snapshot of 10–20 KB), so a reload replays one.

### Pause off: the game runs on while models think

Untick **Pause game while models think** (`POST /start` with `"pause": false`) to run that episode with `environment.execution_mode: real_time` (`run_episode(realtime=True)`; the run's recorded config says so):
- **Waiting:** planner and tactical calls run on a worker thread. Meanwhile the game keeps going at `--tick-ms` per tick with no keys pressed: Dave stands still while monsters and shots move. A slow model (Azure, about 2.5 s, which is about 180 ticks) loses far more game time than Jev (about 0.2 s).
- **When the choice arrives,** it is checked against the latest observation:
  - It is **dropped** if, during the wait, Dave died or respawned, the episode ended, the chosen skill is no longer legal, or the threat screen now removes it. A `decision_stale` event is logged, the calls are still recorded, the card shows "too late (…), deciding again", and the model is asked again on the current state.
  - **Otherwise** the chosen skill runs from the latest observation. A `decision_latency` event is logged, and the card shows "game ran N ticks meanwhile".
- **Waits after the planner:** events that happen during a planner wait (a death, say) reach the goal manager with the next skill's events.
- **Limits:**
  - These episodes depend on model latency. `dave-agent inspect` and `replay` refuse them.
  - `--tick-ms 0` refuses pause off, because unpaced idle ticks would run flat out.
  - `play` and `benchmark` stay paused (Phase 11 item 1, live viewer only so far).

**Routes** (127.0.0.1, CORS `*`):

| Route | Purpose |
| --- | --- |
| `GET /status` | idle or running, levels, arms, `allow_paid` |
| `POST /start` | `{scenario, arm, planner: mock\|live, tactical: mock\|live, seed?, pause?}` (`pause` defaults to true); 400 with a message when refused |
| `POST /stop` | stop at the next tick; the episode is recorded as `truncated` / `interrupted` |
| `GET /events` | Server-Sent Events. The current run's events are replayed first, so a reload, or a reconnect with `Last-Event-ID`, catches up. |
| `GET /frame` | the latest frame (BMP), 204 before the first |

**Options:**
- `--config`: default `configs/watch.yaml`, with an 18,000-frame budget per episode.
- `--store`: default `live.sqlite` next to `memory.episode_store`.
- `--adapter`: default `dave`.
- `--port`: default 8765.
- `--allow-paid`: live models (Azure planner, Azure or Jev tactical) are paid calls. They are refused unless the server was started with this flag.
- `--tick-ms 0` runs as fast as the game steps.

**Graph arm C** reads and updates its own store next to the live episode store. By default that is `artifacts/graphs/arm-C/dave/`, the same store `play` uses. With a custom `--store`, pass the matching `--graph` to `inspect`.

## Page: `frontend/app/live/page.tsx`

- **Server address:** `NEXT_PUBLIC_LIVE_URL`, default `http://127.0.0.1:8765`.
- **Code:** `lib/live.ts` holds the event types, the `useLive` EventSource reducer and the start/stop calls. The components are in `components/live/`.

| Component | Shows |
| --- | --- |
| `GameView` | the frame, about 20 per second, scaled with `image-rendering: pixelated`; score, lives, deaths; the "choosing" overlay |
| `DecisionFeed` | newest first: decision cards (options sorted by Jev probability with `ProbabilityBar`, `danger:` notes, a `route` badge on the options that make the planned route's next move, the live graph score on each option (`best`, or `+regret` worse than the best; hover for the cost and the tries), "graph's best" / "+x vs graph's best" on the card, "on route" / "off route" on the card, removed skills struck through with their reason, the outcome), planner cards (goal, triggers, waypoints, rationale), goal and error notes |
| `PlannerPanel` + `LevelMap` | the latest plan: the explored map (unseen dimmed, the screen outlined) with Dave now; the estimated moves Dave → waypoints → goal (walks as lines, jumps as arcs, falls as drops, a leg with no known way red and dashed); the planner's waypoints and the one the engine is heading for; reachable platforms (green outline), moves that failed this level (red arrows from take-off to landing, thicker once marked avoid), deaths (✕) and the latest decision's threat forecast: each shot's path until a wall (orange arrows; dashed with a number for a shot not fired yet, the number being the ticks until it is) and each monster's route (purple dashes; monsters fly through walls). Jumps are drawn along their simulated flight from the take-off the estimate found. Beside it: goal, reason, triggers, waypoints, the path estimate, the learned route and its past deaths, what was tried on this level, the failed moves, the candidate goals |
| `StatsPanel` | this run's tally: tactical decisions by the model (Jev, or the LLM on arm A), forced (one legal skill, no call) and fallback, with fallback reasons and the mean probability of Jev's choice; planner plans, calls, fallbacks and triggers; latency per call (median, mean, max); tokens and cost per role (a call without a reported cost is counted as unknown, never $0); skill outcomes and the most chosen skills; and **LLM vs Jev** (`ModelCompare`): bar charts of calls (planner + tactical), tokens (output solid, input light; Azure's completion tokens include reasoning) and cost (the LLM estimated from `models.*.price`, Jev as reported, calls with no cost listed under the chart, not added), and donuts of each model's share of the three. *This run* or *All runs* since the page opened (kept by run id, so a reconnect does not count a run twice). Mock and rule calls are not counted. Computed in the browser from the `decision`, `plan` and `outcome` events; each call carries `input_tokens` and `output_tokens` |
| `GraphPanel` | arm C's learned graph of the current level, live: platforms drawn where they are (filled once Dave stood on them, dashed where they run past the screen, ✕ for deaths, dots for items, Dave's platform outlined), each learned move an arrow coloured by how it went (green p ≥ 0.75, amber mixed, red once fatal), thicker the more it was tried, the newest one pulsing; hover for details. Beside it: counts, what the last skill added, and the riskiest moves with "led to goal" (past goals for the active goal's target that used the move from its platform, reached/tried; the platform tooltip lists all its goal credit). Arms A and B get a note |

Mock models give no probabilities or waypoints. The rule planner never gives waypoints; the Azure planner does, especially when Dave is stuck.

## Tests

`tests/unit/test_live.py` covers:
- the hook reports each step in order and changes no decision;
- a failing viewer is harmless;
- the hub replays the current run;
- paid runs need `--allow-paid`, and runs do not overlap;
- Stop records an interrupted episode;
- a graph-arm run is summarised;
- the HTTP routes and the SSE stream.

`tests/unit/test_realtime.py` covers pause off:
- the game advances with no keys while a slow model decides;
- paused mode takes no idle ticks;
- a late choice is stale after a death or when its skill is no longer legal;
- the toggle is refused at `--tick-ms 0`;
- inspect refuses a real-time run.

The page was checked on the real game in headless Chrome (level 2, arm C, mock).
