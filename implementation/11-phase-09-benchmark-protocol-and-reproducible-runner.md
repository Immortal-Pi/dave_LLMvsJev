# Phase 9 — benchmark protocol and reproducible runner

Read [the project overview](01-project-overview.md) first. Complete Phase 8 before dependent work in this phase.

### Tasks

Build a manifest-driven benchmark CLI. Record game/adapter version, scenario snapshot hashes, seeds, observation policy, model IDs/settings, prompts/config hashes, code revision, memory checkpoint hashes, execution mode, budgets, and fallback behavior.

Use paired initial scenarios for each configuration; randomize arm order to reduce service/time effects. A common initial state does not mean later states are identical once agents choose different actions. Use snapshot-based tactical tests separately when identical decision states are desired.

Memory regimes:

1. **Cold:** fresh working memory and empty learned graph per trial. Graph may learn within the episode; label this correctly.
2. **Warm:** separate training episodes, fixed graph checkpoint, frozen updates during evaluation. Hold out test scenarios or clearly label repeated-level results as same-level learning.
3. **Continual learning, optional:** update graph across evaluation episodes and report a learning curve, not independent frozen-policy trials.

Do not mix these regimes in one aggregate. No cross-arm checkpoint sharing unless the explicit experiment asks how controllers perform from an identical pretrained graph.

Start with 3–5 smoke trials, then about 30 paired trials per scenario/configuration as an initial pilot. Increase trials based on variance and confidence-interval precision rather than treating a fixed count as proof. Set a configurable monetary ceiling before paid runs.

Primary metrics:

- Level completion rate and paired completion difference.
- Deaths/lives consumed per attempt.
- Simulation frames to completion, with failures/censoring reported separately.
- Total wall-clock duration and model decision latency p50/p95.
- API cost per attempt and successful completion where known.

Secondary metrics: score, model calls split by planner/tactical, provider-reported usage, retries, invalid choices, fallback count, skill executions, route replans, graph lookup time, repeated-failure rate, escalation reasons, observation/execution overhead. Define repeated failure using a fixed `(region, skill, failure type)` key, not a subjective model judgment.

Use a monotonic clock. Separate successful model latency from total latency including retries. Distinguish simulated time, API waiting, and total wall time. Report measured charges when available; label computed costs as estimates with the price source/date. Do not invent missing usage or costs.

Calculate episode-level confidence intervals, paired differences by matched scenario/seed, and success counts. Repeated episodes from one evolving graph are correlated; cluster by training run/seed or report learning curves. Never treat frames as independent experimental samples. Include all failures and termination reasons, not only completed games.

### Acceptance gate

A small mock benchmark generates manifest, per-episode records, summaries, and paired metrics reproducibly. Tests verify cost/latency aggregation and failure inclusion. Paid full runs require actual verified providers and game adapter; output clearly identifies simulator vs Dave.


## Phase completion handoff

Update `docs/progress.md` with implemented changes, exact verification commands/results, open blockers, and the next phase. Preserve all shared experiment constraints from the overview. Do not mark live integration complete using mock evidence.
