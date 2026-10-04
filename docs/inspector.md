# Decision inspector: what the models saw

`dave-agent inspect` rebuilds, for each decision of a recorded episode, exactly what the tactical models were shown. It writes an inspection bundle. The local viewer in `frontend/` (Next.js) displays it. Code: `src/dave_agent/runner/inspect.py`; tests: `tests/integration/test_inspect.py`.

## Why a rebuild

The episode store keeps the observation, the offered candidate ids, Jev's probabilities and a digest of every tactical request (`context_digest`). It does not keep the request text, so the candidate notes (reach estimates, experience notes) and the goal waypoint are not stored.

The inspector therefore replays the episode on the real game with the recorded choices:
- **Planner:** `ReplayPlanner` returns the recorded goals and their waypoints, rebuilt from the `goal_set` events. Failed attempts are replayed as failed calls.
- **Tactical controller:** `ReplayController` returns the recorded candidate. Forced decisions are taken by the runner as usual.
- **Graph-enabled arms:** start from the graph store as it was before the run, level by level.
  - By default this is the arm's store. Each level file is used as it is, or its `.bak` when the file already includes the run.
  - A level whose lineage starts with the run was first learned in it, so it is left out.
  - `--graph PATH` names a store (or a legacy combined checkpoint) explicitly, and `--graph empty` starts from an empty store.
  - A level learned before the run with no saved version from before it is refused.
- **Threat screen:** candidates are screened again during the replay (`docs/skills.md`), since the screen depends only on observations. Runs recorded before the screen existed no longer match their digests.

**Exactness check.** Every rebuilt request's `context_digest` must equal the recorded one. The first mismatch stops the inspection with the decision number (`InspectError`), so a bundle is never shown as exact when it is not. The settings come from the config recorded with the run, not from a config file.

## Command

```bash
uv run dave-agent inspect --store artifacts/benchmark-dave.sqlite --run-id demo-C-L2-1
#   [--decisions 90-95,100] [--no-outcomes] [--graph PATH|empty] [--out artifacts/inspect/<run-id>]
uv run dave-agent inspect ... --decisions 93-94 --ask jev --ask azure   # PAID: one call per provider per decision
```

- **Outcomes** (on by default): every offered candidate is executed from the decision's state, using `control/skills.execute` after restoring the snapshot. Each outcome records:
  - outcome, reason and whether it was fatal;
  - end tile, end pixel position and state;
  - frames, events and score change;
  - the pixel trajectory.

  This is what each option *really* does. On Dave a decision takes a few seconds (one snapshot restore per candidate). `--no-outcomes` skips it.
- **Screenshots:** the bridge's rendered frame at the decision (BMP). The fixture adapter has none.
- **`--ask`:**
  - **What it sends:** the rebuilt request, to a live model (`jev` through OpenRouter, `azure` for the LLM).
  - **When it is refused:** it needs `--decisions`. Credentials are checked before the replay starts, and a notice is printed first.
  - **Where answers go:** into the decision's `asked` list. Earlier answers are kept when the bundle is rebuilt.

## Bundle

`artifacts/inspect/<run-id>/` is gitignored, like all of `artifacts/`.

| File | Content |
| --- | --- |
| `run.json` | Run, arm, controller, adapter, scenario, seed, the graph used, the episode summary, `digests_verified`, every decision (tile, chosen skill, outcome, end tile, goal, forced, fallback, `provider_score`, fatal, whether it was inspected) and the goal events. |
| `decisions/<seq>.json` | The exact `TacticalRequest`, plus the exact provider bodies: `bodies.jev` (the decisions request) and `bodies.azure` (the system and user messages). Also: the recorded answer (provider, probabilities, confidence, latency, cost); what the recorded execution did; the planner request and the `goal_set` behind the current goal; `outcomes`; `screenshot`; `asked`. |
| `decisions/<seq>.bmp` | The screenshot. |

`schema_version` is 1. The Azure body carries `<deployment>` instead of the deployment name, and no body contains credentials.

## Viewer (`frontend/`)

A local, read-only Next.js app (App Router, TypeScript). It reads bundles from `INSPECT_DIR`, which defaults to `../artifacts/inspect`, and never calls a model or writes a file. This is a user decision that overrides the spec's "no web application" for V1; the app is not deployed.

```bash
cd frontend && npm install && npm run dev     # http://localhost:3000
npm run lint && npm run build                  # checks
```

| Route | Shows |
| --- | --- |
| `/` | Inspected runs |
| `/runs/<run>` | Episode summary; a heat map of where decisions were taken (fatal start tiles in red); the goal timeline with the planner's rationale and waypoints; every decision, with back-and-forth loops marked and a tile filter |
| `/runs/<run>/<seq>` | The perspective at one decision (see below). Arrow keys step between inspected decisions. |

The perspective page (`/runs/<run>/<seq>`) shows:
- **The tile grid exactly as the request encodes it.** It marks Dave's pixel position and the goal waypoint. Hovering an option draws that option's estimated end tile (dashed) and its real path (solid; red when fatal).
- **The screenshot.**
- **Every option** as the models read it, with its notes, the recorded probabilities, any asked answers, the estimated end versus the real outcome, and "estimate wrong" marks.
- **The goal and the planner choice.**
- **The memory in the request:** progress and recent skills.
- **Tabs with the exact payloads:** the Jev body, the LLM system prompt and user message, the tactical request, the planner request, and the answers.

Components are in `frontend/components/` (`TileGrid`, `OptionsTable`, `ProbabilityBar`, `GoalPanel`, `JsonView`, `Timeline`, `MiniMap`, `Perspective`, `KeyNav`); the bundle types and loaders are in `frontend/lib/bundle.ts`. No game asset is copied into `frontend/public/`: screenshots are served from the bundle by `/api/shot/<run>/<seq>`.
