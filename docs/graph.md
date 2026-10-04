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

`RouteTracker` follows a route at decision boundaries only. It returns a replan reason in this order: `target_reached`, `edge_failed`, `inventory_changed`, `topology_changed` (when `topology_version` changed, i.e. a node or edge was added or merged), or `off_route`.

## Checkpoints

- **Store layout:** a store is a directory with one checkpoint per level, `<dir>/<level_id>.json`. A store path written `X.json` means the directory `X/`. A legacy combined checkpoint at `X.json` (one graph for all levels) is split by node `level_id` on load: edges are kept only between nodes of the same level. It is then written back as the directory, so earlier learning is kept.
- **Format** (one level): versioned JSON (`graph_schema_version: 1`). It holds:
  - adapter, build_id, observation_policy, `level_id`, scenarios;
  - lineage: `[{run_id, episode_key, arm, scenario_id}]`, plus `parent_sha256` of the checkpoint it was loaded from;
  - counts, aliases, nodes, edges and suggestions.

  The JSON is sorted, so identical learning gives byte-identical files (tested on the real game).
- **Atomic save:** for each level file, write `<path>.tmp` and fsync it, copy the current file to `<path>.bak`, then `os.replace`. An interrupted save leaves the previous checkpoint intact (tested). Lineage is added to every level in the store at save time.
- **Load:** a schema, adapter, build or observation-policy mismatch is rejected with `GraphCheckpointError`. Learned routes do not transfer across builds.
- **YAML:** `export_yaml` (one level) and `export_store_yaml` (every level) are for inspection only.
- **Per-arm stores:** `dave-agent play --arm C` loads or creates `artifacts/graphs/arm-C/<adapter>/` (`level1.json`, `level2.json`, …), or `--graph PATH` / `memory.graph_checkpoint` if set. It learns when `memory.graph_updates` is true, then saves. Use one store per arm and trial; arms A and B never create or read one.

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
