# Phase 8 — Jev tactical controller and hybrid loop

Read [the project overview](01-project-overview.md) first. Complete Phase 7 before dependent work in this phase.

### Tasks

Implement the Jev client using the verified current contract from Phase 0. Translate the same tactical input/candidates into its documented structured choice request. Keep provider-specific code separate from game logic.

Do not assume the response contains a probability distribution, confidence, reasoning, or token usage. Preserve actual provider fields. Unsupported metrics are null, not zero. Use a sanitized real response fixture for contract tests.

Connect the hybrid loop: shared planner establishes goal → Jev selects skills until a shared planning trigger fires → common executor steps game → memory/events update. Never keep executing a stale objective after a terminal or respawn event.

If Jev is unavailable, keep mock integration runnable and clearly mark live hybrid evaluation blocked. Do not quietly substitute another model while retaining the Jev label.

### Acceptance gate

B and C run end to end offline; a live Jev smoke episode runs when available. Candidate/context parity with A is demonstrated in logs. Shared retry/fallback limits apply. Planner calls are sparse because of event-driven goal validity, not an arbitrary wall-time timer.


## Phase completion handoff

Update `docs/progress.md` with implemented changes, exact verification commands/results, open blockers, and the next phase. Preserve all shared experiment constraints from the overview. Do not mark live integration complete using mock evidence.
