# Phase 0 — feasibility and scope gate

Read [the project overview](01-project-overview.md) first. Begin here after reviewing the overview.

### Tasks

1. Identify selected Dangerous Dave edition, game files, platform, emulator or compatible implementation, and license constraints. Record build hashes and setup instructions without committing binaries.
2. Compare two adapter approaches: instrumenting an existing compatible implementation, or reading a verified emulator memory interface. Prefer the approach that demonstrably exposes reset, stepping, and live state. Do not assume stock DOSBox has a remote RAM/step API.
3. Verify reset/restore, pause, exact or bounded frame advance, input press/release, structured state access, and headless operation. If frame stepping is unavailable, document limitations before calling the benchmark deterministic.
4. Enumerate actual controls and gameplay properties: position units, grounded state, gravity/jump behavior, lives/death, inventory, exit prerequisites, moving enemies, camera offset, jetpack/fuel if supported.
5. Check current Jev primary documentation: endpoint, authentication, model ID, input schema, allowed choice format, returned decision representation, timeout/rate limits, usage/cost fields, and whether a useful confidence signal exists. Record documentation URLs and verification date. Use environment variables, never committed credentials.
6. With credentials, make a minimal legal-choice API probe and retain a sanitized response fixture. Without them, implement a mock provider and mark live integration unverified.
7. Select observation policy: `local_observed` as the primary experiment, with a fixed visible/nearby region and discovered-map memory. A full-map/oracle condition is optional and reported separately. RAM access must not leak off-screen enemies or unvisited collectibles under the local policy.

### Deliverables

`docs/feasibility.md`, `docs/state_mapping.md`, sanitized provider fixture, adapter capability matrix, and initial configuration.

### Acceptance gate

An executable probe demonstrates reset → observe → action → advance → changed observation for the selected game, or clearly records the real-game blocker while the fixture adapter supports this sequence. Do not describe the fixture as completed Dave integration. No invented memory addresses or API schemas.


## Phase completion handoff

Update `docs/progress.md` with implemented changes, exact verification commands/results, open blockers, and the next phase. Preserve all shared experiment constraints from the overview. Do not mark live integration complete using mock evidence.
