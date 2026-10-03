# Dangerous Dave: LLM vs LLM + Jev — phased implementation plan

## 1. Instructions to Claude Code

Build a reproducible research harness in Python for a platform game, with Dangerous Dave as the intended target. Agents must consume structured game state, without screenshots or a vision model. Compare an LLM tactical controller with a Jev tactical controller while holding the planner, observations, memory, and action interface constant. Add learned graph memory as a separate experiment.

Read this overview and the relevant phase files before implementing. Inspect the existing repository and any AGENTS.md instructions first. Work through phases in order. Implement, verify, and document each phase before moving on. Make routine implementation choices autonomously; ask only when a missing game executable, unavailable API, or an environment decision genuinely blocks work. Keep useful offline work moving when credentials or game access are unavailable. Do not claim that a simulator run proves Dangerous Dave integration works.

Maintain `docs/progress.md` with completed phases, checks and results, decisions, current blockers, and exact next steps. Update `docs/architecture.md` when interfaces change. Provide runnable commands and concrete validation evidence after each phase. Do not build a web application, deploy services, or add Neo4j/vector infrastructure for V1.

This document is an implementation specification, not a claim that any particular emulator already exposes the required API or that any current Jev endpoint/model is available. Phase 0 must verify those facts against primary documentation and working probes.

## 2. Research question and experiment design

Hypothesis: replacing frequent LLM tactical decisions with a structured Jev controller can reduce decision latency and cost while preserving or improving game performance. Separately test whether learned graph memory improves route choice and reduces repeated failures.

Use these configurations:

| ID | Planner | Tactical controller | Working memory | Learned route graph |
| --- | --- | --- | --- | --- |
| A | LLM | LLM | Same current state + recent history | Disabled |
| B | Same LLM | Jev | Same current state + recent history | Disabled |
| C | Same LLM | Jev | Same current state + recent history | Enabled |
| D, optional | Same LLM | LLM | Same current state + recent history | Enabled |
| E, engineering baseline | Fixed goal policy | Deterministic controller | Same available observation | Configurable |

A vs B isolates tactical-controller replacement within the same hierarchy. B vs C isolates learned graph memory. D completes a 2×2 comparison and helps identify interaction effects. A pure single LLM selecting every action without a separate planner may be added as a secondary baseline, but label it separately because it changes the architecture too.

All research configurations must share:

- Game build, levels, initial snapshots/seeds, observation policy, legal-action generator, deterministic skill executor, decision frame budget, termination criteria, and model retry policy.
- LLM planner model/settings and goal schema; common event-driven planning triggers.
- Identical working-memory fields and limits. Episode logs are recorded for every arm; graph-disabled arms cannot read learned route knowledge.
- The same input context and candidate skills for the LLM and Jev tactical controllers, except provider-specific request syntax.
- Separate memory directories per arm and trial. No route knowledge or outcome leakage between arms.

Treat cloud latency as a measured variable, not a guarantee of real-time reaction. First run in paused/stepped game time. Evaluate live real-time execution separately.

## 3. Architecture and ownership

Flow: adapter observes → working memory updates → event detector evaluates → goal manager requests planning if needed → tactical controller selects an allowed skill → deterministic executor applies buttons for bounded frames → adapter observes again → events and metrics are recorded.

| Component | Responsibility | V1 technology |
| --- | --- | --- |
| Game adapter | Reset, structured observation, bounded frame stepping, snapshots | Python interface + verified emulator or instrumented game bridge |
| Working memory | Latest state, rolling history, current goal, progress | Typed Python objects + deque |
| Episode store | Durable events, actions, outcomes, model calls | SQLite; JSONL export |
| World graph | Observed locations, feasible directed transitions, learned outcomes | NetworkX MultiDiGraph |
| Graph persistence | Versioned graph checkpoints | JSON; YAML for readable export/config |
| Goal manager | Goal validity and planning triggers | Deterministic Python |
| Strategic planner | Choose goal/waypoint and constraints | LLM returning validated structured output |
| Tactical controller | Choose one candidate skill | LLM or Jev behind the same interface |
| Skill executor | Buttons/frame duration, progress checks, interruption | Deterministic Python |
| Benchmark runner | Paired trials, isolation, metrics, reports | CLI + SQLite + Pandas/plots |

NetworkX is the graph used at runtime. JSON stores it between processes. YAML is appropriate for configuration and inspection; do not rewrite YAML every frame. SQLite records historical evidence. No database is queried inside the frame-level execution loop.

## 4. Non-negotiable game and API verification

Earlier conceptual examples mentioned keys, health, ducking, climbing, and confidence probabilities. Those were illustrative fields, not verified Dangerous Dave features. Discover actual mechanics for the selected edition. For example, the required exit collectible may be a trophy rather than a key; expose the real object and unlock condition. Do not invent duck/climb actions, a health bar, enemy damage values, or Jev confidence fields.

Only expose supported controls. Distinguish lives from health. Distinguish pixel coordinates from tile coordinates. Mark unavailable values as null with an availability flag; never silently substitute zero. Static level-file data does not by itself provide live player/enemy state.

Use a user-provided lawful game installation or a compatible implementation with verified license terms. Do not bundle commercial game binaries. A small deterministic test platformer is allowed for building/testing the harness, but keep its results labeled separately.

## 5. Proposed repository

```text
pyproject.toml
README.md
.env.example
configs/
  environment.yaml
  models.yaml
  skills.yaml
  experiments.yaml
src/dave_agent/
  cli.py
  schemas.py
  adapters/{base,fixture,simulator,dave}.py
  memory/{working,episodes,graph,persistence}.py
  control/{goals,triggers,skills,executor,legality}.py
  models/{base,llm,jev,mock}.py
  agents/{planner,tactical_llm,tactical_jev,deterministic}.py
  runner/{episode,benchmark,replay}.py
  evaluation/{metrics,statistics,report}.py
scripts/
  probe_environment.py
  probe_jev.py
tests/
  fixtures/
  unit/
  integration/
docs/
  feasibility.md
  architecture.md
  state_mapping.md
  skill_catalog.md
  progress.md
  benchmark_protocol.md
artifacts/  # generated, ignored by Git
```

