# Phase 10 — reporting, replay, and human inspection

Read [the project overview](01-project-overview.md) first. Complete Phase 9 before dependent work in this phase.

### Tasks

Generate Markdown/HTML reports and CSV/JSON summaries. Include:

- Configuration table and setup limitations.
- Completion rate with confidence intervals.
- Cost and latency comparisons.
- Deaths/failures by scenario and termination reason.
- Warm-memory learning curves where applicable.
- Controller/fallback and planner-call breakdown.
- At least a few successful and failed trace examples.
- Graph diagram showing observed regions, directed skills, attempts/reliability, and selected route.

Replay saved traces using snapshots plus recorded inputs when supported. Distinguish input replay from re-querying nondeterministic models. Optional video is for human review, never perception input. Provide a CLI command to inspect the observation, goal, candidate list, decision, and outcome at a selected step.

### Acceptance gate

A reviewer can reproduce a fixture report and inspect why an action occurred. Graph labels match evidence logs. Reports make no speed/accuracy claims beyond collected results.


## Phase completion handoff

Update `docs/progress.md` with implemented changes, exact verification commands/results, open blockers, and the next phase. Preserve all shared experiment constraints from the overview. Do not mark live integration complete using mock evidence.
