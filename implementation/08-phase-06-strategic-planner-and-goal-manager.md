# Phase 6 — strategic planner and goal manager

Read [the project overview](01-project-overview.md) first. Complete Phase 5 before dependent work in this phase.

### Tasks

Implement a deterministic goal lifecycle: absent → planned → active → achieved/failed/expired. Give the LLM filtered state, bounded recent events, actual game rules, candidate goals, and graph route summaries only when that experiment enables them.

The planner selects an objective and constraints, such as collect the required exit item, reach a discovered exit, explore a frontier, or recover from a failed route. Use actual game terminology. Return structured output validated against known entities/regions. Ask for a brief rationale, not hidden reasoning or long transcripts.

Shared planning triggers:

- Episode/level start and no valid goal.
- Goal achieved, failed, or expired.
- Death/respawn.
- No progress for a configured number of simulation frames.
- Repeated failed skill outcomes.
- Changed inventory affecting the current route.
- Newly observed obstacle invalidating the plan.

Debounce triggers and cap planner calls. Use the same trigger rules for A and B. Provider-native Jev confidence escalation, if actually available, is a later separately labeled condition, not a hidden difference in the primary comparison. Do not assume a threshold such as 0.70 is calibrated.

Graph-enabled mode: LLM chooses the objective; Python computes the eligible route; next waypoint goes to the tactical controller. Graph-disabled mode: provide the same goal schema and observed target information without learned routes. Log route-search time separately from model time.

### Acceptance gate

Mock planner tests show no calls during stable goal execution, calls on documented triggers, rejection of unknown targets, and bounded recovery on malformed output. Missing routes result in explicit exploration/recovery. Both tactical-controller arms share planner code and trigger settings.


## Phase completion handoff

Update `docs/progress.md` with implemented changes, exact verification commands/results, open blockers, and the next phase. Preserve all shared experiment constraints from the overview. Do not mark live integration complete using mock evidence.
