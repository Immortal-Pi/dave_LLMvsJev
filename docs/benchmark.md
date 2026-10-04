# Benchmark protocol

Phase 9 (`implementation/11-phase-09-benchmark-protocol-and-reproducible-runner.md`). The code lives in `runner/benchmark.py` (schedule, isolation, manifest, ceiling), `runner/session.py` (the episode setup shared with `play`) and `evaluation/` (per-episode records, statistics, summaries).

```bash
# offline, free: the fixture (synthetic platformer, NOT Dangerous Dave)
uv run dave-agent benchmark --arms A,B,C --trials 3
# offline, free: Dave with the mock controllers
uv run dave-agent benchmark --config configs/benchmark_dave.yaml --adapter dave --scenarios level1 --arms A,B,C --trials 3
# warm regime: train a checkpoint for arm C, then evaluate with it frozen
uv run dave-agent train-memory --config configs/benchmark_dave.yaml --adapter dave --scenarios level1 --arm C --episodes 20 --out artifacts/graphs/train/arm-C/dave
uv run dave-agent benchmark --config configs/benchmark_dave.yaml --adapter dave --scenarios level1 --arms B,C --memory-regime warm --checkpoint C=artifacts/graphs/train/arm-C/dave
# rebuild the summaries of a finished (or interrupted) benchmark from its records
uv run dave-agent summarize --benchmark artifacts/benchmarks/<id>
```

