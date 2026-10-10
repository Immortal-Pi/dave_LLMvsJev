# Learned world graph

The code is in `src/dave_agent/memory/`:

| File | Contents |
| --- | --- |
| `graph.py` | `WorldGraph` (one level), `GraphStore` (one graph per level), segmentation and evidence updates |
| `routes.py` | `find_route`, `RouteTracker` |
| `persistence.py` | JSON checkpoints, per-level stores and the YAML export |

## One graph per level

A `GraphStore` holds one `WorldGraph` per `level_id`, created when the level is first seen. The episode loop and the goal manager use the store like a graph, and every call is routed by the observation's `level_id`:
- `observe`, `record_execution` and `skill_evidence` go to the current level's graph;
- the goal manager plans on `store.for_level(level)`.

So no node, edge, frontier or route spans two levels. Before the split, level-1 frontier nodes appeared in level-2 route summaries. A `WorldGraph` created by a store asserts its level: observing another level raises `ValueError`. A skill that ends on another level (`record_execution` sees two `level_id`s) is recorded as `level_changed`, never as an edge; the new level's first view goes into its own graph.

Each level's graph is a NetworkX `MultiDiGraph`. It is used only by graph-enabled arms (C, D), and in Phase 5 it is learned and stored but not read during an episode. The Phase 6 planner will consume routes. A test shows that controllers see identical inputs with or without the graph.

## Nodes: platform segments (deterministic segmentation)

- **Standable cell:** a cell inside the observed region that holds no `solid` or `hazard` tile and sits directly above an observed `solid` tile. Only BRICK (`solid`) supports Dave in deadly-dave. Rows whose row below is outside the view are skipped.
- **Segment:** a maximal horizontal run of standable cells in one row. `open_left` / `open_right` mark a side clipped by the view edge, where what lies beyond is unknown.
- **Merging:** a new segment that overlaps or touches an existing node on the same level and row widens that node.
  - When one view joins two nodes, the older id survives, and the other id goes into `aliases`.
  - Ids are `{level}:r{row}:c{first_col}`.
  - Splits are not modelled, because the levels are static apart from item pickups.
- **Attributes:**

  | Attribute | Meaning |
  | --- | --- |
  | `level_id`, `row`, `col_min`/`col_max`, open flags, `surface` | position and extent |
  | `visited` | Dave has stood on it |
  | `items` | exit, collectibles, trophy, gun and climbables on its cells, as last seen |
  | `stays` | skills that ended on the same segment |
  | `inconclusive` | completed skills that ended airborne or off-map |
  | `failed_attempts` | failures whose target cannot be identified |
  | evidence | refs `episode_key#observation_id`; the newest 20 are kept, plus a full count |
  | `incidents` | deaths of skills started here: `{skill, cause, tile, ref}`, the newest 20 |
  | `last_verified_frame` | last frame the node was confirmed |

- **Where Dave stands** (`locate`): Dave must be grounded and standing or walking. Use the segment under Dave's centre tile. Dave's hitbox is 20 px, so if that tile matches no segment, try the neighbouring columns.
- **Never topology:** entities such as monsters, plasma and bullets. They live in working memory.
- **`local_observed`:** nodes come only from observed tiles. Tests check that every node lies within columns that were actually in view.

## Edges: observed transitions and evidence

`record_execution(start_obs, execution, ref)` runs after every skill:

| Case | Recorded as |
| --- | --- |
| Start not located (airborne, burning…) | `unanchored` counter |
| `completed`, ends standing on another node B | **success** on edge `A→B`, key `skill\|inventory_context`. Created on the first success with `validation="observed"`. |
| `completed`, ends on A | node `stays` (no self-loops) |
| `completed`, ends airborne or unmapped | node `inconclusive`. No transition was seen, and it is not a failure either. |
| `interrupted` / `failed` | **failure**. It attaches to an edge only if exactly one edge from A has the same key (the target is identifiable); otherwise it goes to `A.failed_attempts[key]`. |

- **Raw counts:** each edge stores `attempts`, `successes`, `failures`, `fatal` and `frames_total`.
  - `fatal` (a death or `hazard_contact`) is counted separately: not every failure is a death risk.
  - Success uses the Laplace prior `(successes+1)/(attempts+2)`, so an untried edge is 0.5, never certain.
