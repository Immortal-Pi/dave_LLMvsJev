# Phase 7 — LLM tactical baseline

Read [the project overview](01-project-overview.md) first. Complete Phase 6 before dependent work in this phase.

### Tasks

Implement a model client with timeout, bounded retry/backoff, structured-output parsing, schema validation, and sanitized call logging. Use configurable model ID/settings and record effective settings for every run.

The tactical prompt contains only current/recent structured state, active goal/waypoint, shared constraints, and candidate IDs/descriptions. Return exactly one allowed candidate ID. Translate it through the common executor. No extra tools or unbounded planning inside this controller.

Invalid outputs use the common retry policy, then a documented deterministic legal fallback. Count model failures and fallback decisions. Never count a fallback as a successful model decision. Enforce per-episode call, token/cost when available, wall-time, and simulation-frame budgets.

### Acceptance gate

A complete fixture episode runs with a mock LLM, then a small live smoke run when credentials exist. Invalid responses, timeouts, unauthorized candidate IDs, and budget exhaustion terminate/recover as configured. Every action is traceable to one decision.


## Phase completion handoff

Update `docs/progress.md` with implemented changes, exact verification commands/results, open blockers, and the next phase. Preserve all shared experiment constraints from the overview. Do not mark live integration complete using mock evidence.
