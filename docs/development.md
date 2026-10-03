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
| `uv run dave-agent play --arm A --mock` | One offline fixture episode for an arm |
| `scripts\setup_dave.bat` | Clone, patch and build deadly-dave plus the bridge (needs git and VS Build Tools 2022) |
| `uv run python scripts/probe_environment.py` | Real-game acceptance checks; screenshots go to `artifacts/probe/` |
| `uv run python scripts/calibrate_skills.py` | Real-game skill calibration; checks the measurements against `configs/skills.yaml` and saves `artifacts/calibration/skills.json` |
| `uv run dave-agent probe --adapter dave --scenario level1` | Real-game reset → step probe |
| `uv run pytest -m "not dave"` | Skip the real-game tests |
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
- `configs/experiments.yaml` is the entry point. Its sibling files `environment.yaml`, `models.yaml` and `skills.yaml` are merged in, and each top-level key may appear in only one file.
- Secrets live in `.env`; `.env.example` lists the variable names.

## External game source

deadly-dave is cloned to `external/deadly-dave/`, which is gitignored and never committed. Our changes to it live only in `bridge/deadly-dave-bridge.patch`. To change the bridge:

1. Edit the files in `external/deadly-dave/`.
2. Rebuild with `scripts\build_dave.bat` (from the repo root).
3. Regenerate the patch: `cd external/deadly-dave && git add -N bridge.c && git diff > ../../bridge/deadly-dave-bridge.patch`.

Bridge tests (`-m dave`) skip automatically when `deadly-dave-bridge.exe` is missing.