- **Incidents:** a fatal skill also leaves an incident on its start node. `cause` is what touched Dave on his first burning frame (`control/threats.py` `contact_cause`): `plasma`, a monster type, the hazard tile's name, or `unknown`; `tile` is where. Route summaries for the planner list the incidents on a route's platforms (`docs/planner.md`), so the graph remembers where plasma or fire killed Dave.
- **Inventory context:** the items held (value > 0) when the skill started. It keeps a macro's reliability from mixing situations with and without the gun or trophy.
- **No edge is ever created to an unseen destination.**
- **Shown to the tactical model:** `skill_evidence(obs)` sums, per skill, the attempts, successes and fatal counts over the out-edges of Dave's segment with the same held items, plus that segment's `failed_attempts`, and names the segment most often reached. Graph-enabled arms see it as a "past runs" note on each candidate (`docs/memory.md`, "Experience notes"). It comes from the graph as it was at episode start.
- **Suggestions:** model or planner suggestions (`suggest(...)`) are kept in a separate list. They never create nodes or edges, and route search ignores them.

## Route search

`find_route(graph, start, target, inventory, cfg)` is a deterministic Dijkstra (`networkx`).
- **Edge filter:** an edge is usable only if its inventory context ⊆ the items held. This is an assumption, not verified for jetpack mode: holding extra items never blocks a move.
- **Cost:** between two nodes the cheapest usable parallel edge is used, with cost

  `time * expected_frames / reference_frames + risk * -log(clip(p, p_min, p_max)) + uncertainty / (attempts + 1)`.

  The weights and limits are in `graph:` in `configs/experiments.yaml` and are not calibrated. **Limitation:** the reliability term assumes edge outcomes are independent, but correlated failures and enemy timing break that.
- **Exit:** a target with an exit needs the trophy (verified for deadly-dave and the fixture rules). Without it the result is `unreachable` with `requires:trophy`. Inventory-gated routes are filtered on the current inventory, and the planner replans after an item is picked up.
- **Statuses:** `found` (nodes, steps, cost), `at_target`, or `unreachable` with a reason: `unknown_start`, `unknown_target`, `no_verified_route` or `requires:…`.
- **Frontier:** an unreachable result includes `frontier`:
  - reachable nodes with an open side, by cost;
  - then discovered nodes that were never visited, by id.
- **Ties** break deterministically by insertion order, which a JSON round trip preserves.
- **Goal credit:** with the goal's `target_ref` (the goal manager passes it), each edge's cost is multiplied by its start platform's credit for that skill and target (`credit_factor`): `1 + credit_penalty * (1 - rate) - credit_bonus * rate`, where `rate` is the share of past goals for that target that used the move from that platform and were achieved. With no past goal the factor is 1. `graph.credit_bonus` (0.5) and `graph.credit_penalty` (1.0) are not calibrated. Credit is stored on nodes as `credit` (`docs/memory.md`, "Goal credit"). The attribute is optional, so older checkpoints load unchanged, and merging two nodes adds their credit.

`RouteTracker` follows a route at decision boundaries only. It returns a replan reason in this order: `target_reached`, `edge_failed`, `inventory_changed`, `topology_changed` (when `topology_version` changed, i.e. a node or edge was added or merged), or `off_route`.

## Live move scores (`control/move_score.py`)

While Dave plays, every option of a decision is scored from the **live** graph, the one learning this episode, so a move that just failed scores worse at the next decision. Graph-enabled arms only; the score informs and nothing is masked or reordered.

