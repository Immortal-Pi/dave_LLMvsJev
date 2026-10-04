# Skills: action catalog and execution

The catalog is in `configs/skills.yaml` under `skills.catalogs.<adapter>`. The executor is `src/dave_agent/control/skills.py` and the predicates are in `src/dave_agent/control/predicates.py`. The Dave numbers below were measured by `scripts/calibrate_skills.py` on bridge build `deadly-dave-bridge-p1-f89cb497be96` (2026-10-03). Every measurement repeats exactly from a snapshot, and the full output is saved to `artifacts/calibration/skills.json`.

## Calibration (deadly-dave, verified)

| Mechanic | Measured | Source of the start state |
| --- | --- | --- |
| Walk | 2 px on the first tick of each 3-tick cycle: dx = 2 / 16 / 48 px after 1 / 24 / 72 held ticks. Dave stops on the first released tick. | level 1 start |
| Jump (tap) | Starts on the pressed tick, rises 32 px (2 tiles), and lands after 94 ticks on flat ground | level 1 start |
| Holding Up | Re-jumps after 5 ticks on the ground. **Jumps must release Up.** | level 1 |
| Landing cooldown | With Up held from the moment of landing, the new jump starts after 5 ticks. The cooldown is not observable. | level 1 |
| Air control | 1 px per tick while Left or Right is held. A full long jump gives dx = 94 px (about 6 tiles); holding for 32 ticks gives dx = 32 px. | level 3 flat floor |
| Gun | Reachable on level 3 by a scripted route. The bullet spawns 8 px ahead in the facing direction and moves 2 px per tick. Holding fire never creates a second bullet while one is alive. | level 3 |
| Climbing | Pressing Up on a tree trunk starts a *jump*, not a climb, so entering a climb needs an unmeasured input sequence. **Unverified: not in the catalog.** | level 5 trunk at column 4 |
| Burning | Burning lasts 200 ticks before the death event (level 2 fire), and inputs are ignored meanwhile | level 2 |
| Jetpack | Not measured. **Not in the catalog** (the spec requires its fuel and control semantics to be verified first). | — |

Dave's hitbox is 20 px wide, wider than a tile. Starting a long jump right next to a fire tile can still touch it during the slow initial rise.

## Execution model

- A skill is a list of **phases**. Each phase holds its `buttons` either for a fixed `ticks` count, or until a predicate named in `until` holds, with at most `max_ticks`.
- **Release:** buttons are released when a phase ends. The executor calls `adapter.step(buttons, 1)` once per frame, and the bridge holds keys only within a step, so no key can stay held after a skill.
- **Hard cap:** `max_frames` is the sum of the phase budgets and must be ≤ 600. Input ticks never exceed it (asserted).
- **Outcomes:**

  | Outcome | Meaning |
  | --- | --- |
  | `completed` | Every phase finished. |
  | `interrupted` | An interrupt rule fired (the reason is recorded). |
  | `failed` | An `until` phase ran out (`phaseN_timeout:<predicate>`). |
  | `rejected` | Revalidation failed, so no input was sent. |

  Each skill emits `skill_started` and `skill_finished` events, with the outcome and reason in the payload.
- **Interrupt rules** (`interrupt_on`), checked after every frame:

  | Rule | Fires when |
  | --- | --- |
  | `death` | A death event occurs. |
  | `terminal` | The level ends. |
  | `hazard_contact` | Dave starts burning; death is then certain and input is ignored. |
  | `new_hazard_nearby` | A monster or plasma that was not visible at skill start comes within `executor.hazard_radius_px` (48 px) of Dave on both axes. |
  | `threat_incoming` | While Dave stands, a monster or plasma is predicted to touch him within `executor.threats.interrupt_ticks` (16) if he stays put. Threats already predicted when the skill started are skipped, because the choice saw them (see "Threat prediction"). |

  The rules are identical for every arm.
- **Revalidation:** before the first input, the candidate's preconditions are re-checked on the latest observation. A candidate whose `max_frames` differs from the catalog is also rejected.
- **No reflexes:** the executor never chooses inputs of its own. Avoiding enemies is the controller's job; the threat screen below only removes options.
- **Paused mode** (default): the game advances only inside `execute`, so it is frozen while a model decides.
- **Real-time mode** (Phase 11): a stale decision will be replaced by `stale_fallback()`, the first offered candidate that presses no buttons (a wait).
- **Forced decisions:** when exactly one candidate is legal (for example `wait_long` while burning), the runner picks it without a model call and marks it `Decision.forced=True`. This applies to every arm.

## Legal-action masks

`generate_candidates(catalog, adapter_buttons, observation)` returns a `CandidateSet`:
- the legal candidates in catalog order, with IDs `c{i}_{skill}`;
- `masked` (`skill → first failed precondition`, or `unsupported_buttons:…`);
- a `digest()` used to prove cross-arm parity.

A predicate whose field is unavailable returns False, so the skill is masked rather than guessed.

| Predicate | True when |
| --- | --- |
| `alive` | `player_state` is standing, walking, jumping, climbing, freefalling or jetpacking |
| `on_ground` | standing or walking |
| `jumping` | `player_state == jumping` |
| `landed` | grounded **and** standing or walking |
| `has_gun` | `inventory.gun > 0` |
| `facing_side` | `facing` is left or right (the gun cannot fire while facing front) |
| `no_bullet` | there is no `bullet` entity. A live bullet is always inside the viewport, because bullets die at the screen edge. |

## Threat prediction and the candidate screen

`src/dave_agent/control/threats.py`, settings in `skills.executor.threats` (`configs/skills.yaml`). It is shared by every arm and uses only the observation and the goal manager's memory of seen cells.

