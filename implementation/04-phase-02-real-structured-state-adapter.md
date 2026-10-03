# Phase 2 — real structured-state adapter

Read [the project overview](01-project-overview.md) first. Complete Phase 1 before dependent work in this phase.

### Tasks

1. Implement the selected bridge from Phase 0. Keep raw memory decoding inside the adapter.
2. For memory extraction, document verified addresses/offsets, endianness, executable hash, and evidence for each field. Calibrate positions by known movements; check reset and level changes. Reverify mappings when game builds differ.
3. For source instrumentation, expose actual runtime state through a narrow bridge and retain the implementation/version identifier.
4. Derive local tiles, hazards, collision surfaces, inventory, and entity motion from available state. Use observation history only when velocity is absent; mark the value as derived.
5. Implement input release, death/respawn detection, terminal events, snapshot restoration, frame counters, and resource cleanup.
6. Filter raw state through the configured observation policy before any agent or memory component sees it.
7. Add optional video/screenshots for human debugging only; do not pass them to models.

### Acceptance gate

A scripted run demonstrates movement, a supported jump/action, collectible interaction when feasible, death/respawn, and repeatable restoration. Recorded structured positions align with independent human inspection of the game. Unknown fields stay unknown. If real-game integration remains blocked, report the limitation explicitly and continue only with harness development.


## Phase completion handoff

Update `docs/progress.md` with implemented changes, exact verification commands/results, open blockers, and the next phase. Preserve all shared experiment constraints from the overview. Do not mark live integration complete using mock evidence.
