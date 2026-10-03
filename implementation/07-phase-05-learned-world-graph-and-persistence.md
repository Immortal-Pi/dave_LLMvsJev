# Phase 5 — learned world graph and persistence

Read [the project overview](01-project-overview.md) first. Complete Phase 4 before dependent work in this phase.

### Tasks

Use a directed `MultiDiGraph`: direction matters and two skills can connect the same locations with different outcomes. Define stable location nodes as standable platform segments/regions rather than one node per frame. Start with a documented deterministic region segmentation method.

Node attributes: level/build ID, region bounds, surface type, observed/discovered state, known items/exit conditions, last verified frame, evidence references. Track moving enemies in working memory; do not encode their current positions as permanent topology.

Edge attributes: skill/parameters, inventory preconditions, observed attempts/successes/failures, elapsed simulation frames, uncertainty, observed fatal outcomes, evidence IDs, and validation status. Create a traversable edge after a successful observed transition. Retain failed attempts separately and attach them to a candidate transition only when its target is identifiable. Do not fabricate an edge to an unseen destination.

Distinguish observed topology from model suggestions. An LLM can propose exploration targets but cannot assert a verified path. An untried edge has unknown reliability, not 100% success.

Update success estimates with a documented prior, for example `(successes + 1) / (attempts + 2)`, and store raw counts. Do not automatically equate every transition failure with death risk. Maintain fatal outcomes separately. Avoid deriving a macro's reliability from unrelated situations; include relevant inventory/region context.

Route search is deterministic Python, not LLM traversal. Filter incompatible edges first. Use Dijkstra for nonnegative additive costs. Begin with a normalized cost:

`cost = wt * expected_frames / reference_frames + wr * (-log(clipped_success_probability)) + wu * uncertainty_penalty`

The reliability term is an approximation: outcome correlations and state-dependent enemy behavior can violate independence. Record this limitation. A* is optional only with a justified admissible heuristic; use zero heuristic otherwise. Return `unreachable` or an exploration frontier when no verified route exists.

Maintain route progress. Replan when topology/preconditions change, a route fails, or the target is achieved. Do not search every frame. For inventory-gated routes, either filter based on current inventory and replan after acquisition, or explicitly search augmented `(region, inventory)` states; do not mix unreachable prerequisites into a plain shortest path.

Persist versioned JSON atomically via temporary file and rename. Include build/scenario ID, observation policy, training lineage, counts, and schema version. Export YAML for inspection. Reject incompatible checkpoints; retain a previous valid checkpoint on interrupted writes.

### Acceptance gate

Tests establish directed/multiple-edge behavior, least-cost route choice, unavailable-inventory filtering, unknown-route handling, evidence-based updates, successful round trips, and checkpoint compatibility rejection. A learned route survives restart. No oracle/unvisited map data leaks into local-observed graph memory.


## Phase completion handoff

Update `docs/progress.md` with implemented changes, exact verification commands/results, open blockers, and the next phase. Preserve all shared experiment constraints from the overview. Do not mark live integration complete using mock evidence.
