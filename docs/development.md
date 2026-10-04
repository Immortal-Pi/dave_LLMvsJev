# Development Environment

## Virtual environment (uv)

- The venv is **`jev/`** (Python 3.13.5), managed by **uv** 0.7.13. Point uv at it before any `uv` command:
  - Git Bash: `export UV_PROJECT_ENVIRONMENT=jev`
  - PowerShell: `$env:UV_PROJECT_ENVIRONMENT="jev"`

  Without this, `uv sync` would create a separate `.venv/`.
- `python` is not on PATH on this machine. Use `uv run ...` or `jev/Scripts/python.exe`. Activating `jev\Scripts\activate.bat` only affects the shell where you run it.

## Dependencies

- `pyproject.toml` declares the dependencies, and `uv.lock` pins them (commit both).
  - Runtime: pydantic, pyyaml, httpx, python-dotenv.
  - Groups: `dev` (pytest) and `notebook` (ipykernel, openrouter, requests for `test.ipynb`). Both install by default.
- `uv sync` installs everything, including the `dave_agent` package in editable mode.
- `requirements.txt` is just a pointer kept for compatibility. Do not add packages there.

## Commands

| Command | Purpose |
| --- | --- |
| `uv run pytest` | Offline test suite; no credentials needed |
| `uv run pytest tests/unit/test_fixture_adapter.py::test_snapshot_round_trip` | Run a single test |
| `uv run dave-agent probe --adapter fixture` | reset → step → changed observation |
| `uv run dave-agent play --arm A` | One offline fixture episode for an arm (the default; `--mock` says so explicitly), logged to `artifacts/events.sqlite` (`--store`, `--run-id`) |
| `uv run dave-agent export --run-id RUN --out FILE.jsonl` | Export the episode store, or one run, as JSONL |
| `uv run dave-agent replay --jsonl FILE.jsonl` | Re-run exported episodes and verify the recorded evidence |
| `uv run dave-agent play --arm C --mock [--graph PATH]` | Graph-enabled arm: learns into `artifacts/graphs/arm-C/<adapter>/`, one `<level>.json` per level (a legacy `<adapter>.json` is split on load) |
| `uv run dave-agent play --arm A --mock --planner live [--adapter dave --scenario level1]` | **Live, paid**: Azure OpenAI strategic planner with the mock tactical controller (run mode `live-planner`) |
| `uv run dave-agent play --arm A --tactical live [--planner live] --adapter dave --scenario level1` | **Live, paid**: the arm's tactical model: Azure OpenAI for arm A, Jev for arms B and C (`--arm B` / `--arm C`). Run mode `live-tactical`, or `live` with the live planner. Prints the budget to stderr before any call |
| `uv run dave-agent probe-provider --provider azure [--purpose tactical]` | **Live, paid**: one planner (default) or tactical call; prints status, latency, usage and the validated choice |
| `uv run dave-agent play --arm B [--planner live --tactical live] --adapter dave --scenario level1 --watch --config configs/watch.yaml` | Watch a whole level: `configs/watch.yaml` is `experiments.yaml` with an 18000-frame cap, a 3600 s wall-time budget and its own store (`artifacts/watch.sqlite`). For watching and demos, not benchmark results |
| `uv run dave-agent probe-provider --provider jev [--save-fixture]` | **Live, paid**: one Jev tactical call; prints the choice, probabilities, confidence, usage and cost. `--save-fixture` refreshes `tests/fixtures/jev/tactical_response.json` |
| `uv run python scripts/probe_azure.py` | **Live, paid**: one planner call; refreshes `tests/fixtures/azure/planner_response.json` |
| `uv run dave-agent graph --checkpoint PATH [--yaml OUT] [--route FROM TO --items trophy]` | Inspect a graph store (per-level counts), export YAML, search a route on the level named by the node ids |
| `scripts\setup_dave.bat` | Clone, patch and build deadly-dave plus the bridge (needs git and VS Build Tools 2022) |
| `uv run python scripts/probe_environment.py` | Real-game acceptance checks; screenshots go to `artifacts/probe/` |
| `uv run python scripts/try_skills.py [--scenario level1] SKILL ... [--extra-skills trial.yaml] [--watch]` | Free: run a fixed skill sequence on the real game and print each skill's start and end tile, outcome, score and inventory. Feasibility checks, and measuring trial skills before adding them |
| `uv run python scripts/calibrate_skills.py` | Real-game skill calibration; checks the measurements against `configs/skills.yaml` and saves `artifacts/calibration/skills.json` |
| `uv run dave-agent probe --adapter dave --scenario level1` | Real-game reset → step probe |
| `uv run pytest -m "not dave"` | Skip the real-game tests |
| `uv run dave-agent benchmark --arms A,B,C --trials 3` | Offline mock benchmark on the fixture: paired trials, `manifest.json`, `episodes.jsonl`, `summary.json`/`.csv`, `pairs.csv` and `events.sqlite` in `artifacts/benchmarks/<id>/` (`--out`, `--id`). See `docs/benchmark.md` |
| `uv run dave-agent benchmark --config configs/benchmark_dave.yaml --adapter dave --scenarios level1 --arms A,B,C --trials 3` | The same on Dave with Dave-scale settings. Add `--planner live --tactical live` for a **paid** run (needs `models.*.price` for Azure or `--allow-unpriced`) |
| `uv run dave-agent train-memory --arm C --episodes N --out CKPT [--adapter dave --scenarios level1]` | Build a warm graph checkpoint; then `benchmark --memory-regime warm --checkpoint C=CKPT` |
| `uv run dave-agent summarize --benchmark DIR` | Rebuild a benchmark's summaries from its `episodes.jsonl` |
| `uv run dave-agent inspect --store STORE --run-id RUN [--decisions 90-95] [--no-outcomes] [--graph PATH\|empty]` | Rebuild exactly what the models saw at each decision (checked against the recorded digests) into `artifacts/inspect/RUN/`. Add `--ask jev --ask azure` (with `--decisions`) for **paid** live answers. See `docs/inspector.md` |
| `cd frontend && npm install && npm run dev` | The local, read-only viewer for inspection bundles (http://localhost:3000; `INSPECT_DIR` overrides `../artifacts/inspect`). `npm run lint`, `npm run build` check it |
| `uv run python scripts/search_route.py --scenario level2 [--max-states N] [--key tile\|px] [--extra-skills Y]` | Free, offline: breadth-first search on the real game for a route made only of catalog skills (snapshot restore, fatal skills pruned). Prints the route or the frontier (reached tiles, fatal (tile, skill) pairs). See `docs/skills.md` |
| `uv run python scripts/skill_frames.py STORE ... [--adapter dave]` | Read-only: skill-duration distribution in episode stores (the evidence for `configs/benchmark_dave.yaml`) |
| `uv run python scripts/probe_jev.py` | **Live, paid**: one Jev decision; refreshes `tests/fixtures/jev/choice_response.json` |

## Watching the game

Add `--watch` to open the game in a window while the harness plays. It works with the dave adapter only. The window is for humans: models never receive pixels, and runs are identical with or without it.

```powershell
$env:UV_PROJECT_ENVIRONMENT="jev"
uv run dave-agent play --arm A --mock --adapter dave --scenario level1 --watch
uv run dave-agent play --arm A --mock --adapter dave --scenario level1 --watch --watch-delay 40   # slower
uv run python scripts/probe_environment.py --watch
```

`--watch-delay` is the pause in milliseconds after each tick: 14 is real speed, 0 is as fast as possible. Closing the window lets the run continue headless.

## Configuration

- `configs/skills.yaml` holds one skill catalog per adapter (`skills.catalogs.fixture` and `.dave`) plus executor settings. Re-run the calibration script after changing Dave durations.
- `graph:` in `configs/experiments.yaml` sets the route-cost weights and limits (see `docs/graph.md`).
- `planning:` sets the planner triggers, debounce, call cap, goal timeout and planner context size (see `docs/planner.md`).
- `tactical:` sets the per-episode tactical budgets, what happens when one runs out, and the fallback skills (see `docs/tactical.md`). `models.planner` and `models.tactical_llm` set `max_completion_tokens` and `reasoning_effort`.
- Live tests are marked `live` and skip unless `RUN_LIVE=1` (e.g. `RUN_LIVE=1 uv run pytest -m live`).
- `memory.*` in `configs/experiments.yaml` sets the working-memory limits and the episode store path and batch size (see `docs/memory.md`).
- `configs/experiments.yaml` is the entry point. Its sibling files `environment.yaml`, `models.yaml` and `skills.yaml` are merged in, and each top-level key may appear in only one file.
- `configs/benchmark_dave.yaml` is a full copy of `experiments.yaml` with Dave-scale frame settings (memory window, triggers, goal timeout, episode length) for benchmarks on Dave. `benchmark.seed`, `confidence` and `bootstrap_samples` set the trial seeds, the interval level and the bootstrap.
- `models.planner.price` and `models.tactical_llm.price` (default null) take your Azure prices (`input_per_mtok`, `output_per_mtok`, `source`, `as_of`); Azure costs are then recorded as estimates.
- Secrets live in `.env`; `.env.example` lists the variable names.

## External game source

deadly-dave is cloned to `external/deadly-dave/`, which is gitignored and never committed. Our changes to it live only in `bridge/deadly-dave-bridge.patch`. To change the bridge:

1. Edit the files in `external/deadly-dave/`.
2. Rebuild with `scripts\build_dave.bat` (from the repo root).
3. Regenerate the patch: `cd external/deadly-dave && git add -N bridge.c && git diff > ../../bridge/deadly-dave-bridge.patch`.

Bridge tests (`-m dave`) skip automatically when `deadly-dave-bridge.exe` is missing.