This is a target structure, not a requirement to create empty files. Adapt to the repository's conventions. Suggested packages: Pydantic, NetworkX, PyYAML, an HTTP client, pytest, and plotting/reporting packages when needed. Use standard-library SQLite/deque. Pin a compatible environment and commit a lockfile. Avoid introducing LangGraph unless existing project structure or demonstrated workflow complexity warrants it; the control loop is a straightforward state machine.

## 6. Configuration starter

Illustrative configuration; implement and validate these keys. Values are starting points to calibrate, not measured defaults.

```yaml
schema_version: 1
environment:
  adapter: fixture             # dave only after verification
  observation_policy: local_observed
  execution_mode: paused_step
  decision_frames: 8           # calibrate against actual game frame rate
memory:
  recent_history_frames: 120
  episode_store: artifacts/events.sqlite
  graph_enabled: false
  graph_checkpoint: null
  graph_updates: true
planning:
  no_progress_frames: 180
  repeated_skill_failures: 3
  max_calls_per_episode: 30
models:
  planner_id: REQUIRED_FOR_LIVE_RUN
  tactical_llm_id: REQUIRED_FOR_LIVE_RUN
  jev_model_id: VERIFY_CURRENT_MODEL_ID
  timeout_seconds: 20
  max_retries: 1
benchmark:
  pilot_trials: 5
  initial_trials: 30
  max_episode_frames: 18000
  max_episode_wall_seconds: 900
  paid_run_budget_usd: 5
  randomize_arm_order: true
```

Store secrets in environment variables. `.env.example` lists names only. Ignore `.env`, game binaries, snapshots, raw credentials, generated artifacts, and caches. Logs redact secrets; preserve enough sanitized request detail to audit decisions.

## 7. Proposed CLI workflow

These are target commands to implement, not commands that already exist:

```bash
uv sync
uv run pytest
uv run dave-agent probe --adapter fixture
uv run dave-agent probe --adapter dave
uv run dave-agent probe-provider --provider jev
uv run dave-agent play --config configs/experiments.yaml --arm A --mock
uv run dave-agent play --config configs/experiments.yaml --arm B --mock
uv run dave-agent benchmark --config configs/experiments.yaml --arms A,B,C --trials 5 --mock
uv run dave-agent train-memory --config configs/experiments.yaml --arm C --episodes 20
uv run dave-agent benchmark --config configs/experiments.yaml --arms A,B,C --memory-regime warm
uv run dave-agent report --run-id RUN_ID
uv run dave-agent replay --episode-id EPISODE_ID
```

CLI help must distinguish mock/simulator/live modes, validate missing prerequisites before spending API money, and show estimated configured budget. Preserve completed run data when a run is interrupted.

## 8. Required meaningful tests

| Area | Verification |
| --- | --- |
| Adapter | Reset/action/step semantics; snapshot reproduction; actual state mapping |
| Observation policy | Off-screen/unvisited data excluded in local mode |
| Executor | Legal candidate enforcement, bounded actions, key release, interruption |
| Working memory | Bounded history, episode reset, motion/progress detection |
| Episode store | Foreign keys, restart durability, complete event linkage |
| Graph | Directed parallel edges, evidence updates, inventory filters, persistence |
| Planner | Trigger behavior, valid goals, unknown targets, recovery limits |
| Providers | Actual response fixtures, malformed output, timeout, unavailable fields |
| Parity | Same tactical state/candidates and planner settings across A/B |
| Benchmark | Arm isolation, seeds/manifests, failed-episode inclusion, paired summaries |
| Integration | Entire offline episode and optional verified real-game/provider smoke runs |

Default CI runs offline and never requires credentials. Mark paid/live tests explicitly. CI should check formatting/type contracts relevant to the project, tests, and the reproducible mock benchmark.

## 9. Definition of done

V1 is complete when the selected Dangerous Dave environment exposes verified structured state; supported actions execute through a common bounded controller; working/episode/graph memory function with provenance and restart persistence; A/B/C run with actual configured providers; and a reproducible benchmark/report shows outcomes, latency, cost, and failure cases without observation or memory leakage.

If the adapter or Jev access is unavailable, call the result a completed offline harness with blocked live integration. List precisely what remains. Do not replace missing evidence with a claimed result.

The first useful milestone is smaller: a reproducible structured-state adapter plus deterministic fixture episode, before any paid model calls. The first valid comparison is A vs B with graph disabled. Learned graph experiments come after that comparison works.

## 10. Copy/paste kickoff prompt

> Read `00-README.md`, `01-project-overview.md`, and the numbered phase files and inspect this repository and its AGENTS.md instructions. Implement this project phase by phase. Begin with Phase 0 feasibility checks and Phase 1 offline contracts. Verify the selected Dangerous Dave edition and available adapter capabilities; do not invent RAM addresses, game mechanics, or Jev API fields. Keep offline work progressing when credentials/game files are unavailable, but label live integration as blocked. Use identical observation, planner, short-term memory, candidate skills, and execution settings for the LLM and Jev controller comparison. Use NetworkX at runtime, versioned JSON for graph persistence, YAML for config/export, and SQLite for episode evidence. Maintain `docs/progress.md`, write meaningful offline tests, and report concrete verification after each phase. Continue autonomously through feasible phases; only ask when a missing external prerequisite blocks the next dependent action.
