"""Threat prediction: where plasma and monsters will be, and whether a skill's path meets them.

Shared by every arm and computed from the observation alone (docs/skills.md):

- Plasma flies straight at 2 px/tick (plasma.c) until its leading edge enters a brick. With
  no velocity yet (first sighting) it is assumed to fly toward Dave, as the game fires it.
- Monsters move along fixed loops; here they are extrapolated linearly from the derived
  per-tick velocity, and their box grows by 1 px per ``monster_growth_ticks`` to cover the turn.
- Dave's path comes from the reach simulation (control/reach.py ``trace_skill``); after the
  skill ends he is assumed to stand still until the look-ahead ends.
- Boxes are the game's collision boxes: Dave x+2, y+2, 14x16 (dave.c), plasma 20x3, monsters
  about 24x21 (monster.c), and the hazard part of a fire, vine or water cell (tile.c), each
  grown by ``margin_px``.

``assess`` returns the first contact for every candidate; ``screen`` drops the candidates
that touch something unless all of them do, then keeps the ones that touch latest.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from dave_agent.config import SkillSpec, ThreatConfig
from dave_agent.control.reach import TILE, ReachMap, trace_skill
from dave_agent.schemas import Observation, SkillCandidate

Box = tuple[int, int, int, int]  # dx, dy, width, height from the sprite's top-left pixel
DAVE_BOX: Box = (2, 2, 14, 16)
PLASMA_BOX: Box = (0, 0, 20, 3)
MONSTER_BOX: Box = (0, 0, 24, 21)
# Within a 16 px cell, the union of the game's boxes (tile.c): fire x+6 w4 and vines x+4 w8 are
# full height, water x+6 w4 y+4 h8. Hazard kinds are not told apart in the observation.
HAZARD_BOX: Box = (4, 0, 8, 16)
PLASMA_SPEED = 2
HARMLESS = frozenset({"bullet"})  # Dave's own shot
STANDING_STATES = frozenset({"standing", "walking"})
FACING = {"left": -1, "right": 1}
FALLING_STATES = frozenset({"freefalling"})  # steerable in the air: walks and waits can still dodge


@dataclass(frozen=True)
class Threat:
    entity_id: str
    entity_type: str
    x: int
    y: int
    dx: int  # px per tick
    dy: int


@dataclass(frozen=True)
class Contact:
    """The first predicted contact of a path: with an entity, or with a hazard cell."""

    what: str  # entity id, or "hazard"
    kind: str  # plasma | monster type | hazard
    tick: int

    def note(self) -> str:
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
    for e in obs.entities:
        if not e.visible or e.entity_type in HARMLESS:
            continue
        v = e.velocity
        if e.entity_type == "plasma":
            toward = 1 if obs.player_position.x > e.position.x else -1
            dx = (PLASMA_SPEED if v.dx > 0 else -PLASMA_SPEED) if v is not None and v.dx else PLASMA_SPEED * toward
            out.append(Threat(e.entity_id, "plasma", e.position.x, e.position.y, dx, 0))
        else:
            out.append(Threat(e.entity_id, e.entity_type, e.position.x, e.position.y,
                              v.dx if v else 0, v.dy if v else 0))
    return out


def _brick(cells: dict[tuple[int, int], str], x: int, y: int) -> bool:
    return cells.get((x // TILE, y // TILE)) == "solid"


def positions(threat: Threat, ticks: int, cells: dict[tuple[int, int], str]) -> list[tuple[int, int] | None]:
    """Predicted top-left after ticks 1..ticks; None once a plasma has hit a brick."""
    out: list[tuple[int, int] | None] = []
    x, y, alive = threat.x, threat.y, True
    for _ in range(ticks):
        if alive:
            x, y = x + threat.dx, y + threat.dy
            if threat.entity_type == "plasma":
                lead = x + 20 if threat.dx > 0 else x - 2
                alive = not _brick(cells, lead, y + 1)
        out.append((x, y) if alive else None)
    return out


def first_contact(path: list[tuple[int, int]], found: Iterable[Threat], cells: dict[tuple[int, int], str],
                  cfg: ThreatConfig) -> Contact | None:
    """The earliest contact along ``path`` (Dave's top-left after ticks 1..n), then standing at its
    end until the look-ahead ends. Plasma is checked over the whole path, monsters only within
    ``horizon_ticks``."""
    found = list(found)
    ticks = max(len(path), cfg.horizon_ticks)
    predicted = {t.entity_id: positions(t, ticks, cells) for t in found}
    hazards = [(c * TILE, r * TILE) for (c, r), kind in cells.items() if kind == "hazard"]
    for tick in range(1, ticks + 1):
        x, y = path[min(tick, len(path)) - 1]
        for hx, hy in hazards:
            if tick <= len(path) and _overlap(x, y, DAVE_BOX, hx, hy, HAZARD_BOX, 0):
                return Contact("hazard", "hazard", tick)
        for t in found:
            if t.entity_type != "plasma" and tick > cfg.horizon_ticks:
                continue
            pos = predicted[t.entity_id][tick - 1]
            if pos is None:
                continue
            box = PLASMA_BOX if t.entity_type == "plasma" else MONSTER_BOX
            margin = cfg.margin_px + (0 if t.entity_type == "plasma" else tick // cfg.monster_growth_ticks)
            if _overlap(x, y, DAVE_BOX, pos[0], pos[1], box, margin):
                return Contact(t.entity_id, t.entity_type, tick)
    return None


def assess(obs: Observation, reach: ReachMap, candidates: list[SkillCandidate], specs: dict[str, SkillSpec],
           cfg: ThreatConfig) -> dict[str, Contact | None]:
    """First contact per candidate while Dave stands on a known cell or falls; empty otherwise
    (mid-jump the remaining arc is unknown)."""
    pos = obs.player_position
    if pos is None:
        return {}
    standing = obs.player_state in STANDING_STATES and reach.locate(pos.x, pos.y) is not None
    if not standing and obs.player_state not in FALLING_STATES:
        return {}
    found = threats(obs)
    # A falling Dave drifts only once a key turned him in the air; the observation's facing does
    # not tell a turned Dave from one that just stepped off (dave.c FRONTL/FRONTR), so a fall is
    # tried both ways and the earlier contact counts.
    drifts = (0, FACING.get(obs.facing or "", 0)) if not standing else (0,)
    out: dict[str, Contact | None] = {}
    for c in candidates:
        found_contacts = [first_contact(trace_skill(reach, pos.x, pos.y, specs[c.skill], d), found, reach.cells, cfg)
                          for d in set(drifts)]
        hits = [h for h in found_contacts if h is not None]
        out[c.candidate_id] = min(hits, key=lambda h: h.tick) if hits else None
    return out


def screen(candidates: list[SkillCandidate], contacts: dict[str, Contact | None]) -> tuple[list[SkillCandidate],
                                                                                           dict[str, str]]:
    """(kept, masked {id: reason}). Candidates with a predicted contact are dropped; when every
    candidate has one, those whose contact comes latest are kept."""
    if not contacts:
        return candidates, {}
    unsafe = {cid: c for cid, c in contacts.items() if c is not None}
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
    return first_contact([(pos.x, pos.y)], found, bricks, cfg.model_copy(update={"horizon_ticks": cfg.interrupt_ticks}))


def imminent(obs: Observation, cfg: ThreatConfig) -> frozenset[str]:
    """Every threat that would touch Dave within ``interrupt_ticks`` if he stayed where he is."""
    return frozenset(t.entity_id for t in threats(obs) if time_to_contact(obs, cfg, t.entity_id) is not None)


def contact_cause(obs: Observation) -> tuple[str, list[int] | None]:
    """What touches Dave in ``obs`` (his first burning frame): an entity type, "hazard" or
    "unknown", and the tile where it happened."""
    pos = obs.player_position
    if pos is None:
        return "unknown", None
    tile = [(pos.x + 8) // TILE, (pos.y + 8) // TILE]
    for t in threats(obs):
        box = PLASMA_BOX if t.entity_type == "plasma" else MONSTER_BOX
        if _overlap(pos.x, pos.y, DAVE_BOX, t.x, t.y, box, 2):
            return t.entity_type, tile
    for cell in obs.tiles:
        if cell.kind == "hazard" and _overlap(pos.x, pos.y, DAVE_BOX, cell.pos.col * TILE, cell.pos.row * TILE,
                                              HAZARD_BOX, 2):
            return cell.name, tile
    return "unknown", tile
