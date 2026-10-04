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
  - The game stays paused while a model decides, as in every run, so the page shows "… is choosing among N skills (game paused)".
  - Frames come from the bridge's `screenshot` command: 320×200 with the HUD, headless, no window needed.
- **`run_episode(on_event=…)`** is a write-only hook. A failing viewer is logged and never stops the episode. It reports:

  | Event | When | Holds |
  | --- | --- | --- |
  | `episode` | start, end (`started` / `finished`; `stopped` from the server) | frame, level, tile, state, score, lives |
  | `plan` | every planning step | triggers, chosen goal, rationale, waypoints, route summary (with incidents), fallback, calls, candidate goals (with their `path`), the explored `map`, `platforms`, `tried` (attempts), `failed_links`, the estimated `path` and the level's `deaths` |
  | `goal` | a goal ends | status and reason |
  | `deciding` | a model is asked | the number of options, the screened skills |
  | `decision` | the choice | every offered skill with its description, `screened` (removed skills and reasons), the chosen id, forced/fallback, Jev's per-skill probabilities, call latency, `threats` (each visible threat's predicted path) |
  | `outcome` | the skill ended | outcome, reason, frames, end tile, events (death, pickup, …) |

  The server adds `run` (settings), `notice` (the budget of a paid run), `summary` (the `play` summary), `error` and `idle`.

**Routes** (127.0.0.1, CORS `*`):

| Route | Purpose |
| --- | --- |
| `GET /status` | idle or running, levels, arms, `allow_paid` |
| `POST /start` | `{scenario, arm, planner: mock\|live, tactical: mock\|live, seed?}`; 400 with a message when refused |
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
| `DecisionFeed` | newest first: decision cards (options sorted by Jev probability with `ProbabilityBar`, `danger:` notes, removed skills struck through with their reason, the outcome), planner cards (goal, triggers, waypoints, rationale), goal and error notes |
| `PlannerPanel` + `LevelMap` | the latest plan: the explored map (unseen dimmed, the screen outlined) with Dave now; the estimated moves Dave → waypoints → goal (walks as lines, jumps as arcs, falls as drops, a leg with no known way red and dashed); the planner's waypoints and the one the engine is heading for; reachable platforms (green outline), moves that failed this level (red dashes), deaths (✕) and each visible threat's predicted path (orange dashes, from the latest decision). Beside it: goal, reason, triggers, waypoints, the path estimate, the learned route and its past deaths, what was tried on this level, the failed moves, the candidate goals |

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

The page was checked on the real game in headless Chrome (level 2, arm C, mock).
