# Phase 3 — action catalog and deterministic execution

Read [the project overview](01-project-overview.md) first. Complete Phase 2 before dependent work in this phase.

### Tasks

Define supported atomic buttons and bounded skills such as `move_left`, `move_right`, `jump_left`, `jump_right`, `wait`, and `shoot` only when verified. Add jetpack skills only after fuel/control semantics work. Start with a small catalog and calibrated durations.

For each skill document:

- Parameters and frame range.
- Preconditions and legal candidate generation.
- Button sequence and release rules.
- Completion/progress predicate, interruption conditions, and maximum duration.
- Whether it is a single action or a macro.

Use simulation frames, not wall-clock sleeps, to execute. A macro may poll state and abort on death, missing preconditions, changed hazards, or time budget. It must not secretly plan a global route. If a deterministic reflex performs enemy avoidance, expose it as a common feature and log interventions for every arm; otherwise it confounds Jev's contribution.

Prevent duplicate/overlapping inputs and cap all execution lengths. Revalidate candidates against the latest observation before execution. Under real-time mode, reject stale decisions and use a documented shared fallback. Under paused mode, hold game time while waiting for model calls.

### Acceptance gate

Repeat skill fixtures from identical snapshots. Verify deterministic execution, correct release on failures, legal-action masks, macro cancellation, and bounded duration. LLM and Jev receive exactly the same candidate list.


## Phase completion handoff

Update `docs/progress.md` with implemented changes, exact verification commands/results, open blockers, and the next phase. Preserve all shared experiment constraints from the overview. Do not mark live integration complete using mock evidence.
