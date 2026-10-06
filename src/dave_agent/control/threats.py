"""Threat prediction: where plasma and monsters will be, and whether a skill's path meets them.

Shared by every arm and computed from the observation alone (docs/skills.md):

- Plasma flies straight at 2 px/tick (plasma.c) until a point 2 px behind or 20 px ahead of it
  enters a brick, or it leaves the screen by 80 px. With no direction yet (first sighting) it
  is assumed to fly toward Dave, as the game fires it.
- Monsters replay the game's own rule (monster.c) when the adapter reads their state
  (``Entity.motion``): one route step every 5 ticks, and a new plasma as soon as the last one is
  gone and ``ticks_before_shoot`` has counted down from ``fire_rate``, spawned at the monster's
  ``x - 8`` (Dave to the right) or ``x - 21`` (left), ``y + 8``. So shots not fired yet are
  predicted too (``shot<n>``), toward the side Dave is on at that tick of his path. Monsters fly
  through walls by design; shots stop at them. Without motion data a monster is extrapolated
  linearly from its derived velocity, its box growing by 1 px per ``monster_growth_ticks``.
- Dave's path comes from the reach simulation (control/reach.py ``trace_skill``); after the
  skill ends he is assumed to stand still until the look-ahead ends.
- Boxes are the game's collision boxes: Dave x+2, y+2, 14x16 (dave.c), plasma 20x3, monsters
  about 24x21 (monster.c), and the hazard part of a fire, vine or water cell (tile.c), each
  grown by ``margin_px``.

Unseen cells (past the screen edge) are open air for these paths, and nothing is predicted
after a path scrolls the screen or leaves the observed map: the game pauses while it scrolls
and what is beyond is unknown. Such a path gets an ``edge`` contact, which does not mask.

Timing (``assess_timing``): each moving skill is also tried after standing 6, 12 ... 48 ticks
(``wait_short`` steps). A wait is judged by its own ticks and by whether a moving skill is safe
after it, not by standing still for the whole look-ahead; the notes say when to go.

``assess`` returns the first contact for every candidate; ``screen`` drops the candidates
that touch something unless all of them do, then keeps the ones that touch latest.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from dave_agent.config import SkillSpec, ThreatConfig
from dave_agent.control.reach import DAVE_BOX, HAZARD_BOX, TILE, ReachMap, trace_flying, trace_in_jump, trace_skill
from dave_agent.schemas import Observation, SkillCandidate

Box = tuple[int, int, int, int]  # dx, dy, width, height from the sprite's top-left pixel
PLASMA_BOX: Box = (0, 0, 20, 3)
MONSTER_BOX: Box = (0, 0, 24, 21)
# DAVE_BOX and HAZARD_BOX (reach.py): the hazard box is, within a 16 px cell, the union of the
# game's boxes (tile.c): fire x+6 w4 and vines x+4 w8 are full height, water x+6 w4 y+4 h8.
# Hazard kinds are not told apart in the observation.
PLASMA_SPEED = 2
HARMLESS = frozenset({"bullet"})  # Dave's own shot
STEP_EVERY = 5  # monster.c: a route step when the cooldown, counting 0..4, is 0
SHOT_SPAWN = {1: (-8, 8), -1: (-21, 8)}  # plasma_create_right / _left offsets from the monster
SCREEN_PX = 320
DEADZONE_PX = 80  # a plasma dies this far beyond the screen (game.c game_do_plasmas)
STANDING_STATES = frozenset({"standing", "walking"})
FACING = {"left": -1, "right": 1}
FALLING_STATES = frozenset({"freefalling"})  # steerable in the air: walks and waits can still dodge
# game.c game_adjust_scroll_to_dave: the screen scrolls 15 columns once Dave's x is more than
# 280 px into it, or less than 30 px (objects stand still while it scrolls).
SCROLL_RIGHT_PX = 280
SCROLL_LEFT_PX = 30
WAIT_STEP = 6  # wait_short: the timing notes try moving skills after this many standing ticks
MAX_DELAY = 48  # ... up to this many
FIRE_WINDOW = 160  # the planned move's safe moment is searched this far ahead: a firing cycle and a shot's crossing
WINDOW_STEP = 2  # ... every this many ticks (wait_tick)
BULLET_SPEED = 2  # game.c: Dave's bullet spawns at x+8 (right) or x-8 (left), y+8, and moves 2 px/tick
BULLET_BOX: Box = (0, 0, 2, 2)


@dataclass(frozen=True)
class Threat:
    entity_id: str
    entity_type: str
    x: int
    y: int
    dx: int  # px per tick
    dy: int
    motion: dict | None = None  # a monster's scripted motion (Entity.motion), when known


@dataclass(frozen=True)
class Contact:
    """The first predicted contact of a path: with an entity, or with a hazard cell."""

    what: str  # entity id, "hazard", or "edge" (the path scrolls the screen or leaves the map)
    kind: str  # plasma | monster type | hazard | unknown
    tick: int

    @property
    def blocks(self) -> bool:
        """A predicted touch; an ``edge`` contact only says the path goes where nothing is known."""
        return self.kind != "unknown"

    def note(self) -> str:
        if not self.blocks:
            return f"passes the screen edge at tick {self.tick}: the screen scrolls, what is beyond is unseen"
        if self.what == "trap":
            return f"danger: every move after it is hit (the first in {self.tick} ticks)"
        if self.what.startswith("shot"):
            return f"danger: the next shot hits in {self.tick} ticks"
        return f"danger: touches {self.what if self.kind == 'hazard' else self.kind} in {self.tick} ticks"

    def reason(self) -> str:
        return f"threat:{self.what}@{self.tick}"


def _overlap(ax: int, ay: int, a: Box, bx: int, by: int, b: Box, margin: int) -> bool:
    x1, y1 = ax + a[0] - margin, ay + a[1] - margin
    x2, y2 = bx + b[0], by + b[1]
    return x1 < x2 + b[2] and x1 + a[2] + 2 * margin > x2 and y1 < y2 + b[3] and y1 + a[3] + 2 * margin > y2


def threats(obs: Observation) -> list[Threat]:
    """Every visible entity that can burn Dave, with its per-tick motion."""
    if obs.player_position is None:
        return []
    out = []
    # A monster whose motion is known carries its plasma: both are simulated together.
    owned = {"plasma" + e.entity_id[len("monster"):] for e in obs.entities
             if e.visible and e.motion is not None and e.entity_id.startswith("monster")}
    for e in obs.entities:
        if not e.visible or e.entity_type in HARMLESS or e.entity_id in owned:
            continue
        v = e.velocity
        if e.entity_type == "plasma":
            toward = 1 if obs.player_position.x > e.position.x else -1
            if e.motion is not None and e.motion.get("dx"):
                dx = PLASMA_SPEED if e.motion["dx"] > 0 else -PLASMA_SPEED
            else:
                dx = (PLASMA_SPEED if v.dx > 0 else -PLASMA_SPEED) if v is not None and v.dx else PLASMA_SPEED * toward
            out.append(Threat(e.entity_id, "plasma", e.position.x, e.position.y, dx, 0))
        else:
            out.append(Threat(e.entity_id, e.entity_type, e.position.x, e.position.y,
                              v.dx if v else 0, v.dy if v else 0, e.motion))
    return out


def deadzone(obs: Observation) -> tuple[int, int]:
    """Pixel x range outside which a plasma dies: the screen widened by 80 px each side."""
    left = obs.region.min.col * TILE
    return left - DEADZONE_PX, left + SCREEN_PX + DEADZONE_PX


def _plasma_blocked(cells: dict[tuple[int, int], str], x: int, y: int, unseen_walls: bool = False) -> bool:
    """plasma.c: both collision points are tested whichever way it flies. With ``unseen_walls``
    an unseen cell counts as a brick."""
    return _brick(cells, x + 20, y + 1, unseen_walls) or _brick(cells, x - 2, y + 1, unseen_walls)


@dataclass
class Forecast:
    """A monster's predicted top-left per tick, and its plasma's (None while there is none) with
    which shot it is: 0 the plasma already flying, 1.. the shots fired after it."""

    monster: list[tuple[int, int]]
    shots: list[tuple[int, int] | None]
    shot_no: list[int]


def simulate(t: Threat, dave_xs: list[int], ticks: int, cells: dict[tuple[int, int], str],
             zone: tuple[int, int], unseen_walls: bool = False) -> Forecast:
    """Replay monster.c tick by tick for ``ticks`` ticks: Dave's x per tick (``dave_xs``, the
    last one held) decides which way each new shot flies."""
    m = t.motion or {}
    steps, cooldown = m.get("steps") or [[0, 0]], m.get("cooldown", 0)
    shoot_in, rate = m.get("shoot_in", 0), m.get("fire_rate", 0)
    p = m.get("plasma")
    shot = [p["x"], p["y"], p["dx"], p["dead"], 0] if p is not None else None
    x, y, k, fired = t.x, t.y, 0, 0
    out = Forecast([], [], [])
    for tick in range(1, ticks + 1):
        dave_x = dave_xs[min(tick, len(dave_xs)) - 1] if dave_xs else x
        if cooldown > STEP_EVERY - 1:
            cooldown = 0
        if cooldown == 0:
            dx, dy = steps[k % len(steps)]
            x, y, k = x + dx, y + dy, k + 1
        cooldown += 1
        if shot is None:
            if shoot_in == 0:
                side = 1 if dave_x > x else -1
                fired += 1
                shot = [x + SHOT_SPAWN[side][0], y + SHOT_SPAWN[side][1], PLASMA_SPEED * side, False, fired]
            else:
                shoot_in -= 1
        else:
            shoot_in = rate
        if shot is not None:
            if shot[3]:
                shot = None  # destroyed this tick (game_do_plasmas)
            else:
                shot[0] += shot[2]
                if shot[0] >= zone[1] or shot[0] <= zone[0] or _plasma_blocked(cells, shot[0], shot[1], unseen_walls):
                    shot[3] = True
        out.monster.append((x, y))
        alive = shot is not None and not shot[3]
        out.shots.append((shot[0], shot[1]) if alive else None)
        out.shot_no.append(shot[4] if alive else -1)
    return out


def _brick(cells: dict[tuple[int, int], str], x: int, y: int, unseen_walls: bool = False) -> bool:
    kind = cells.get((x // TILE, y // TILE))
    return kind == "solid" or (unseen_walls and kind is None)


def positions(threat: Threat, ticks: int, cells: dict[tuple[int, int], str],
              zone: tuple[int, int] | None = None) -> list[tuple[int, int] | None]:
    """Predicted top-left after ticks 1..ticks; None once a plasma has hit a brick or left the
    screen. A monster with known motion follows its route."""
    if threat.motion is not None:
        return list(simulate(threat, [], ticks, cells, zone or (-10**6, 10**6)).monster)
    out: list[tuple[int, int] | None] = []
    x, y, alive = threat.x, threat.y, True
    for _ in range(ticks):
        if alive:
            x, y = x + threat.dx, y + threat.dy
            if threat.entity_type == "plasma":
                alive = not _plasma_blocked(cells, x, y) and (zone is None or zone[0] < x < zone[1])
        out.append((x, y) if alive else None)
    return out


def first_contact(path: list[tuple[int, int]], found: Iterable[Threat], cells: dict[tuple[int, int], str],
                  cfg: ThreatConfig, zone: tuple[int, int] | None = None, until: int | None = None,
                  settle: int = 0, killed: Mapping[str, int] | None = None) -> Contact | None:
    """The earliest contact along ``path`` (Dave's top-left after ticks 1..n), then standing at its
    end until the look-ahead ends. Plasma (flying or still to be fired) and monsters whose motion
    is known are checked over the whole path, other monsters only within ``horizon_ticks``. Nothing is predicted from tick ``until`` on (the
    path scrolls the screen or leaves the map): an ``edge`` contact then, if nothing came before.
    Plasma is also checked ``settle`` ticks after the path ends, past the look-ahead if need be:
    a move that lands where the next shot hits before Dave can move again is not safe (level 4:
    a jump_left judged safe landed on (21,5) 8 ticks before a shot, and every move from there
    was hit). ``killed`` maps a monster to the tick Dave's bullet sets it burning: from then on
    it neither touches him nor fires (monster.c: a burning monster leaves its active routine), and
    only the shot already flying goes on."""
    found = list(found)
    killed = killed or {}
    ticks = max(len(path) + settle, cfg.horizon_ticks)
    zone = zone or (-10**6, 10**6)
    dave_xs = [p[0] for p in path]
    predicted = {t.entity_id: positions(t, ticks, cells, zone) for t in found if t.motion is None}
    # A shot flying past the screen edge may meet a wall there or not, and the monster fires its
    # next one as soon as it is gone: both are predicted (level 4: the swirl's shot flying right
    # died on the unseen wall at column 35, and the next one, fired at Dave 40 ticks early, hit
    # him mid-jump).
    scripted = {t.entity_id: [simulate(t, dave_xs, ticks, cells, zone, unseen) for unseen in (False, True)]
                for t in found if t.motion is not None}
    hazards = [(c * TILE, r * TILE) for (c, r), kind in cells.items() if kind == "hazard"]
    for tick in range(1, ticks + 1):
        if until is not None and tick >= until:
            return Contact("edge", "unknown", until)
        x, y = path[min(tick, len(path)) - 1]
        for hx, hy in hazards:
            if tick <= len(path) and _overlap(x, y, DAVE_BOX, hx, hy, HAZARD_BOX, 0):
                return Contact("hazard", "hazard", tick)
        for t in found:
            dead = t.entity_id in killed and tick >= killed[t.entity_id]
            if t.entity_id in scripted:
                for f in scripted[t.entity_id]:
                    shot, no = f.shots[tick - 1], f.shot_no[tick - 1]
                    if dead and no >= 1 and f.shot_no.index(no) + 1 >= killed[t.entity_id]:
                        continue  # due after the monster was hit: never fired
                    if shot is not None and _overlap(x, y, DAVE_BOX, shot[0], shot[1], PLASMA_BOX, cfg.margin_px):
                        n = t.entity_id[len("monster"):]
                        return Contact(f"plasma{n}" if f.shot_no[tick - 1] == 0 else f"shot{n}", "plasma", tick)
                # The game's own route rule: checked over the whole path like its shots (level 4:
                # a 49-tick jump onto (26,3) met the swirl 5 ticks after landing, past the horizon).
                mx, my = scripted[t.entity_id][0].monster[tick - 1]
                if not dead and _overlap(x, y, DAVE_BOX, mx, my, MONSTER_BOX, cfg.margin_px):
                    return Contact(t.entity_id, t.entity_type, tick)
                continue
            if t.entity_type != "plasma" and (tick > cfg.horizon_ticks or dead):
                continue
            pos = predicted[t.entity_id][tick - 1]
            if pos is None:
                continue
            box = PLASMA_BOX if t.entity_type == "plasma" else MONSTER_BOX
            margin = cfg.margin_px + (0 if t.entity_type == "plasma" else tick // cfg.monster_growth_ticks)
            if _overlap(x, y, DAVE_BOX, pos[0], pos[1], box, margin):
                return Contact(t.entity_id, t.entity_type, tick)
    return None


def forecast(obs: Observation, ticks: int) -> list[dict]:
    """Every visible threat's predicted path with Dave standing where he is, for the viewer:
    monsters, flying plasma, and each shot still to be fired (with the tick it is fired)."""
    if obs.player_position is None:
        return []
    cells = {(t.pos.col, t.pos.row): t.kind for t in obs.tiles}
    zone = deadzone(obs)
    out = []
    for t in threats(obs):
        if t.motion is None:
            pts = positions(t, ticks, cells, zone)
            out.append({"id": t.entity_id, "kind": t.entity_type, "spawn": 0,
                        "path": [(t.x, t.y)] + [p for p in pts if p is not None][:ticks]})
            continue
        f = simulate(t, [obs.player_position.x], ticks, cells, zone)
        out.append({"id": t.entity_id, "kind": t.entity_type, "spawn": 0, "path": [(t.x, t.y)] + f.monster})
        n = t.entity_id[len("monster"):]
        for no in sorted({s for s in f.shot_no if s >= 0}):
            idx = [i for i, s in enumerate(f.shot_no) if s == no]
            out.append({"id": f"plasma{n}" if no == 0 else f"shot{n}.{no}", "kind": "plasma",
                        "spawn": 0 if no == 0 else idx[0] + 1, "path": [f.shots[i] for i in idx]})
    return out


def scroll_tick(obs: Observation, path: list[tuple[int, int]]) -> int | None:
    """The first tick (1-based) of ``path`` at which the screen starts to scroll, or None."""
    left = obs.region.min.col * TILE
    for i, (x, _) in enumerate(path):
        if x - left > SCROLL_RIGHT_PX or (x - left < SCROLL_LEFT_PX and obs.region.min.col > 0):
            return i + 1
    return None


def _judge(obs: Observation, reach: ReachMap, path: list[tuple[int, int]], found: list[Threat],
           cfg: ThreatConfig, killed: Mapping[str, int] | None = None) -> Contact | None:
    """``first_contact`` for a path on the opened map, cut where it scrolls or leaves the map."""
    stops = [t for t in (reach.leaves_map(path), scroll_tick(obs, path)) if t is not None]
    return first_contact(path, found, reach.cells, cfg, deadzone(obs), min(stops) if stops else None,
                         settle=cfg.interrupt_ticks, killed=killed)


def _after(reach: ReachMap, obs: Observation, spec: SkillSpec, delay: int, drift: int = 0) -> list[tuple[int, int]]:
    """Dave's path standing ``delay`` ticks, then running ``spec`` (the jump cooldown runs down
    while he stands)."""
    pos = obs.player_position
    assert pos is not None
    cooldown = max(0, (obs.jump_cooldown or 0) - delay)
    return [(pos.x, pos.y)] * delay + trace_skill(reach, pos.x, pos.y, spec, drift, cooldown)


def safe_window(obs: Observation, reach: ReachMap, spec: SkillSpec, cfg: ThreatConfig,
                horizon: int = FIRE_WINDOW) -> tuple[int, int] | None:
    """(ticks to wait, ticks it stays safe) for the first moment within ``horizon`` at which
    ``spec``, started after standing that long, is safe; None when there is none. Monsters fire
    in a pattern (monster.c), so a move blocked now is often safe a few dozen ticks later: the
    route waits for it instead of giving up (level 4: the person timed every move past the
    swirl)."""
    found = threats(obs)
    opened = reach.opened()
    start = None
    for delay in range(0, horizon + 1, WINDOW_STEP):
        ok = not _blocks(_judge(obs, opened, _after(opened, obs, spec, delay), found, cfg))
        if ok and start is None:
            start = delay
        elif not ok and start is not None:
            return start, delay - start
    return None if start is None else (start, horizon + WINDOW_STEP - start)


def _moves(spec: SkillSpec) -> bool:
    return bool(spec.buttons & {"left", "right", "jump"})


def _blocks(contact: Contact | None) -> bool:
    return contact is not None and contact.blocks


def assess(obs: Observation, reach: ReachMap, candidates: list[SkillCandidate], specs: dict[str, SkillSpec],
           cfg: ThreatConfig) -> dict[str, Contact | None]:
    """First contact per candidate while Dave stands on a known cell, falls, or is mid-jump with
    a known jump tick; empty otherwise (``assess_timing`` without the notes)."""
    return assess_timing(obs, reach, candidates, specs, cfg)[0]


def assess_timing(obs: Observation, reach: ReachMap, candidates: list[SkillCandidate], specs: dict[str, SkillSpec],
                  cfg: ThreatConfig) -> tuple[dict[str, Contact | None], dict[str, str]]:
    """(first contact per candidate, timing and shot notes per candidate). Paths run on the
    opened map (unseen cells are open air) and stop being judged where they scroll the screen.
    While Dave stands with threats around, moving skills are also tried after 6, 12 ... 48
    standing ticks; a wait is safe when its own ticks are and a moving skill is safe after it."""
    pos = obs.player_position
    if pos is None:
        return {}, {}
    standing = obs.player_state in STANDING_STATES and reach.locate(pos.x, pos.y) is not None
    jumping = obs.player_state == "jumping" and obs.jump_tick is not None
    flying = obs.player_state == "jetpacking"
    if not standing and not jumping and not flying and obs.player_state not in FALLING_STATES:
        return {}, {}
    found = threats(obs)
    opened = reach.opened()
    armed = bool(obs.inventory and obs.inventory.get("gun"))
    note, hit = shot(obs, reach.cells, found) if armed else ("", None)
    fires = [c.candidate_id for c in candidates if armed and "fire" in specs[c.skill].buttons]
    # A shot that hits a monster stops it firing again: the shooting skill is judged without it.
    kills = {cid: dict([hit]) for cid in fires if hit is not None}
    notes: dict[str, str] = {cid: note for cid in fires}
    if flying:
        # With the jetpack on Dave hovers: each skill's keys move him 1 px a tick, the jetpack
        # key drops him (control/reach.py trace_flying).
        fuel = (obs.inventory or {}).get("jetpack_fuel")
        return {c.candidate_id: _judge(obs, opened, trace_flying(opened, pos.x, pos.y, specs[c.skill], fuel),
                                       found, cfg, kills.get(c.candidate_id)) for c in candidates}, notes
    if jumping:
        # Mid-jump the rest of the arc is known (the observation's jump tick); a skill can only
        # steer it, or shoot (level 3: the spider seen only once a jump scrolled the screen).
        assert obs.jump_tick is not None
        return {c.candidate_id: _judge(obs, opened, trace_in_jump(opened, pos.x, pos.y, obs.jump_tick, specs[c.skill]),
                                       found, cfg, kills.get(c.candidate_id)) for c in candidates}, notes
    # A falling Dave drifts only once a key turned him in the air; the observation's facing does
    # not tell a turned Dave from one that just stepped off (dave.c FRONTL/FRONTR), so a fall is
    # tried both ways and the earlier contact counts.
    drifts = (0, FACING.get(obs.facing or "", 0)) if not standing else (0,)
    out: dict[str, Contact | None] = {}
    for c in candidates:
        found_contacts = [_judge(obs, opened, _after(opened, obs, specs[c.skill], 0, d), found, cfg,
                                 kills.get(c.candidate_id)) for d in set(drifts)]
        hits = [h for h in found_contacts if h is not None]
        blocking = [h for h in hits if h.blocks]
        out[c.candidate_id] = min(blocking or hits, key=lambda h: h.tick) if hits else None
    if standing and found:
        for c in candidates:
            if out[c.candidate_id] is None and _moves(specs[c.skill]):
                out[c.candidate_id] = _trap(obs, opened, _after(opened, obs, specs[c.skill], 0), specs[c.skill],
                                            candidates, specs, found, cfg)
        _timing(obs, opened, candidates, specs, cfg, found, out, notes)
    return out, notes


LANDING_COOLDOWN = 5  # dave.c: the jump cooldown after a landing


def _trap(obs: Observation, reach: ReachMap, path: list[tuple[int, int]], spec: SkillSpec,
          candidates: list[SkillCandidate], specs: dict[str, SkillSpec], found: list[Threat],
          cfg: ThreatConfig) -> Contact | None:
    """A ``trap`` contact when ``path`` ends standing where every next move is hit and standing
    there is not safe for ``MAX_DELAY`` ticks, None otherwise. A short wait is no way out: it only
    puts the choice off (level 4: on (26,3) the swirl circles past every few dozen ticks). Level 4:
    the jump onto (23,5) is safe and a standing Dave is hit only 22 ticks after landing, but every
    move from there runs into the swirl's next shot."""
    end = path[-1]
    if reach.locate(*end) is None:
        return None  # not ending on known ground: no follow-up to judge
    stay = _judge(obs, reach, path + [end] * MAX_DELAY, found, cfg)
    if not _blocks(stay):
        return None
    cooldown = LANDING_COOLDOWN if "jump" in spec.buttons else max(0, (obs.jump_cooldown or 0) - len(path))
    first: Contact | None = stay
    for c in candidates:
        follow = specs[c.skill]
        if not _moves(follow):
            continue
        hit = _judge(obs, reach, path + trace_skill(reach, end[0], end[1], follow, 0, cooldown), found, cfg)
        if not _blocks(hit):
            return None
        if first is None or hit.tick > first.tick:
            first = hit
    return None if first is None else Contact("trap", first.kind, first.tick)


