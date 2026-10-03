# Repository Guidelines

## Project Structure & Module Organization

This is a small Python project targeting Python 3.13 or newer. The current entry point is `main.py`, which contains the runnable `main()` function. Project metadata and dependencies live in `pyproject.toml` (locked in `uv.lock`); the harness package is `src/dave_agent/`. `README.md` is present but currently empty. Exploratory work can be found in `test.ipynb`; keep notebook experiments separate from production code until they are ready to be moved into Python modules. Media and other non-code inputs belong under `assets/`, such as `assets/sample.mkv`.

## Build, Test, and Development Commands

- The virtual environment is `jev/`, managed by uv: set `UV_PROJECT_ENVIRONMENT=jev`, then `uv sync` installs dependencies.
- `uv run pytest` runs the offline test suite; `uv run dave-agent --help` lists CLI commands.
- `uv sync` installs the project itself in editable mode.

No lint or type-check tooling is configured yet. Add it to `pyproject.toml` before documenting it here.

## Coding Style & Naming Conventions

Use standard Python style: 4-space indentation, clear function names in `snake_case`, classes in `PascalCase`, and constants in `UPPER_SNAKE_CASE`. Prefer small functions with explicit inputs and outputs. Keep environment-specific values out of source files; load secrets and local configuration from `.env` via `python-dotenv` when needed. Avoid committing generated outputs, large temporary files, or notebook execution noise unless they are intentionally part of the project.

## Testing Guidelines

Tests live in `tests/` (`unit/`, `integration/`, `fixtures/`) and follow `pytest` conventions: files named `test_*.py`, test functions named `test_*`, and focused fixtures for shared setup. Tests that call paid APIs must be marked `live` and stay out of the default offline run. Cover behavior in Python modules before relying on notebook checks.

## Commit & Pull Request Guidelines

This repository has no commit history yet, so no project-specific convention is established. Start with short, imperative commit messages such as `Add OpenRouter client setup` or `Document local development commands`. Pull requests should include a concise summary, testing notes, linked issues if applicable, and screenshots or sample outputs when user-visible behavior changes.

## Security & Configuration Tips

Do not commit API keys, tokens, or `.env` files. Document required environment variables in `README.md` and provide safe examples, such as placeholder values, when configuration becomes necessary.
