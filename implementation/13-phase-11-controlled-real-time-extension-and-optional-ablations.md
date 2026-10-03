# Phase 11 — controlled real-time extension and optional ablations

Read [the project overview](01-project-overview.md) first. Complete Phase 10 before dependent work in this phase.

Only after the paused/stepped benchmark is reliable:

1. Run live game time while models respond; measure state age, discarded stale decisions, control gaps, and total service latency.
2. Tune shared macro duration/frequency on training scenarios, then freeze for evaluation. Do not let one controller get larger action windows.
3. Compare atomic actions vs macros with the same catalog per comparison.
4. Add graph-enabled LLM controller D to complete the memory/controller factorial experiment.
5. Compare local-observed vs oracle observations as separate conditions.
6. Add provider-confidence escalation only after validating its semantics and calibrating it on training outcomes.
7. Consider semantic episode retrieval or Neo4j only if measured needs justify them. They are outside V1.

### Acceptance gate

Every ablation changes a named factor, retains a fixed benchmark manifest, and reports its limitations. Real-time results are separated from paused results.


## Phase completion handoff

Update `docs/progress.md` with implemented changes, exact verification commands/results, open blockers, and the next phase. Preserve all shared experiment constraints from the overview. Do not mark live integration complete using mock evidence.