Live runs add `--planner live --tactical live` and are paid. See [Paid runs](#paid-runs).

## Trials and pairing

- **Seeds and pairing.** For each scenario and trial `k`, every arm starts from the same scenario with seed `benchmark.seed + k` (`--seed` overrides the base). The pair key is (scenario, trial, seed).
- **Arm order** is shuffled per (scenario, trial) by `random.Random("<base seed>:<scenario>:<k>")` when `benchmark.randomize_arm_order` is true. This spreads time-of-day and provider-load effects over the arms. The order is in the manifest's `schedule`, and each record's `order` gives its position.
- **A common start is not a common trajectory.** Arms diverge as soon as they choose differently. Identical decision states are tested separately: the parity tests and context digests (`docs/tactical.md`).
- **Determinism.** Dave is deterministic, so in live runs only the models vary. In mock runs the seeded mock makes choices from the trial seed, and every arm gets the same mock, so A, B and C match.
- **Trial counts.** `benchmark.pilot_trials` (5) is the default for `--trials`. The spec's plan is 3–5 smoke trials, then about 30 paired trials per scenario and configuration (`benchmark.initial_trials`). Increase the count based on the confidence-interval width, not as proof.

## Isolation

- Each episode gets a fresh adapter process, fresh models (a mock seeded with the trial seed), a fresh goal manager and fresh working memory.
- Each episode is its own store run: `<id>-<scenario>-t<k>-<arm>` in `<out>/events.sqlite`, with `command = benchmark`. `export` and `replay` work on it as usual.
- No knowledge or outcome passes between arms. Nothing passes between trials either, except through a warm checkpoint, which is frozen.

## Memory regimes

Each regime is a separate benchmark. Regimes are never pooled in one summary.

| Regime | Graph arms | Label (`memory`) |
| --- | --- | --- |
| `cold` (default) | An empty graph store every trial. It learns within the episode only and is saved to `<out>/graphs/<arm>/<scenario>-t<k>/` (one `<level>.json` per level) for inspection, never reused | `in-episode` |
| `warm` | Reads its own checkpoint (`--checkpoint ARM=PATH`), a store directory (one checkpoint per level, `docs/graph.md`) or a legacy combined `.json`, freshly loaded every episode, never updated or saved | `frozen-checkpoint` |
| (graph-disabled arms) | No graph | `none` |

Warm-regime rules:

- **A checkpoint is required** for every graph arm in the benchmark. Checkpoints are refused for non-graph arms and in the cold regime.
- **No cross-arm sharing.** A checkpoint whose lineage names another arm is refused. `--shared-checkpoint` exists only for the spec's explicit "identical pretrained graph" experiment.
- **Frozen.** The sha256 of each checkpoint (for a store directory: every level file's name and bytes, in name order) is recorded before the run and checked after it. A change sets the manifest status to `checkpoint_changed`.
- **Same-level learning is labeled.** If an evaluation scenario is in the checkpoint's training lineage, it is listed under `checkpoints.<arm>.same_level_learning` in the manifest, and the records set `same_level_learning: true`. Hold out test levels to avoid this.
- **Grouping.** All records of a warm benchmark carry one `checkpoint` label (a hash of every arm's checkpoint hash). Groups therefore split per checkpoint set, while arms within one benchmark still pair.

`train-memory` builds a checkpoint: `--episodes N` training episodes of one graph arm. It cycles `--scenarios`, uses seed `benchmark.seed + i`, records lineage and saves after every episode.

Continual learning (updating the graph across evaluation episodes, with a learning curve) is optional in the spec and **not implemented**.

## Before anything runs

The benchmark checks the following and refuses before starting the game or making any call:

- **Arms:** known and distinct, and compatible with `--planner/--tactical live`.
- **Memory regime:** the checkpoint rules above.
- **Cost:** in live mode, every live role must report its cost or have a price table (below), unless `--allow-unpriced` is passed.
- **Output:** the directory is new or empty.
- **Credentials:** every arm's models are built and closed.
- **Game and scenarios:** the adapter resets every (scenario, seed) once. This proves the game runs and gives the scenario hashes.

## Manifest (`<out>/manifest.json`)

It is written before the first episode, updated after every episode, and finalized at the end.

| Field | Content |
| --- | --- |
| `benchmark_id`, `created_at`, `finished_at`, `status` | `running`, `complete`, `interrupted` (Ctrl-C), `budget_stopped` or `checkpoint_changed` |
| `mode`, `paid` | `mock`, `live-planner`, `live-tactical` or `live` |
| `environment`, `environment_label`, `adapter` | `fixture: synthetic test platformer, not Dangerous Dave` or `dave: Dangerous Dave via the deadly-dave bridge`; adapter, `build_id`, frames per second |
| `code`, `runtime` | git revision plus a dirty flag (tracked files); Python, platform and package versions |
| `config_path`, `config_sha256`, `config` | the full effective configuration and its hash |
| `prompt_hashes` | sha256 of `GAME_RULES`, `INPUT_GUIDE`, `TACTICAL_TASK`, the Azure planner and tactical system prompts, and the Jev request builder's source |
| `arms` | per arm: planner, tactical and graph flags, plus the effective planner and tactical settings (model ids, deployment, reasoning effort, timeouts, transport retries, price) |
| `observation_policy`, `execution_mode`, `reach_hints` | the shared observation and execution settings; `reach_hints: false` is the ablation without reachability waypoints and estimated end tiles (`--reach-hints off`, all arms) |
| `memory_regime`, `memory`, `checkpoints`, `shared_checkpoint` | regime, per-arm label, and warm checkpoint path, sha256, training episodes and scenarios, `trained_by`, `same_level_learning` |
| `scenarios`, `scenario_hashes`, `trials`, `base_seed`, `randomize_arm_order`, `schedule` | sha256 of each canonical reset observation (without per-episode ids) per scenario and seed; one schedule entry per (scenario, trial) with its seed and arm order |
| `budgets`, `fallback`, `prices` | per-episode frame, wall-time, tactical and planner budgets; `paid_run_budget_usd`; unpriced roles; fallback skills, `on_budget_exhausted`, `max_retries`; the price tables |
| `episodes_run`, `not_run`, `spent_usd` | progress, the episodes not run with the reason (`paid_run_budget_usd` or `interrupted`), and the reported and estimated spend |

## Per-episode records (`<out>/episodes.jsonl`)

One JSON line per episode is appended as soon as it ends, so an interrupted benchmark keeps everything it finished. **Every episode gets a record:**

- an exception inside an episode is recorded with `outcome: error` and the error text, and the benchmark goes on;
- budget stops and `max_frames` truncations keep their termination reason.

| Group | Fields |
| --- | --- |
| Identity | `benchmark_id`, `environment`, `mode`, `regime`, `checkpoint`, `scenario`, `trial`, `seed`, `arm`, `order`, `run_id`, `episode_key`, `memory`, `reach_hints`, `arm_checkpoint_sha256`, `same_level_learning` |
| Outcome | `outcome`, `termination_reason`, `error`, `completed` (`level_complete`), `frames`, `frames_to_completion` (null means censored), `deaths`, `lives_left`, `score` |
| Time | `wall_seconds` (monotonic, the whole episode), `model_wait_seconds` (sum of call latencies), `overhead_seconds` (the difference: observation, execution, planning code, logging). Simulated time is `frames` |
| Decisions | `decisions`, `forced_decisions`, `model_decisions`, `fallback_decisions`, `fallback_reasons` |
| Calls | `tactical_calls`, `planner_calls`, `call_status` (non-ok per purpose and status), `invalid_outputs`, `transport_retries`, `tactical_reasks` (extra calls for one decision) |
| Latency | `tactical_ok_latency_ms` (successful tactical calls only) and `decision_latency_ms` (all calls for one decision, re-asks included), each `{n, p50, p95}`, plus the raw lists |
| Usage and cost | `tokens` per purpose (as reported); `cost_reported_usd`, `cost_estimated_usd`, `cost_unknown_calls`, `cost_usd` |
| Planning and skills | `planning_episodes`, `planner_fallbacks`, `planning_triggers` (escalation reasons), `route_replans`, `route_ms` (graph lookup), `skill_executions`, `skill_outcomes` |
| Repeated failures | `failures`, `repeated_failures`, `repeated_failure_keys`. A failure is a skill execution that did not complete. Its fixed key is `(start tile, skill, failure type)`, for example `c3r9\|jump_right\|interrupted:hazard_contact`. A failure is repeated when its key occurred earlier in the episode |
| Trace | `candidate_trace` (as in `play`) |

**Cost rules:**

- **Reported vs estimated.** `provider_reported` (Jev) and `estimated` (from a price table) are summed separately.
- **Unknown cost.** A call with neither, from a provider that makes paid calls, counts in `cost_unknown_calls`, and `cost_usd` is then null, never zero.
- **Mocks.** Offline mocks (`mock`, `scripted`) make no paid call and count as zero.

## Summaries

`summary.json`, `summary.csv` (one row per group and arm) and `pairs.csv`. They are built only from `episodes.jsonl`, so `dave-agent summarize` rebuilds them byte for byte.

- **The episode is the unit;** frames are never samples. Groups are (`environment`, `mode`, `regime`, `checkpoint`, `scenario`), then the arm, so mock and live, fixture and Dave, cold and warm, and different checkpoints are never pooled.
- **Per arm:**
  - **Completion:** `n`, `completions`, `completion_rate` with a Wilson interval (`benchmark.confidence`).
  - **Deaths:** `deaths_mean` and total.
  - **Frames to completion:** median and mean among completers, plus the `censored` count.
  - **Time and latency:** mean frames and wall time, model wait, and the pooled tactical latency p50/p95 for successful calls and per decision.
  - **Cost:** total, per attempt and per success, only when every call's cost is known (else null with `cost_unknown_calls`), plus the reported and estimated sums.
  - **Usage and failures:** tokens; call, fallback, invalid-output, retry, re-ask, planner-fallback and route-replan counts; `repeated_failure_rate`; outcome and termination-reason counts; errors.
- **Pairs.** Each pair of arms is matched on (trial, seed) within a group:
  - **Counts:** `n_pairs`; `unpaired` (episodes whose partner did not run); both, only-first, only-second and neither completed.
  - **Completion:** `completion_diff` (first minus second) with a percentile-bootstrap interval resampling whole pairs (`benchmark.bootstrap_samples`, seeded with `benchmark.seed`); the exact McNemar p-value from the discordant pairs.
  - **Deaths and frames:** the paired deaths difference with its interval; the frames difference over pairs where both completed.
- **Correlated episodes.** In a warm regime all episodes read the same checkpoint; report per checkpoint, and repeat training runs to cluster by training seed. Continual learning would need learning curves, not these summaries.

## Reproducibility

Rerunning the same benchmark (same config, code, arms, trials, seed and id) gives the same manifest, records and summaries, except for the fields in `evaluation.metrics.VOLATILE_FIELDS`:

- timestamps;
- `wall_seconds`, `model_wait_seconds`, `overhead_seconds` and their aggregates;
- `route_ms`;
- `code`, `runtime`, `store`, `out_dir`.

`tests/integration/test_benchmark_mock.py` checks this on the fixture, together with:

- arm isolation;
- the warm-regime rules;
- the paid ceiling;
- error and interrupt handling;
- refusing an unpriced live arm.

## Paid runs

- **The ceiling.** `benchmark.paid_run_budget_usd` is checked before every episode. Once reported plus estimated spend reaches it, the run stops (`budget_stopped`), and the remaining episodes are listed in `not_run`. An episode already running finishes, so the ceiling can be overrun by at most one episode.
- **Worst case.** Before the first episode, the benchmark prints the worst-case call counts (episodes × the per-episode call caps) to stderr.
- **Azure prices.** Azure reports no cost. Set `models.planner.price` and `models.tactical_llm.price` in `configs/models.yaml` to `{input_per_mtok, output_per_mtok, source, as_of}` from your deployment's price page. Azure calls then get `cost_usd` with `cost_source: estimated`, and the price source and date go into the record's `output` and the manifest. Completion tokens include reasoning tokens and are priced once at the output rate. Cached-input discounts are not modelled. Without prices, a live benchmark refuses to start; `--allow-unpriced` runs anyway, and the ceiling then sees only Jev's reported cost.
- **Per-episode cost budget.** `tactical.max_cost_usd_per_episode` stays null, so the arms stay symmetric (`docs/tactical.md`).

## Dave settings (`configs/benchmark_dave.yaml`)

This is `experiments.yaml` with the frame-based settings rescaled to measured Dave skill durations. They are identical for every arm and provisional: revisit them after the smoke trials.

**Evidence:**
- `scripts/skill_frames.py` over 1707 Dave executions (2026-10-03):
  - walks take 24 or 72 frames;
  - long jumps have a p50 of 44 frames, short jumps 85–95;
  - the longest skill is 138 frames;
  - a typical skill is about 72 frames (p75, excluding the ceiling-bonk `jump_up` spam).
- Level 1: the scripted route takes 554 frames, and live Jev finished in 470.

| Setting | Fixture | Dave | Reason |
| --- | --- | --- | --- |
| `memory.recent_history_frames` | 120 | 600 | 8 context entries × a 72-frame skill |
| `planning.no_progress_frames` | 180 | 240 | above the longest skill, about 3 typical skills |
| `planning.min_frames_between_calls` | 60 | 150 | about 2 typical skills |
| `planning.goal_timeout_frames` | 480 | 1200 | about 2× the whole scripted level-1 route |
| `benchmark.max_episode_frames` | 600 | 3600 | about 6.5× the scripted route, with room for deaths (a burn is about 200 frames) |