- **Dave's path:** `control/reach.py` `trace_skill` gives Dave's pixel position after every tick of a skill. It reads the skill's phases and follows the game's rules (dave.c):
  - the jump arc, with 1 px/tick air control;
  - the ceiling test at x+4 and x+9, which a jump slips past when the held direction clears it 2 px ahead;
  - side tests between y+2 and y+15;
  - walking at 2 px per 3 ticks;
  - free fall at 1 px/tick. Entering a fall faces Dave front. A key turns him, and from then on he drifts 1 px/tick that way with no key held.

  A skill that ends in the air is followed until Dave lands. A short jump stopped early by a ceiling keeps walking for the rest of its hold.
- **Threats:**
  - Plasma flies straight at 2 px/tick (plasma.c) until its leading edge enters a brick. Before its velocity is known, it is assumed to fly toward Dave.
  - Monsters are extrapolated linearly from their per-tick velocity. Their box grows 1 px per `monster_growth_ticks` ahead.
  - Plasma is checked over the whole path, monsters within `horizon_ticks`. After the path ends, Dave is assumed to stand still until the look-ahead ends.
- **Boxes** (the game's collision boxes, grown by `margin_px`):
  - Dave: x+2, y+2, 14×16;
  - plasma: 20×3;
  - monsters: 24×21;
  - hazard cells: x+4..x+12 at full height, which covers fire and vines (water is smaller).
- **Screen:** `assess` returns each candidate's first contact. `GoalManager.annotate` leads the description with `danger: touches plasma in 14 ticks` (or ends it with `no threat predicted`). It then drops every candidate with a contact, unless all have one, in which case the candidates whose contact comes latest are kept. The drop is recorded as a `candidates_screened` event (`{masked: {id: "threat:<what>@<tick>"}, kept}`). The offered set and its digest are taken before the screen, which depends only on shared observations, so it replays exactly. It applies while Dave stands on a known cell or free-falls. While falling, both the drifting and non-drifting case are tried, because the observation does not tell them apart. Mid-jump it does not apply, because the remaining arc is unknown.
- **Checked against the real game** (2026-10-03, 300 random catalog skills on levels 1–3):
  - every one of the 8 burns was predicted, with no false alarms;
  - the end position matched within 2 px for 88% of skills.
  - Long mock runs on levels 2 and 3 (18,000 frames each) went from 4 and 2 deaths to none.

## Dave catalog

| Skill | Kind | Preconditions | Phases (buttons → length) | Max frames |
| --- | --- | --- | --- | --- |
| `move_left_1` / `move_right_1` | single | alive | dir → 24 ticks (1 tile) | 24 |
| `move_left_3` / `move_right_3` | single | alive | dir → 72 ticks (3 tiles) | 72 |
| `jump_up` | macro | on_ground | jump → until `jumping` (≤ 6); none → until `landed` (≤ 200) | 206 |
| `jump_left` / `jump_right` | macro | on_ground | jump → until `jumping` (≤ 6); dir → until `landed` (≤ 200) | 206 |
| `jump_left_short` / `jump_right_short` | macro | on_ground | jump → until `jumping` (≤ 6); dir → 32; none → until `landed` (≤ 200) | 238 |
| `shoot` | single | alive, has_gun, facing_side, no_bullet | fire → 1 tick | 1 |
| `wait_short` | single | alive | none → 6 ticks (covers the 5-tick landing cooldown) | 6 |
| `wait_long` | single | — (always legal) | none → 24 ticks | 24 |

- All Dave skills interrupt on `death`, `terminal` and `new_hazard_nearby`; walks and waits also on `threat_incoming`.
- `move_*`, `jump_*` and `wait_short` also interrupt on `hazard_contact`.
- `shoot` interrupts on `death` and `terminal` only.
- `wait_long` omits `hazard_contact`, so it can wait out a burn.
- The 200-tick landing budget covers a 94-tick flat jump plus falls to lower ground. If Dave has not landed by then, the skill ends `failed`.

The fixture catalog keeps its Phase 1 skills (1–3 frame holds), so fixture results are unchanged.

## Known limitations

- Episodes are truncated at the first decision point after `max_episode_frames`, so an episode can overrun by up to one skill (≤ 238 frames).
- The landing cooldown is hidden state, so a jump requested within 5 ticks of landing spends those ticks in its first phase. Sideways jumps therefore press **only Up** until the jump starts and add the direction afterwards (changed 2026-10-03). Holding the direction during the cooldown walked Dave off 1-tile pillars before takeoff (level 1). Re-measured: flat-ground dx (94 / 32 px) and frames are unchanged, and `calibrate_skills.py` passes 12/12.
- **Level 1 is completable with the catalog** (`scripts/try_skills.py`, deterministic, 554 frames):
  `move_right_3 move_right_3 move_right_1 jump_right jump_right jump_left jump_right move_right_3 move_left_3 move_left_3`.
  This walks right, jumps onto the row-8 platform (11,7) and the pillar (13,5), takes the trophy from the pillar (11,3), lands at (16,7), drops through the gap at column 17 and walks left into the door.
- **Level 2 is completable with the catalog** (`scripts/search_route.py --scenario level2`, a breadth-first search over catalog skills on the real game; 37 skills, 1928 frames, route in `artifacts/search/level2-tile.json`). It starts with a zig-zag climb of alternating short hops right and left, and needs backtracking after the trophy.
- Every recent-history entry shown to the planner and tactical models carries `moved_px`, the observed displacement. `[0, 0]` marks a skill that did not move Dave: a walk into a wall, or a jump blocked by the tile above. Those skills still report `completed`, because nothing failed.
- Climbing and jetpack skills are absent until verified.
