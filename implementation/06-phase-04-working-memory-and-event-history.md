# Phase 4 — working memory and event history

Read [the project overview](01-project-overview.md) first. Complete Phase 3 before dependent work in this phase.

### Tasks

Implement short-term memory as typed in-process state:

- Latest filtered observation.
- A deque of recent observations/actions indexed by simulation frames, covering a configurable recent window.
- Current goal and waypoint, last decision, current skill status.
- Derived motion, progress toward goal, and stuck/repetition counters.

Current state is what is happening now. The deque explains what changed recently. Neither requires a database lookup. Configure context limits and deterministic summarization; do not append an unbounded conversation transcript.

Implement SQLite tables for `runs`, `episodes`, `events`, `decisions`, `skill_executions`, and `model_calls`. Use foreign keys, schema versioning, batched writes, and a JSONL export. Preserve evidence connecting a transition outcome to its skill and start/end observations. Log deaths, goals achieved/failed, discovered areas, inventory changes, model failures, and termination reasons.

Do not invent death causes. Record `unknown` when evidence does not establish the cause. A nearby enemy alone does not prove a collision.

### Acceptance gate

History remains bounded; episode resets clear working state. SQLite survives process restart and exports replayable events. Derived velocity/stuck detection works on fixture sequences. Episode logging is identical in all experiment arms and does not itself provide additional planning context.


## Phase completion handoff

Update `docs/progress.md` with implemented changes, exact verification commands/results, open blockers, and the next phase. Preserve all shared experiment constraints from the overview. Do not mark live integration complete using mock evidence.
