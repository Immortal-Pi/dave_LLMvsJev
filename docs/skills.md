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
  | `new_hazard_nearby` | A monster or plasma that was not visible at skill start comes within `executor.hazard_radius_px` (48 px) of Dave on both axes. The shots of monsters whose motion is known do not count (`threats.scripted_shots`): the choice already saw them. On level 4 a jump chosen to dodge the swirl's next shot was stopped before take-off when that shot appeared. |
  | `threat_sighted` | Walks and jumps: a monster or plasma that was not in view at skill start appears anywhere on screen (the screen scrolled, or it came in from the side). Dave gets a decision there, mid-air included: shoot, steer, or fly on. Level 3: jumps across the screen edge met the spider, unseen at take-off. |
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

- **Dave's path:** `control/reach.py` `trace_skill` gives Dave's pixel position after every tick of a skill. It reads the skill's phases and follows the game's rules (dave.c), tick for tick:
  - **Take-off:** a jump starts one still tick after Up, and after a landing the game's jump cooldown (5) adds four more (`reach.takeoff_delay`, `Observation.jump_cooldown` from the bridge). Before 2026-10-04 every predicted jump was one tick early, and up to five after a landing.
  - **The jump arc:** y first, then x. With a key held, Dave moves 2 px every other tick, starting on the first (the walk_state cooldown); before, 1 px every tick.
  - **The ceiling test** is at x+4 and x+9, after each 1 px rise; a jump slips past when the held direction clears it 2 px ahead. On a bump there is no sideways step that tick, and the next tick Dave lands 2 px lower if there is ground there, or starts to fall.
  - **Walls:** only the leading edge is tested (x+12 before a step right, x+1 before a step left; `body_px: [2, 11]`, the left edge was +4 before), between y+2 and y+15. Rising past a brick beside him does not stop Dave moving away from it.
  - **Ground** is tested at x+4 .. x+9 (`foot_px: [4, 9]`; it was [4, 8], and Dave standing at x 359 on level 4 was taken for falling, so no threat was screened there).
  - **Walking** at 2 px per 3 ticks, the 2 px on the first tick of each 3.
  - **Free fall** at 1 px/tick: the ground test, the fall, then the drift. Entering a fall faces Dave front. A key turns him, and from then on he drifts 1 px/tick that way with no key held.

  `scripts/audit_threats.py` runs skills on the real game and prints, per skill, the first tick where Dave, a monster or its plasma differs from the prediction (`--patient` waits while the next skill is masked). Along the recorded level 4 path (68 skills) Dave is within 1 px except one 149-tick jump (2 px), and the swirl and its shots match exactly.

  A skill that ends in the air is followed until Dave lands. A short jump stopped early by a ceiling keeps walking for the rest of its hold.
  - **Mid-jump** (the observation's `jump_tick`, dave.c `jump_state`): `trace_in_jump` continues the arc from that tick, steered by the skill's direction for its ticks, and a walk keeps walking after the landing. Before, decisions taken mid-jump were not screened at all (level 3: `move_left_3` chosen 8 ticks before landing walked Dave into a vine). Checked on the real game: within 2 px until a hazard contact.
- **Threats:**
  - **Monsters replay monster.c** when the bridge reports their state (`Entity.motion`, bridge protocol 2): one route step `(dx, dy)` every 5 ticks (the step cooldown counts 0..4), routes passing through walls by design.
  - **Shots not fired yet are predicted too.** A monster fires when its last plasma is gone and `ticks_before_shoot` has counted down from `fire_rate` (level 4's swirl: 5 ticks, so there is nearly always a shot in its line). The plasma spawns at the monster's `x - 8` (Dave to the right) or `x - 21` (left), `y + 8`, toward the side Dave's path is on at that tick, and moves 2 px in the same tick. Contacts with such a shot read `danger: the next shot hits in N ticks`.
  - **Plasma** flies at 2 px/tick until a point 2 px behind or 20 px ahead of it (both are tested, plasma.c) enters a brick, or it is 80 px beyond the screen. Before its direction is known, it is assumed to fly toward Dave.
  - Checked on the real game (`tests/integration/test_dave_bridge.py`): level 4's swirl and every plasma it fires over 120 ticks match exactly, including shots fired after the forecast.
  - Without motion data (the fixture), monsters are extrapolated linearly from their per-tick velocity, their box growing 1 px per `monster_growth_ticks` ahead.
  - Plasma and monsters whose motion is known are checked over the whole path, other monsters within `horizon_ticks`. After the path ends, Dave is assumed to stand still until the look-ahead ends, and plasma is checked for `interrupt_ticks` (16) more after the landing. Before, a 49-tick jump onto (26,3) on level 4 met the swirl 5 ticks after landing, past the horizon.
  - **Shots past the screen edge** may die on a wall Dave has not seen, and the monster then fires its next shot sooner: each scripted monster is forecast both ways (unseen cells as bricks for its shots, or open) and the earlier contact counts. In the live run `paid-l4-live` the swirl's shot flying right died on the unseen wall at column 35, and the next one hit Dave mid-jump 48 ticks into a `jump_left` predicted safe.
  - **Traps:** a move that ends standing where every next move is hit, and standing is not safe for 48 ticks, gets a `trap` contact (`danger: every move after it is hit (the first in 22 ticks)`). On level 4 the jump onto (23,5) is safe, but every move from there runs into the swirl's next shot.
  - The `threat_incoming` interrupt treats a monster's shot as one threat whether it is flying (`plasma<n>`) or still to come (`shot<n>`), so a shot expected when a skill started does not interrupt it when fired.
- **Boxes** (the game's collision boxes, grown by `margin_px`):
  - Dave: x+2, y+2, 14×16;
  - plasma: 20×3;
  - monsters: 24×21;
  - hazard cells: x+4..x+12 at full height, which covers fire and vines (water is smaller).
- **Past the screen edge:** unseen cells are open air for these paths (`ReachMap.opened`), and nothing is predicted from the tick a path scrolls the screen (game.c: Dave's x more than 280 px into it, or less than 30) or enters an unseen cell. The game pauses while it scrolls and what is beyond is unknown. Such a path gets an `edge` contact, noted (`passes the screen edge at tick 18: the screen scrolls, what is beyond is unseen`, after any route note) and never masked. Before, unseen cells were walls: on level 4's ledge at (31,3) every move right was simulated bouncing off an imaginary wall into the fire below, masked, and Dave paced there for 75 decisions. Routes still keep to seen cells.
- **Timing** (`assess_timing`, while Dave stands with threats around): every moving skill is also judged after standing 6, 12 ... 48 ticks (`wait_short` steps).
  - A move that is safe now but not later reads `timing: go now, unsafe if started 18 or more ticks later`.
  - A wait is safe when its own ticks are and a moving skill is safe after it, not only when standing still for the whole look-ahead is: `timing: standing is safe for 31 ticks; then safe: jump_up, jump_left; later: jump_right from 24 ticks`. A wait with no move safe after it keeps the standing contact.
- **Shots** (with the gun): `shoot` reads `shot: hits the swirl in 9 ticks`, `shot: misses (hits a wall in 12 ticks)` or `shot: facing away from the swirl`. The bullet spawns at x+8 or x-8, y+8, moves 2 px/tick and stops at a brick (bullet.c tests x+10 going right, x-2 going left) or the screen edge; it is tested against the monsters' predicted motion. It passes through plasma (game.c tests bullets against monsters only). Bullets are unlimited, one on screen at a time.
- **A predicted kill:** `shoot` is judged without the monster it hits from the tick of the hit: a burning monster neither touches Dave nor fires again (monster.c); the shot already flying goes on (`first_contact(killed=...)`).
- **Mid-air** (a jump stopped by `threat_sighted` or `new_hazard_nearby`): the options are the walks (they steer), the waits (fly on) and `shoot`. Each gets its predicted contact over the rest of the arc, its landing tile, and the shot note; the ones landing cheapest toward the goal get `route: lands on [21, 6], the best landing toward the goal` (`GoalManager._air_notes`), so the plan goes on after a shot.
- **Screen scrolls freeze the game:** for 16 ticks (the trigger tick, then 15 columns) the game moves nothing and ignores every key (game.c `game_adjust_scroll_to_dave`). Those ticks do not count toward a skill's phases (`ExecutionResult.frozen_ticks`, at most 64 per skill), or a fixed hold lets go early: on level 3 `jump_right_5` across the screen edge dropped into the fire short of the pillar. `shoot` needs `screen_still`: eight shots pressed mid-air during a scroll fired nothing.
- **Death cause:** `contact_cause` checks every visible entity at its own position. A monster's plasma used to be folded into the monster, and every level 4 death was logged as `unknown`.
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
| `step_off_left` / `step_off_right` | macro | on_ground | dir → until `airborne` (≤ 72); none → until `landed` (≤ 200) | 272 |
| `jump_up` | macro | on_ground | jump → until `jumping` (≤ 6); none → until `landed` (≤ 200) | 206 |
| `jump_left` / `jump_right` | macro | on_ground | jump → until `jumping` (≤ 6); dir → until `landed` (≤ 200) | 206 |
| `jump_left_short` / `jump_right_short` | macro | on_ground | jump → until `jumping` (≤ 6); dir → 32; none → until `landed` (≤ 200) | 238 |
| `jump_left_4` / `jump_right_4` | macro | on_ground | jump → until `jumping` (≤ 6); dir → 64; none → until `landed` (≤ 200) | 270 |
| `jump_left_5` / `jump_right_5` | macro | on_ground | jump → until `jumping` (≤ 6); dir → 80; none → until `landed` (≤ 200) | 286 |
| `jetpack_on` | single | alive, has_fuel, not_jetpacking, screen_still | jetpack → 1 tick | 1 |
| `jetpack_off` | single | jetpacking, screen_still | jetpack → 1 tick (Dave then falls) | 1 |
| `fly_up_1` / `fly_down_1` / `fly_left_1` / `fly_right_1` | single | jetpacking | jump (up) / down / left / right → 16 ticks (1 tile) | 16 |
| `fly_up_3` / `fly_down_3` / `fly_left_3` / `fly_right_3` | single | jetpacking | the same → 48 ticks (3 tiles) | 48 |
| `fly_up_nudge` / `fly_down_nudge` / `fly_left_nudge` / `fly_right_nudge` | single | jetpacking | the same → 2 ticks (to line up with a gap) | 2 |
| `shoot` | single | alive, has_gun, facing_side, no_bullet, screen_still | fire → 1 tick | 1 |
| `wait_short` | single | alive | none → 6 ticks (covers the 5-tick landing cooldown) | 6 |
| `wait_tick` | single | alive | none → 2 ticks | 2 |
| `wait_long` | single | — (always legal) | none → 24 ticks | 24 |

- All Dave skills interrupt on `death`, `terminal` and `new_hazard_nearby`; walks and waits also on `threat_incoming`.
- `move_*`, `jump_*` and `wait_short` also interrupt on `hazard_contact`.
- `shoot` interrupts on `death` and `terminal` only.
- `wait_long` omits `hazard_contact`, so it can wait out a burn.
- The 200-tick landing budget covers a 94-tick flat jump plus falls to lower ground. If Dave has not landed by then, the skill ends `failed`.

**Stepping off** (added 2026-10-04): walk to the platform's end, then let go, so Dave drops straight down (entering a fall faces him front, and with no key he does not drift; dave.c). A walk that keeps its key held drifts on while falling: on level 4 from (64,5) one walk stops 4 px short of the edge, and from closer the walk drops and drifts past (65,6) into the pit. The person let go mid-fall. `trace_skill` predicts it tick by tick (the step past the edge, a tick entering the fall, then 1 px a tick); on the real game it matched.

**Waiting for the safe moment** (added 2026-10-04): when the move the route wants is blocked now, `threats.safe_window` searches every 2 ticks up to `FIRE_WINDOW` (160, a firing cycle and a shot's crossing) for the first start at which it is safe, and how long it stays safe. The longest safe wait that does not pass that moment gets `route: wait — jump_right is safe in 30 ticks (for 12 ticks)`, and the move itself `timing: safe from 30 ticks (for 12)` (`GoalManager._wait_notes`). `wait_tick` (2 ticks) fits short moments.

**Self-sacrifice, the last resort** (added 2026-10-04): touching a monster burns both, and the monster stays dead: after a death the game resets only Dave and the scroll to the level start (game.c `G_STATE_LEVEL_START`), not the monsters, and collected items stay collected. When no option has carried a route note for `SACRIFICE_FRAMES` (1200) and Dave has at least 2 lives, the moves whose first contact is a monster's body (not its plasma, which kills only Dave) are no longer masked and get `route: last resort (stuck 1200 frames) — collides with the swirl in 14 ticks: it is destroyed for good; Dave loses a life (2 left) and restarts at the level start, keeping what he collected` (`GoalManager._sacrifice_notes`).

**Forecast misses** (added 2026-10-04): when a move the threat screen predicted safe ends in a death by plasma or a monster, the goal manager records it for that tile and skill. The option then carries `forecast missed here before: hit 1x (plasma) though predicted safe`, the tile costs more in routes, a `forecast_miss` event is logged, and on graph arms the incident is marked `predicted: safe`. `scripts/audit_threats.py` prints `FORECAST MISS`. Nothing is masked.

**The jetpack** (added 2026-10-04; dave.c `dave_state_jetpacking_routine`). The jetpack key turns it on from standing, walking, jumping or falling while there is fuel, and off again (Dave then falls). On, Dave hovers with no gravity and moves 1 px a tick along one axis: left before right before up (the jump key) before down; down stops on the floor; walls and ceilings stop him with the same leading-edge and head tests as walking and jumping. Every tick burns one bar of fuel, moving or not (900 a pickup); at 0 the jetpack switches off. The walks need `not_jetpacking` (the same keys fly). `reach.trace_flying` predicts a flight tick by tick; on the real game (level 3, from the jetpack pickup: up 3, up 1 against the ceiling, left, a nudge, up, right 3 against a wall, down) it matched every tick. The threat screen judges flights with it, and the route notes guide them (`docs/planner.md`).

The 4- and 5-tile jumps (added 2026-10-04, `reach.mid_hold_ticks: [64, 80]`) come from human play (`scripts/compare_play.py`). Level 3 is a row of pillars between fire pits, crossed in 4-5 tile jumps. The long jump overshoots into the next pit and the short one falls short, so from many pillars no catalog jump was safe. The person held the direction 58-86 ticks; holds of 64 and 80 land within 8 px of 23 of those 25 jumps. The reach estimate, routes and in-flight grabs use every hold (`ReachMap.shapes`). On the real game, `follow_route.py` crosses level 3's pillars with them, and level 2 completes in 3793 frames (6549 before), taking the trophy in flight with `jump_right_5` from (11,6).

The fixture catalog keeps its Phase 1 skills (1–3 frame holds), so fixture results are unchanged.

## Comparing with human play

`scripts/record_play.py --scenario level4` lets a person play in the bridge's viewer window (arrows or WASD, Up to jump, Ctrl/Space to fire, Alt for the jetpack, Esc to stop; `--delay 30` for slow motion) and writes one line per tick with the keys held to `artifacts/human/`. The game is deterministic, so the key log is the whole run.

`scripts/compare_play.py <log>` replays it headless and reports what the catalog cannot do:
- each jump, from Up on the ground to the landing, against every catalog jump traced from the same start, and against "hold the direction h ticks". A jump is labelled `catalog` (a skill lands within `--tolerance` px), `hold length` (only a different hold matches), `mid-air reversal`, `re-press in the air`, `other` or `died`. The threat screen's verdict at the take-off is shown, so a jump the person survived but the screen masks stands out;
- walk and wait lengths on the ground against the catalog's 24/72 and 6/24 ticks;
- fire standing, walking or in the air;
- deaths with `contact_cause`;
- how often the keys changed on the ground and in the air (the agent never changes them in the air).

## Known limitations

- Episodes are truncated at the first decision point after `max_episode_frames`, so an episode can overrun by up to one skill (≤ 238 frames).
- The landing cooldown is hidden state, so a jump requested within 5 ticks of landing spends those ticks in its first phase. Sideways jumps therefore press **only Up** until the jump starts and add the direction afterwards (changed 2026-10-03). Holding the direction during the cooldown walked Dave off 1-tile pillars before takeoff (level 1). Re-measured: flat-ground dx (94 / 32 px) and frames are unchanged, and `calibrate_skills.py` passes 12/12.
- **Level 1 is completable with the catalog** (`scripts/try_skills.py`, deterministic, 554 frames):
  `move_right_3 move_right_3 move_right_1 jump_right jump_right jump_left jump_right move_right_3 move_left_3 move_left_3`.
  This walks right, jumps onto the row-8 platform (11,7) and the pillar (13,5), takes the trophy from the pillar (11,3), lands at (16,7), drops through the gap at column 17 and walks left into the door.
- **Level 2 is completable with the catalog** (`scripts/search_route.py --scenario level2`, a breadth-first search over catalog skills on the real game; 37 skills, 1928 frames, route in `artifacts/search/level2-tile.json`). It starts with a zig-zag climb of alternating short hops right and left, and needs backtracking after the trophy.
- Every recent-history entry shown to the planner and tactical models carries `moved_px`, the observed displacement. `[0, 0]` marks a skill that did not move Dave: a walk into a wall, or a jump blocked by the tile above. Those skills still report `completed`, because nothing failed.
- Climbing and jetpack skills are absent until verified.