- **Position value `V(p)`:** the cost of the best way from platform `p` to the goal's target, choosing the best move at every step. One reverse Dijkstra from the target over the route costs above (time, risk, uncertainty, goal credit), so `V(here)` equals `find_route(here → target).cost`. With no reachable target, or an exit without the trophy, it is the cost to the nearest platform with an unexplored side (`explore`).
- **Option value `Q(here, a)`:** the cost of doing `a` plus `V` of where it ends.
  - A tried move ends where its edges most often landed; its cost is `edge_cost` of its outcomes from here with the held items (edges and unattributed failures summed).
  - An untried move ends at the reach estimate's end cell (`estimate_end_at`); its cost adds the skill's frame cap as time. No safe landing, or a landing off the mapped platforms, gives no `Q`.
  - An action that stays (shoot, `wait*`, a move that ends on the same platform) costs its time and its death risk, plus `V(here)`. A shot predicted to hit a monster (`threats.shot_hits`) is cheaper by the whole `graph.kill_bonus` (0.5, not calibrated). Any other shot from a platform where shots killed a monster before is cheaper by `kill_bonus` times the kill rate. Kills are recorded on the start node as `shots` and `kills` (a monster at the shot's start missing at its end; optional attributes).
  - On the target's platform, the walk to the goal tile is added (24 frames per tile), so walking toward the item beats walking away.
- **Regret** `Q − min Q`: 0 for the best option from this position.
- **Note** (first after the leading danger and route notes, at most 60 characters): `score: best (2.1 to goal; ok 4/5)`, `score: +1.3 vs best (ok 1/4, died 3)`, `score: ? (no safe landing; untried)`.
- The live viewer shows a `best` / `+regret` badge on each option and, on the card, whether the model picked the graph's best (`docs/live.md`).

## Checkpoints

- **Store layout:** a store is a directory with one checkpoint per level, `<dir>/<level_id>.json`. A store path written `X.json` means the directory `X/`. A legacy combined checkpoint at `X.json` (one graph for all levels) is split by node `level_id` on load: edges are kept only between nodes of the same level. It is then written back as the directory, so earlier learning is kept.
- **Format** (one level): versioned JSON (`graph_schema_version: 1`). It holds:
  - adapter, build_id, observation_policy, `level_id`, scenarios;
  - lineage: `[{run_id, episode_key, arm, scenario_id}]`, plus `parent_sha256` of the checkpoint it was loaded from;
  - counts, aliases, nodes, edges and suggestions.

  The JSON is sorted, so identical learning gives byte-identical files (tested on the real game).
- **Atomic save:** for each level file, write `<path>.tmp` and fsync it, copy the current file to `<path>.bak`, then `os.replace`. An interrupted save leaves the previous checkpoint intact (tested). Lineage is added to every level in the store at save time.
- **Load:** a schema, adapter, build, observation-policy or execution-mode mismatch is rejected with `GraphCheckpointError`. Learned routes do not transfer across builds.
- **Execution mode:** each checkpoint records `execution_mode`: `paused_step` (the game is paused while models think: `play`, `benchmark`, and the live viewer by default) or `real_time` (the live viewer with "Pause game while models think" off). In real time the game runs on during a model's wait, so monsters and shots move, Dave can die before the move, and moves start from a later state. The two learn different success, death and timing numbers, so they never share a store. A checkpoint written before the field existed is `paused_step`.
- **Build id** (deadly-dave, `adapters/dave.py` `build_id`): `deadly-dave-<hash>`, a hash of the game's own sources and levels (`*.c` except `bridge.c`, `include/`, `res/levels/`, line endings normalised). Rebuilding or re-patching the bridge, which only reports state, keeps it; changing the physics or the levels changes it. The bridge protocol is checked separately at the handshake. Without the sources, the bridge executable's hash is used (`deadly-dave-bridge-p<protocol>-<hash>`, the scheme before 2026-10-04).
- **Re-key:** `dave-agent graph --checkpoint DIR --rekey dave` ties a store to the current build id and keeps every node and edge. Each level file is first copied to `<level>.json.prekey`, and the adapter and observation policy must already match. Use it only when the game's physics and levels did not change, e.g. for checkpoints written under the old executable-hash id.
- **YAML:** `export_yaml` (one level) and `export_store_yaml` (every level) are for inspection only.
- **Per-arm, per-mode stores:** `dave-agent play --arm C` loads or creates `artifacts/graphs/arm-C/<adapter>/` (`level1.json`, `level2.json`, …), or `--graph PATH` / `memory.graph_checkpoint` if set. Real-time play uses `artifacts/graphs/arm-C/<adapter>-realtime/` instead (`session.graph_store_path`), so pausing or not never mixes the two. It learns when `memory.graph_updates` is true, then saves. Use one store per arm and trial; arms A and B never create or read one.

```bash
uv run dave-agent play --arm C --mock --adapter dave --scenario level1
uv run dave-agent graph --checkpoint artifacts/graphs/arm-C/dave --yaml artifacts/graphs/arm-C/dave.yaml
uv run dave-agent graph --checkpoint artifacts/graphs/arm-C/dave --route level1:r9:c2 level1:r7:c4 [--items trophy]
```

`graph` reports the totals, `per_level` counts, and routes on the level named by the node ids.

## Known limitations

- Segments above the top brick row (for example level 1 row 0) are standable by the rule. Dave can reach them only by falling and wrapping through the bottom of the screen, so they usually appear only as frontier.
- A transition's start position within a segment is not part of the edge key, so the same skill from different spots on A can reach different targets. A failure then stays at the node level, because its target is ambiguous.
- Frames are per episode, so `last_verified_frame` should be read together with its evidence refs.