def _timing(obs: Observation, reach: ReachMap, candidates: list[SkillCandidate], specs: dict[str, SkillSpec],
            cfg: ThreatConfig, found: list[Threat], out: dict[str, Contact | None], notes: dict[str, str]) -> None:
    """Fill the timing notes, and judge the waits by their own ticks plus an escape after them."""
    pos = obs.player_position
    assert pos is not None
    delays = range(0, MAX_DELAY + 1, WAIT_STEP)
    movers = [c for c in candidates if _moves(specs[c.skill])]
    safe_at: dict[str, list[int]] = {}
    for c in movers:
        safe_at[c.candidate_id] = [d for d in delays if not _blocks(
            out[c.candidate_id] if d == 0 else _judge(obs, reach, _after(reach, obs, specs[c.skill], d), found, cfg))]
    still = _judge(obs, reach, [(pos.x, pos.y)], found, cfg)
    for c in movers:
        ok = safe_at[c.candidate_id]
        if ok and ok[0] == 0 and len(ok) < len(delays):
            late = next(d for d in delays if d not in ok)
            notes[c.candidate_id] = f"timing: go now, unsafe if started {late} or more ticks later"
    for c in candidates:
        spec = specs[c.skill]
        if _moves(spec) or "fire" in spec.buttons:
            continue
        ticks = spec.max_frames
        own = first_contact([(pos.x, pos.y)] * ticks, found, reach.cells,
                            cfg.model_copy(update={"horizon_ticks": ticks}), deadzone(obs))
        if own is not None and own.blocks and own.tick <= ticks:
            out[c.candidate_id] = own
            continue
        step = -(-ticks // WAIT_STEP) * WAIT_STEP  # the first tried delay at or after the wait's end
        after = [m.skill for m in movers if step in safe_at[m.candidate_id]]
        later = sorted((min(d for d in safe_at[m.candidate_id] if d > step), m.skill) for m in movers
                       if m.skill not in after and any(d > step for d in safe_at[m.candidate_id]))
        safe_for = "48+" if not _blocks(still) else str(still.tick - 1)
        parts = [f"timing: standing is safe for {safe_for} ticks"]
        if after:
            parts.append("then safe: " + ", ".join(after[:4]))
        if later:
            parts.append("later: " + ", ".join(f"{s} from {d} ticks" for d, s in later[:3]))
        notes[c.candidate_id] = "; ".join(parts)
        if after:
            out[c.candidate_id] = None  # a safe wait with an escape after it


def shot_note(obs: Observation, cells: dict[tuple[int, int], str], found: list[Threat]) -> str:
    """What Dave's bullet would do if fired now (``shot``'s note)."""
    return shot(obs, cells, found)[0]


def shot(obs: Observation, cells: dict[tuple[int, int], str],
         found: list[Threat]) -> tuple[str, tuple[str, int] | None]:
    """(note, (monster id, tick it is hit) or None) for Dave's bullet fired now: the first monster
    it hits (by the monsters' predicted motion), or a miss and why. The bullet stops at a brick
    (bullet.c tests x+10 going right, x-2 going left, at y+1) or at the screen's edge. It passes
    through plasma (game.c tests bullets against monsters only)."""
    pos = obs.player_position
    assert pos is not None
    side = FACING.get(obs.facing or "", 0)
    if not side:
        return "shot: facing neither way", None
    monsters = [t for t in found if t.entity_type != "plasma"]
    if not monsters:
        return "shot: no monster in sight", None
    if all((t.x - pos.x) * side < 0 for t in monsters):
        return "shot: facing away from the " + ", ".join(sorted({t.entity_type for t in monsters})), None
    left = obs.region.min.col * TILE
    zone = deadzone(obs)
    tracks = {t.entity_id: (simulate(t, [pos.x], 200, cells, zone).monster if t.motion is not None
                            else positions(t, 200, cells)) for t in monsters}
    bx, by = pos.x + 8 * side, pos.y + 8
    for tick in range(1, 201):
        bx += BULLET_SPEED * side
        if bx >= left + SCREEN_PX or bx <= left:
            return f"shot: misses (leaves the screen in {tick} ticks)", None
        if _brick(cells, bx + (10 if side > 0 else -2), by + 1):
            return f"shot: misses (hits a wall in {tick} ticks)", None
        for t in monsters:
            m = tracks[t.entity_id][tick - 1]
            if m is not None and _overlap(bx, by, BULLET_BOX, m[0], m[1], MONSTER_BOX, 0):
                return f"shot: hits the {t.entity_type} in {tick} ticks", (t.entity_id, tick)
    return "shot: misses", None


def screen(candidates: list[SkillCandidate], contacts: dict[str, Contact | None]) -> tuple[list[SkillCandidate],
                                                                                           dict[str, str]]:
    """(kept, masked {id: reason}). Candidates with a predicted contact are dropped; when every
    candidate has one, those whose contact comes latest are kept. An ``edge`` contact (the path
    goes past what is known) does not count."""
    if not contacts:
        return candidates, {}
    unsafe = {cid: c for cid, c in contacts.items() if c is not None and c.blocks}
    if len(unsafe) < len(candidates):
        keep = [c for c in candidates if c.candidate_id not in unsafe]
    else:
        latest = max(c.tick for c in unsafe.values())
        keep = [c for c in candidates if unsafe[c.candidate_id].tick == latest]
    kept = {c.candidate_id for c in keep}
    return keep, {cid: c.reason() for cid, c in unsafe.items() if cid not in kept}


def time_to_contact(obs: Observation, cfg: ThreatConfig, only: str | None = None) -> Contact | None:
    """The earliest contact within ``interrupt_ticks`` if Dave stays where he is (the
    ``threat_incoming`` interrupt's test); ``only`` restricts it to one entity."""
    pos = obs.player_position
    if pos is None:
        return None
    bricks = {(t.pos.col, t.pos.row): "solid" for t in obs.tiles if t.kind == "solid"}
    found = [t for t in threats(obs) if only is None or t.entity_id == only]
    return first_contact([(pos.x, pos.y)], found, bricks, cfg.model_copy(update={"horizon_ticks": cfg.interrupt_ticks}),
                         deadzone(obs))


def imminent(obs: Observation, cfg: ThreatConfig) -> frozenset[str]:
    """Every threat that would touch Dave within ``interrupt_ticks`` if he stayed where he is (a
    monster's next shot counts under the monster's plasma or shot id)."""
    out = set()
    for t in threats(obs):
        hit = time_to_contact(obs, cfg, t.entity_id)
        if hit is not None:
            out.add(threat_key(hit.what))
    return frozenset(out)


FIRING_TICKS = 200  # how far ahead the line of fire is predicted


def firing_cells(obs: Observation, ticks: int = FIRING_TICKS) -> set[tuple[int, int]]:
    """Cells where a standing Dave would be hit by a predicted shot (flying or still to be fired)
    within ``ticks``, with Dave where he is now deciding which way the shots fly."""
    out: set[tuple[int, int]] = set()
    for f in forecast(obs, ticks):
        if f["kind"] != "plasma":
            continue
        for x, y in f["path"]:
            for r in range((y - 17) // TILE, (y + 2) // TILE + 1):
                if not (y + 3 > r * TILE + 2 and y < r * TILE + 18):
                    continue
                for c in range((x - 16) // TILE, (x + 20) // TILE + 1):
                    if x + 20 > c * TILE + 2 and x < c * TILE + 16:
                        out.add((c, r))
    return out


def scripted_shots(obs: Observation) -> frozenset[str]:
    """The plasma ids of monsters whose motion is known: their shots, fired or not, are predicted
    (``simulate``), so a choice made with this observation already took them into account."""
    return frozenset("plasma" + e.entity_id[len("monster"):] for e in obs.entities
                     if e.visible and e.motion is not None and e.entity_id.startswith("monster"))


def threat_key(what: str) -> str:
    """One name for a monster's projectile whether it is flying (``plasma<n>``) or still to be
    fired (``shot<n>``), so a shot expected when a skill started does not interrupt it on firing."""
    return "plasma" + what[len("shot"):] if what.startswith("shot") else what


def contact_cause(obs: Observation) -> tuple[str, list[int] | None]:
    """What touches Dave in ``obs`` (his first burning frame): an entity type, "hazard" or
    "unknown", and the tile where it happened."""
    pos = obs.player_position
    if pos is None:
        return "unknown", None
    tile = [(pos.x + 8) // TILE, (pos.y + 8) // TILE]
    # Every visible entity at its own position: ``threats`` folds a monster's plasma into the
    # monster, and a death by its shot was logged as "unknown".
    for e in obs.entities:
        if not e.visible or e.entity_type in HARMLESS:
            continue
        box = PLASMA_BOX if e.entity_type == "plasma" else MONSTER_BOX
        if _overlap(pos.x, pos.y, DAVE_BOX, e.position.x, e.position.y, box, 2):
            return e.entity_type, tile
    for cell in obs.tiles:
        if cell.kind == "hazard" and _overlap(pos.x, pos.y, DAVE_BOX, cell.pos.col * TILE, cell.pos.row * TILE,
                                              HAZARD_BOX, 2):
            return cell.name, tile
    return "unknown", tile
