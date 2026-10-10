# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

A Python research harness that plays *Dangerous Dave* (via the open-source [deadly-dave](https://github.com/skoperst/deadly-dave) reimplementation) from structured game state. It compares an Azure OpenAI LLM tactical controller with TypeSafe's **Jev** System One model (via OpenRouter), holding the planner, observations, memory and action interface constant. The phased spec is in `implementation/`; the code is in `src/dave_agent/` (CLI: `dave-agent`). `frontend/` is a local Next.js viewer for inspection bundles (`docs/inspector.md`). `test.ipynb` holds early Jev API experiments.

`AGENTS.md` sets the style, commit, and security conventions.

## Documentation

Detailed documentation lives in `docs/`. Read the relevant file before working in that area:

- [docs/progress.md](docs/progress.md): phase status, verification results, blockers and next steps. **Read this first.**
- [docs/architecture.md](docs/architecture.md): the control loop and the role of each `dave_agent` module.
- [docs/feasibility.md](docs/feasibility.md): game edition, verified mechanics, bridge design, capability matrix and the Jev contract.
- [docs/skills.md](docs/skills.md): the calibrated skill catalog, phases and release rules, legal-action masks, interruption and revalidation.
- [docs/memory.md](docs/memory.md): working memory (bounded history, progress and stuck counters, controller context), derived events, the SQLite episode store, JSONL export and replay.
- [docs/graph.md](docs/graph.md): the learned world graph (platform segmentation, evidence-based edges, Dijkstra routes, atomic versioned checkpoints).
- [docs/planner.md](docs/planner.md): the strategic planner and goal manager (candidate goals, goal lifecycle, shared triggers, fallback, the Azure planner, graph route waypoints).
- [docs/benchmark.md](docs/benchmark.md): the benchmark protocol (paired trials, arm order, cold and warm memory regimes, the manifest, per-episode records, metrics and summaries, the paid-run ceiling and Azure price table, Dave settings).
- [docs/inspector.md](docs/inspector.md): the decision inspector (`dave-agent inspect`: exact replay checked against the recorded request digests, per-decision bundles with the exact Jev and LLM requests, real outcomes of every option, screenshots, opt-in `--ask`) and the local read-only viewer in `frontend/` (Next.js).
- [docs/live.md](docs/live.md): the live viewer (`dave-agent live` streams a running episode; `frontend/` `/live` shows the game, every Jev/LLM decision and the planner's map and waypoints as they happen).
- [docs/tactical.md](docs/tactical.md): tactical controllers (the shared request, output validation, retry, deterministic fallback, per-episode budgets, context digests, the Azure and Jev tactical models).
- [docs/state_mapping.md](docs/state_mapping.md): deadly-dave C fields mapped to `Observation` fields.
- [docs/jev-apis.md](docs/jev-apis.md): the OpenRouter chat SDK and the decisions endpoint, including models, payload shape, question types, and config.
- [docs/development.md](docs/development.md): uv with the `jev/` venv (`UV_PROJECT_ENVIRONMENT=jev`), commands, single-test runs and config layout.
- [docs/article.md](docs/article.md): draft article (System One Jev for moves, LLM for plans) with charts in `docs/images/` from `scripts/article_figures.py`.
- [docs/notebook.md](docs/notebook.md): what `test.ipynb` contains and why its cells depend on execution order.

This file is the overview only. Put new detailed documentation in a `docs/*.md` file and link it here.
