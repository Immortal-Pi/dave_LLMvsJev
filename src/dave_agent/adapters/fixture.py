"""Deterministic grid platformer for harness development.

This is NOT Dangerous Dave. Results produced with it are labeled ``adapter=fixture``.
Rules are deliberately simple and tile-based (16 px per tile, like deadly-dave):
walk one tile per frame, a fresh jump press rises two tiles, gravity drops one tile per frame,
fire kills, the door completes the level only while holding the trophy.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from dave_agent.adapters.base import AdapterError
from dave_agent.schemas import (
    AdapterCapabilities,
    Event,
    Observation,
    ObservedTile,
    PixelPos,
    PixelVelocity,
    Region,
    SnapshotRef,
    StepResult,
    TilePos,
    check_buttons,
)

TILE_PX = 16
JUMP_RISE_TILES = 2
BUTTONS = frozenset({"left", "right", "jump"})
BUILD_ID = "fixture-platformer-v1"

# char -> (kind, name)
TILE_LEGEND = {
    "#": ("solid", "wall"),
    "F": ("hazard", "fire"),
    "*": ("collectible", "gem"),
    "T": ("required_item", "trophy"),
    "X": ("exit", "door"),
}
GEM_SCORE = 100
TROPHY_SCORE = 1000


@dataclass(frozen=True)
class FixtureLevel:
    level_id: str
    grid: tuple[str, ...]
    start: TilePos
    lives: int
    view_half_width: int

    @property
    def width(self) -> int:
        return len(self.grid[0])

    @property
    def height(self) -> int:
        return len(self.grid)

    def char(self, col: int, row: int) -> str:
        if not (0 <= row < self.height and 0 <= col < self.width):
            return "#" if row < self.height else "."
        return self.grid[row][col]

    @classmethod
    def load(cls, path: Path) -> FixtureLevel:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        rows = data.get("rows")
        if not rows or len({len(r) for r in rows}) != 1:
            raise AdapterError(f"{path}: 'rows' must be non-empty strings of equal length")
        starts = [(c, r) for r, line in enumerate(rows) for c, ch in enumerate(line) if ch == "D"]
        if len(starts) != 1:
            raise AdapterError(f"{path}: exactly one 'D' start tile required, found {len(starts)}")
        bad = {ch for line in rows for ch in line} - set(TILE_LEGEND) - {".", "D"}
        if bad:
            raise AdapterError(f"{path}: unknown tile characters {sorted(bad)}")
        col, row = starts[0]
        grid = tuple(line.replace("D", ".") for line in rows)
        return cls(
            level_id=data.get("level_id", path.stem),
            grid=grid,
            start=TilePos(col=col, row=row),
            lives=int(data.get("lives", 3)),
            view_half_width=int(data.get("view_half_width", 4)),
        )


@dataclass
class _State:
    col: int
    row: int
    rise_remaining: int = 0
    jump_held: bool = False
    lives: int = 3
    score: int = 0
    has_trophy: bool = False
    collected: set[tuple[int, int]] = field(default_factory=set)
    frame: int = 0
    terminal: str = "running"
    facing: str = "right"
    last_velocity: PixelVelocity = field(default_factory=lambda: PixelVelocity(dx=0, dy=0))


class FixtureAdapter:
    def __init__(self, levels_dir: Path) -> None:
        self._levels_dir = Path(levels_dir)
        self._level: FixtureLevel | None = None
        self._state: _State | None = None
        self._episode_id = ""
        self._observation_id = 0
        self._episode_count = 0
        self._snapshots: dict[str, tuple[str, _State]] = {}

    # -- contract ---------------------------------------------------------
    def capabilities(self) -> AdapterCapabilities:
        return AdapterCapabilities(
            adapter="fixture",
            build_id=BUILD_ID,
            reset="supported",
            observe="supported",
            step_exact_frames="supported",
            snapshots="supported",
            headless="supported",
            buttons=BUTTONS,
            frames_per_second=None,
            notes=("Synthetic tile platformer; not Dangerous Dave.",),
        )

    def reset(self, scenario_id: str, seed: int) -> Observation:
        path = self._levels_dir / f"{scenario_id}.yaml"
        if not path.exists():
            available = sorted(p.stem for p in self._levels_dir.glob("*.yaml"))
            raise AdapterError(f"unknown fixture scenario {scenario_id!r}; available: {available}")
        self._level = FixtureLevel.load(path)
        self._state = _State(col=self._level.start.col, row=self._level.start.row, lives=self._level.lives)
        self._episode_count += 1
        # The fixture is fully deterministic; the seed is recorded for manifest parity only.
        self._episode_id = f"{scenario_id}-s{seed}-e{self._episode_count}"
        self._snapshots.clear()
        return self.observe()

    def observe(self) -> Observation:
        level, state = self._require()
        self._observation_id += 1
        half = level.view_half_width
        region = Region(
            min=TilePos(col=max(0, state.col - half), row=0),
            max=TilePos(col=min(level.width - 1, state.col + half), row=level.height - 1),
        )
        tiles = []
        for row in range(region.min.row, region.max.row + 1):
            for col in range(region.min.col, region.max.col + 1):
                ch = level.char(col, row)
                if ch in TILE_LEGEND and (col, row) not in state.collected:
                    kind, name = TILE_LEGEND[ch]
                    tiles.append(ObservedTile(pos=TilePos(col=col, row=row), kind=kind, name=name))
        return Observation(
            adapter="fixture",
            build_id=BUILD_ID,
            episode_id=self._episode_id,
            level_id=level.level_id,
            frame=state.frame,
            observation_id=self._observation_id,
            player_position=PixelPos(x=state.col * TILE_PX, y=state.row * TILE_PX),
            player_velocity=state.last_velocity,
            player_velocity_source="measured",
            grounded=self._grounded(level, state),
            player_state=self._player_state(level, state),
            facing=state.facing,
            lives=state.lives,
            inventory={"trophy": int(state.has_trophy)},
            score=state.score,
            tiles=tuple(tiles),
            entities=(),
            region=region,
            terminal=state.terminal,
        )

    def step(self, buttons: frozenset[str], frames: int) -> StepResult:
        level, state = self._require()
        check_buttons(buttons, BUTTONS)
        if frames < 1:
            raise AdapterError(f"frames must be >= 1, got {frames}")
        if state.terminal != "running":
            raise AdapterError(f"episode already terminal ({state.terminal}); call reset()")
        events: list[Event] = []
        advanced = 0
        for _ in range(frames):
            lives_before = state.lives
            events.extend(self._tick(level, state, buttons))
            advanced += 1
            # Stop early on terminal or death so a skill never runs past a respawn.
            if state.terminal != "running" or state.lives != lives_before:
                break
        return StepResult(
            observation=self.observe(), frames_advanced=advanced, applied_buttons=buttons, events=tuple(events)
        )

    def save_snapshot(self) -> SnapshotRef:
        _, state = self._require()
        snapshot_id = f"snap-{len(self._snapshots) + 1}"
        self._snapshots[snapshot_id] = (self._episode_id, copy.deepcopy(state))
        return SnapshotRef(
            snapshot_id=snapshot_id, adapter="fixture", frame=state.frame, observation_id=self._observation_id
        )

    def load_snapshot(self, snapshot: SnapshotRef) -> Observation:
        if snapshot.snapshot_id not in self._snapshots:
            raise AdapterError(f"unknown snapshot {snapshot.snapshot_id!r} for this episode")
        self._episode_id, state = self._snapshots[snapshot.snapshot_id]
        self._state = copy.deepcopy(state)
        return self.observe()

    def close(self) -> None:
        self._snapshots.clear()
        self._state = None

    # -- rules ------------------------------------------------------------
    def _require(self) -> tuple[FixtureLevel, _State]:
        if self._level is None or self._state is None:
            raise AdapterError("adapter not reset; call reset(scenario_id, seed) first")
        return self._level, self._state

    @staticmethod
    def _solid(level: FixtureLevel, col: int, row: int) -> bool:
        return level.char(col, row) == "#"

    def _grounded(self, level: FixtureLevel, state: _State) -> bool:
        return self._solid(level, state.col, state.row + 1)

    def _player_state(self, level: FixtureLevel, state: _State) -> str:
        if state.rise_remaining > 0:
            return "jumping"
        if not self._grounded(level, state):
            return "freefalling"
        return "walking" if state.last_velocity.dx else "standing"

    def _tick(self, level: FixtureLevel, state: _State, buttons: frozenset[str]) -> list[Event]:
        state.frame += 1
        start_col, start_row = state.col, state.row

        dx = ("right" in buttons) - ("left" in buttons)
        if dx:
            state.facing = "right" if dx > 0 else "left"
        if dx and not self._solid(level, state.col + dx, state.row):
            state.col += dx

        # A jump starts only on a fresh press; holding jump does not re-jump on landing.
        pressed = "jump" in buttons and not state.jump_held
        state.jump_held = "jump" in buttons
        if pressed and state.rise_remaining == 0 and self._grounded(level, state):
            state.rise_remaining = JUMP_RISE_TILES
        if state.rise_remaining > 0:
            if self._solid(level, state.col, state.row - 1):
                state.rise_remaining = 0
            else:
                state.row -= 1
                state.rise_remaining -= 1
        elif not self._grounded(level, state):
            state.row += 1

        state.last_velocity = PixelVelocity(
            dx=(state.col - start_col) * TILE_PX, dy=(state.row - start_row) * TILE_PX
        )
        return self._interact(level, state)

    def _interact(self, level: FixtureLevel, state: _State) -> list[Event]:
        events: list[Event] = []
        here = TilePos(col=state.col, row=state.row)
        base = {"episode_id": self._episode_id, "frame": state.frame, "location": here}

        if state.row >= level.height:
            return self._die(level, state, cause="fell_out_of_level", base=base)
        ch = level.char(state.col, state.row)
        key = (state.col, state.row)
        if ch == "F":
            return self._die(level, state, cause="hazard_tile:fire", base=base)
        if ch in ("*", "T") and key not in state.collected:
            state.collected.add(key)
            item = "gem" if ch == "*" else "trophy"
            state.score += GEM_SCORE if ch == "*" else TROPHY_SCORE
            if ch == "T":
                state.has_trophy = True
            events.append(Event(event_type="item_collected", payload={"item": item}, **base))
        if ch == "X" and state.has_trophy:
            state.terminal = "level_complete"
            events.append(Event(event_type="level_complete", **base))
        return events

    def _die(self, level: FixtureLevel, state: _State, cause: str, base: dict) -> list[Event]:
        state.lives -= 1
        # The cause is known exactly here because the fixture rules define it.
        events = [Event(event_type="death", payload={"cause": cause}, **base)]
        if state.lives == 0:
            state.terminal = "game_over"
            events.append(Event(event_type="game_over", **base))
            return events
        state.col, state.row, state.rise_remaining = level.start.col, level.start.row, 0
        events.append(
            Event(
                event_type="respawn",
                episode_id=self._episode_id,
                frame=state.frame,
                location=level.start,
            )
        )
        return events
