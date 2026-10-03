# Dangerous Dave agent — phased build folder

Give Claude Code this entire folder. Start with the shared overview, then implement the phase files in numeric order. Phase numbers begin at 0; filename prefixes place the overview first.

## Reading and implementation order

1. [Shared architecture, constraints, configuration, tests, and kickoff prompt](01-project-overview.md)
2. [Phase 0: feasibility and scope gate](02-phase-00-feasibility-and-scope-gate.md)
3. [Phase 1: project skeleton, schemas, and offline contracts](03-phase-01-project-skeleton-schemas-and-offline-contracts.md)
4. [Phase 2: real structured-state adapter](04-phase-02-real-structured-state-adapter.md)
5. [Phase 3: action catalog and deterministic execution](05-phase-03-action-catalog-and-deterministic-execution.md)
6. [Phase 4: working memory and event history](06-phase-04-working-memory-and-event-history.md)
7. [Phase 5: learned world graph and persistence](07-phase-05-learned-world-graph-and-persistence.md)
8. [Phase 6: strategic planner and goal manager](08-phase-06-strategic-planner-and-goal-manager.md)
9. [Phase 7: LLM tactical baseline](09-phase-07-llm-tactical-baseline.md)
10. [Phase 8: Jev tactical controller and hybrid loop](10-phase-08-jev-tactical-controller-and-hybrid-loop.md)
11. [Phase 9: benchmark protocol and reproducible runner](11-phase-09-benchmark-protocol-and-reproducible-runner.md)
12. [Phase 10: reporting, replay, and human inspection](12-phase-10-reporting-replay-and-human-inspection.md)
13. [Phase 11: controlled real-time extension and optional ablations](13-phase-11-controlled-real-time-extension-and-optional-ablations.md)

## Kickoff prompt

```text
Read 00-README.md and 01-project-overview.md, then review all phase files. Inspect this repository and its AGENTS.md instructions. Implement phases in order, starting with Phase 0 feasibility and Phase 1 offline contracts. Follow each phase’s acceptance gate and maintain docs/progress.md. Continue through feasible work autonomously. Verify actual Dangerous Dave mechanics, adapter capabilities, and current Jev API contracts; do not invent them. Keep mock/simulator results distinct from verified live integration. Preserve identical observation, planning, working memory, action candidates, and execution settings for the LLM-vs-Jev comparison.
```

## Contents

12 phase files plus this README and the shared project overview. The overview contains common schemas, repository layout, benchmark arms, configuration, CLI targets, test requirements, and the definition of done.
