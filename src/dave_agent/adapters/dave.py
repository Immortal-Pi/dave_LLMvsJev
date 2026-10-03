"""Dangerous Dave adapter: drives the deadly-dave stepping bridge in a subprocess.

The bridge (``bridge/deadly-dave-bridge.patch``) speaks JSON lines over stdin/stdout.
Raw state is decoded and filtered to the visible viewport (``local_observed``) here,
before any agent or memory component sees it. Snapshots are input logs: the game is
deterministic, so restoring means reset plus replaying the recorded steps.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from dave_agent.adapters.base import AdapterError
from dave_agent.schemas import (
    AdapterCapabilities,
    Entity,
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

BRIDGE_EXE = "deadly-dave-bridge.exe"
BRIDGE_PROTOCOL = 1
TILE_PX = 16
VIEW_COLS = 20  # 320 px scene
TICK_SECONDS = 0.014  # game.c gameloop tick_interval

# Harness button -> bridge key letter. 'jetpack' is a one-tick key-down toggle.
BUTTON_KEYS = {"left": "L", "right": "R", "jump": "J", "down": "D", "fire": "F", "jetpack": "P"}
BUTTONS = frozenset(BUTTON_KEYS)

# tile.h modifier constants -> (kind, name). MOSS (5) is decorative: no rule reads it.
TILE_MODS = {
    2: ("solid", "brick"),
    3: ("collectible", "loot"),
    4: ("required_item", "trophy"),
    6: ("climbable", "climbable"),
    7: ("hazard", "hazard"),
    8: ("exit", "door"),
    10: ("item", "gun"),
    11: ("item", "jetpack"),
}
PICKUP_MODS = {3: "loot", 4: "trophy", 10: "gun", 11: "jetpack"}

# dave.h DAVE_STATE_* and DAVE_DIRECTION_* (FRONTR/FRONTL fire like RIGHT/LEFT in game_do_bullets).
PLAYER_STATES = ("standing", "walking", "jumping", "climbing", "freefalling", "jetpacking", "burning", "dead", "blinking")
FACING = {0: "front", 1: "right", 2: "left", 3: "left", 4: "right"}

# First sprite index of each monster's 4-frame animation (tile.h SPRITE_IDX_MONSTER_*).
MONSTER_SPRITE_BASE = {89: "spider", 93: "swirl", 97: "sun", 101: "bones", 105: "ufo", 109: "guard"}


def monster_type(sprite: int) -> str:
    for base, name in MONSTER_SPRITE_BASE.items():
        if base <= sprite < base + 4:
            return name
    return "unknown"


@dataclass
class _Episode:
    scenario_id: str
    seed: int
    episode_id: str
    level: int
    log: list[tuple[str, int]] = field(default_factory=list)


class DaveBridgeAdapter:
    def __init__(
        self, dave_dir: Path, timeout_seconds: float = 10.0, watch: bool = False, watch_delay_ms: int = 14
    ) -> None:
        self._dir = Path(dave_dir)
        exe = self._dir / BRIDGE_EXE
        if not exe.exists():
            raise AdapterError(
                f"{exe} not found; run scripts\\setup_dave.bat to clone, patch and build deadly-dave"
            )
        self._exe = exe
        self._build_id = f"deadly-dave-bridge-p{BRIDGE_PROTOCOL}-{_sha256(exe)[:12]}"
        self._timeout = timeout_seconds
        self._watch = watch
        self._watch_delay_ms = watch_delay_ms
        self._proc: subprocess.Popen[str] | None = None
        self._episode: _Episode | None = None
        self._episode_count = 0
        self._observation_id = 0
        self._raw: dict | None = None
        self._last: Observation | None = None
        self._prev_positions: dict[str, tuple[int, int]] = {}
        self._snapshots: dict[str, tuple[_Episode, int]] = {}
        self.game_stdout: list[str] = []  # last 100 non-protocol lines printed by the game

    # -- process ----------------------------------------------------------
    def _start(self) -> None:
        self._proc = subprocess.Popen(
            self._bridge_args(),
            cwd=self._dir,  # the game opens res/... relative to its repo root
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            bufsize=1,
        )
        ready = self._read()
        if ready.get("status") != "ready" or ready.get("protocol") != BRIDGE_PROTOCOL:
            raise AdapterError(f"unexpected bridge handshake: {ready}")

    def _bridge_args(self) -> list[str]:
        args = [str(self._exe)]
        if self._watch:
            # Viewer window for humans only; observations never include pixels.
            args += ["-window", "-delay", str(max(0, int(self._watch_delay_ms)))]
        return args

    def _read(self) -> dict:
        assert self._proc is not None and self._proc.stdout is not None
        while True:
            line = self._proc.stdout.readline()
            if not line:
                raise AdapterError(f"bridge exited unexpectedly (code {self._proc.poll()})")
            if line.startswith("{"):
                return json.loads(line)
            # Upstream debug printf (e.g. monster.c "ROUTE RESET ...") shares stdout with
            # the protocol; keep it as a diagnostic instead of failing the parse.
            self.game_stdout.append(line.rstrip())
            del self.game_stdout[:-100]

    def _command(self, text: str) -> dict:
        if self._proc is None or self._proc.poll() is not None:
            self._start()
        assert self._proc is not None and self._proc.stdin is not None
        self._proc.stdin.write(text + "\n")
        self._proc.stdin.flush()
        reply = self._read()
        if reply.get("status") != "ok":
            raise AdapterError(f"bridge rejected {text!r}: {reply.get('error', reply)}")
        return reply

    # -- contract ---------------------------------------------------------
    def capabilities(self) -> AdapterCapabilities:
        return AdapterCapabilities(
            adapter="dave",
            build_id=self._build_id,
            reset="supported",
            observe="supported",
            step_exact_frames="supported",
            snapshots="supported",
            headless="supported",
            buttons=BUTTONS,
            frames_per_second=1 / TICK_SECONDS,
            notes=(
                "deadly-dave via stepping bridge; one frame = one 14 ms game tick.",
                "Snapshots replay the input log from reset (the game is deterministic).",
                "Respawn after death auto-releases Dave; those ticks are reported as auto_ticks.",
                "jetpack is a key-down toggle applied on the first tick of a step.",
            )
            + (("Watch mode: a viewer window shows each tick (display only).",) if self._watch else ()),
        )

    def reset(self, scenario_id: str, seed: int) -> Observation:
        level = _parse_level(scenario_id)
        self._episode_count += 1
        # The game has no randomness; the seed is recorded for manifest parity only.
        self._episode = _Episode(
            scenario_id=scenario_id,
            seed=seed,
            episode_id=f"dave-{scenario_id}-s{seed}-e{self._episode_count}",
            level=level,
        )
        self._snapshots.clear()
        self._prev_positions.clear()
        self._raw = self._command(f"reset {level}")
        return self._build_observation(self._raw, previous=None)

    def observe(self) -> Observation:
        self._require()
        assert self._last is not None
        self._observation_id += 1
        return self._last.model_copy(update={"observation_id": self._observation_id})

    def step(self, buttons: frozenset[str], frames: int) -> StepResult:
        episode = self._require()
        check_buttons(buttons, BUTTONS)
        if frames < 1:
            raise AdapterError(f"frames must be >= 1, got {frames}")
        keys = "".join(sorted(BUTTON_KEYS[b] for b in buttons)) or "-"
        previous = self._raw
        self._raw = self._command(f"step {keys} {frames}")
        episode.log.append((keys, frames))
        observation = self._build_observation(self._raw, previous=previous)
        events = self._events(previous, self._raw, observation)
        return StepResult(
            observation=observation,
            frames_advanced=self._raw["frames"] + self._raw["auto_ticks"],
            applied_buttons=buttons,
            events=tuple(events),
        )

    def save_snapshot(self) -> SnapshotRef:
        episode = self._require()
        snapshot_id = f"snap-{len(self._snapshots) + 1}"
        self._snapshots[snapshot_id] = (
            _Episode(episode.scenario_id, episode.seed, episode.episode_id, episode.level, list(episode.log)),
            self._raw["tick"],
        )
        return SnapshotRef(
            snapshot_id=snapshot_id, adapter="dave", frame=self._raw["tick"], observation_id=self._observation_id
        )

    def load_snapshot(self, snapshot: SnapshotRef) -> Observation:
        if snapshot.snapshot_id not in self._snapshots:
            raise AdapterError(f"unknown snapshot {snapshot.snapshot_id!r} for this episode")
        saved, tick = self._snapshots[snapshot.snapshot_id]
        self._raw = self._command(f"reset {saved.level}")
        for keys, frames in saved.log:
            self._raw = self._command(f"step {keys} {frames}")
        if self._raw["tick"] != tick:
            raise AdapterError(f"replay diverged: tick {self._raw['tick']} != snapshot tick {tick}")
        self._episode = _Episode(saved.scenario_id, saved.seed, saved.episode_id, saved.level, list(saved.log))
        self._prev_positions.clear()
        return self._build_observation(self._raw, previous=None)

    def screenshot(self, path: Path) -> None:
        """Save the last rendered frame for human inspection. Never model input."""
        self._command(f"screenshot {Path(path).resolve()}")

    def raw_state(self) -> dict:
        """Unfiltered bridge state, for adapter debugging and calibration only."""
        self._require()
        return dict(self._raw)

    def close(self) -> None:
        if self._proc is not None and self._proc.poll() is None:
            try:
                assert self._proc.stdin is not None
                self._proc.stdin.write("quit\n")
                self._proc.stdin.flush()
                self._proc.wait(timeout=self._timeout)
            except (OSError, subprocess.TimeoutExpired):
                self._proc.kill()
        self._proc = None
        self._snapshots.clear()

    # -- decoding ---------------------------------------------------------
    def _require(self) -> _Episode:
        if self._episode is None or self._raw is None:
            raise AdapterError("adapter not reset; call reset(scenario_id, seed) first")
        return self._episode

    def _build_observation(self, raw: dict, previous: dict | None) -> Observation:
        episode = self._require()
        terminal = raw["terminal"]
        if terminal not in ("running", "level_complete", "game_over", "secret_exit"):
            raise AdapterError(f"bridge reached unsupported game state {raw['game_state']} ({terminal})")
        self._observation_id += 1
        scroll = raw["scroll_offset"]
        region = Region(
            min=TilePos(col=scroll, row=0),
            max=TilePos(col=min(scroll + VIEW_COLS, raw["map_cols"]) - 1, row=raw["map_rows"] - 1),
        )
        rows = raw["map_rows"]
        tiles = []
        for col in range(region.min.col, region.max.col + 1):
            for row in range(rows):
                mod = int(raw["mods"][col * rows + row], 36)
                if mod in TILE_MODS:
                    kind, name = TILE_MODS[mod]
                    tiles.append(ObservedTile(pos=TilePos(col=col, row=row), kind=kind, name=name))

        dave = raw["dave"]
        respawned = previous is not None and raw["auto_ticks"] > 0
        velocity = None
        if previous is not None and not respawned:
            elapsed = raw["tick"] - previous["tick"]
            if elapsed > 0:
                # Average pixels per tick over the step; the game exposes no velocity field.
                velocity = PixelVelocity(
                    dx=round((dave["x"] - previous["dave"]["x"]) / elapsed),
                    dy=round((dave["y"] - previous["dave"]["y"]) / elapsed),
                )

        entities = self._entities(raw, previous, region)
        unavailable = set() if velocity is not None else {"player_velocity"}
        self._last = Observation(
            adapter="dave",
            build_id=self._build_id,
            episode_id=episode.episode_id,
            level_id=f"level{raw['level']}",
            frame=raw["tick"],
            observation_id=self._observation_id,
            player_position=PixelPos(x=dave["x"], y=dave["y"]),
            player_velocity=velocity,
            player_velocity_source="derived" if velocity is not None else None,
            grounded=bool(dave["grounded"]),
            player_state=PLAYER_STATES[dave["state"]],
            facing=FACING[dave["face"]],
            lives=raw["lives"],
            inventory={
                "trophy": dave["has_trophy"],
                "gun": dave["has_gun"],
                "jetpack_fuel": dave["jetpack_bars"],
            },
            score=raw["score"],
            tiles=tuple(tiles),
            entities=tuple(entities),
            region=region,
            terminal=terminal,
            unavailable_fields=frozenset(unavailable),
        )
        return self._last

    def _entities(self, raw: dict, previous: dict | None, region: Region) -> list[Entity]:
        left = region.min.col * TILE_PX
        right = (region.max.col + 1) * TILE_PX
        found: list[tuple[str, str, int, int]] = []
        for m in raw["monsters"]:
            if m["alive"]:
                found.append((f"monster{m['idx']}", monster_type(m["sprite"]), m["x"], m["y"]))
            if "plasma" in m:
                found.append((f"plasma{m['idx']}", "plasma", m["plasma"]["x"], m["plasma"]["y"]))
        if raw["bullet"] is not None:
            found.append(("bullet", "bullet", raw["bullet"]["x"], raw["bullet"]["y"]))

        elapsed = raw["tick"] - previous["tick"] if previous is not None else 0
        entities, positions = [], {}
        for entity_id, entity_type, x, y in found:
            if not (left <= x < right):
                continue  # off screen: never visible under local_observed
            positions[entity_id] = (x, y)
            prior = self._prev_positions.get(entity_id)
            velocity = None
            if prior is not None and elapsed > 0 and raw["auto_ticks"] == 0:
                velocity = PixelVelocity(dx=round((x - prior[0]) / elapsed), dy=round((y - prior[1]) / elapsed))
            entities.append(
                Entity(
                    entity_id=entity_id,
                    entity_type=entity_type,
                    position=PixelPos(x=x, y=y),
                    velocity=velocity,
                    velocity_source="derived" if velocity is not None else None,
                    last_observed_frame=raw["tick"],
                    visible=True,
                    source="adapter",
                )
            )
        self._prev_positions = positions
        return entities

    def _events(self, previous: dict, raw: dict, observation: Observation) -> list[Event]:
        episode_id, frame = observation.episode_id, observation.frame
        events: list[Event] = []
        rows = raw["map_rows"]
        for i, (old, new) in enumerate(zip(previous["mods"], raw["mods"])):
            if old != new and int(old, 36) in PICKUP_MODS and int(new, 36) == 0:
                events.append(
                    Event(
                        event_type="item_collected",
                        episode_id=episode_id,
                        frame=frame,
                        location=TilePos(col=i // rows, row=i % rows),
                        payload={"item": PICKUP_MODS[int(old, 36)]},
                    )
                )
        if raw["lives"] < previous["lives"]:
            # The bridge does not report what ignited Dave, so the cause stays unknown.
            events.append(
                Event(
                    event_type="death",
                    episode_id=episode_id,
                    frame=frame,
                    certainty="observed",
                    payload={"cause": "unknown"},
                )
            )
            if raw["terminal"] == "running":
                events.append(
                    Event(
                        event_type="respawn",
                        episode_id=episode_id,
                        frame=frame,
                        payload={"auto_ticks": raw["auto_ticks"]},
                    )
                )
        if raw["terminal"] == "level_complete":
            events.append(Event(event_type="level_complete", episode_id=episode_id, frame=frame))
        elif raw["terminal"] == "game_over":
            events.append(Event(event_type="game_over", episode_id=episode_id, frame=frame))
        return events


def _parse_level(scenario_id: str) -> int:
    if not scenario_id.startswith("level") or not scenario_id[5:].isdigit():
        raise AdapterError(f"dave scenario must look like 'level1'..'level9', got {scenario_id!r}")
    level = int(scenario_id[5:])
    if not 1 <= level <= 9:
        raise AdapterError(f"deadly-dave ships level files 1-9, got {level}")
    return level


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()
