"""Working memory: typed, bounded, in-process state for the current episode.

The latest observation says what is happening now; the recent-history deque says what
changed recently. Nothing here touches a database. Every arm builds identical memory from
identical observations and executions, and controllers see only ``context()``: a
deterministic, size-limited summary, never a growing transcript.
"""

from __future__ import annotations

from collections import deque

from dave_agent.config import AppConfig
from dave_agent.control.skills import ExecutionResult
from dave_agent.schemas import Contract, Decision, Goal, Observation, PixelPos, PixelVelocity, TilePos

TILE_PX = 16
# Outcomes that count towards the repeated-failure counter.
FAILED_OUTCOMES = frozenset({"failed", "interrupted"})
# Skill bookkeeping events are implied by the history entry itself.
_BOOKKEEPING_EVENTS = frozenset({"skill_started", "skill_finished"})


def player_tile(pos: PixelPos) -> TilePos:
    """The tile under the centre of the player's 16 px sprite box."""
    return TilePos(col=(pos.x + TILE_PX // 2) // TILE_PX, row=(pos.y + TILE_PX // 2) // TILE_PX)


class HistoryEntry(Contract):
    """One executed skill: the decision-level unit of the recent history."""

    start_frame: int
    end_frame: int
    start_observation_id: int
    end_observation_id: int
    candidate_id: str
    skill: str
    forced: bool
    outcome: str
    reason: str | None
    start_position: PixelPos | None
    end_position: PixelPos | None
    end_state: str | None
    events: tuple[str, ...]  # event types during the skill, in order

    @property
    def end_tile(self) -> TilePos | None:
        return player_tile(self.end_position) if self.end_position else None

    @property
    def moved_px(self) -> list[int] | None:
        """Observed displacement [dx, dy] in pixels from the skill's start to its end ([0, 0]: Dave
        did not move, e.g. walking into a wall or a jump blocked by the tile above)."""
        if self.start_position is None or self.end_position is None:
            return None
        return [self.end_position.x - self.start_position.x, self.end_position.y - self.start_position.y]


class Experience(Contract):
    """What one skill did when started from one tile, over the whole episode. Unlike the recent
    history it is not windowed, so it survives respawns."""

    attempts: int = 0
    deaths: int = 0  # a death event during the skill, or burning (death is then certain)
    burned: int = 0  # interrupted by hazard_contact
    no_move: int = 0  # moved_px == [0, 0]
    last_end: TilePos | None = None


class Motion(Contract):
    """Pixel displacement from the window start (or the last respawn) to now."""

    dx: int
    dy: int
    frames: int


class Progress(Contract):
    # Progress = score or inventory changed, or (with a waypoint) the best distance to it
    # improved, or (without one) a tile not visited before this episode was entered.
    no_progress_frames: int
    stuck: bool  # no_progress_frames >= planning.no_progress_frames
    consecutive_failures: int
    repeated_failures: bool  # consecutive_failures >= planning.repeated_skill_failures
    skill_repeats: int  # consecutive executions of the latest skill within the window
    tile_revisits: int  # earlier entries in the window that ended on the current tile
    tiles_visited: int


class MemoryContext(Contract):
    """What a controller may see besides the observation. Identical for every arm."""

    frame: int
    level_id: str
    goal: Goal | None
    last: HistoryEntry | None
    velocity: PixelVelocity | None
    motion: Motion | None
    progress: Progress
    recent: tuple[HistoryEntry, ...]


class WorkingMemory:
    def __init__(
        self,
        history_frames: int,
        max_entries: int,
        context_entries: int,
        no_progress_frames: int,
        failure_limit: int,
    ) -> None:
        self.history_frames = history_frames
        self.context_entries = context_entries
        self.no_progress_limit = no_progress_frames
        self.failure_limit = failure_limit
        self._history: deque[HistoryEntry] = deque(maxlen=max_entries)
        self.latest: Observation | None = None
        self.goal: Goal | None = None
        self.last_decision: Decision | None = None
        self._visited: set[tuple[int, int]] = set()
        self._last_progress_frame = 0
        self._best_goal_distance: int | None = None
        self._consecutive_failures = 0
        self._experience: dict[tuple[int, int], dict[str, Experience]] = {}

    @classmethod
    def from_config(cls, config: AppConfig) -> WorkingMemory:
        return cls(
            history_frames=config.memory.recent_history_frames,
            max_entries=config.memory.max_history_entries,
            context_entries=config.memory.context_entries,
            no_progress_frames=config.planning.no_progress_frames,
            failure_limit=config.planning.repeated_skill_failures,
        )

    @property
    def history(self) -> tuple[HistoryEntry, ...]:
        return tuple(self._history)

    def reset(self, observation: Observation) -> None:
        """Start a new episode: forget everything from the previous one."""
        self._history.clear()
        self._visited.clear()
        self.latest = observation
        self.goal = None
        self.last_decision = None
        self._best_goal_distance = None
        self._consecutive_failures = 0
        self._experience.clear()
        self._last_progress_frame = observation.frame
        self._visit(observation)

    def set_goal(self, goal: Goal | None, restart_clock: bool = True) -> None:
        """Adopt a goal from the goal manager. A new goal restarts the no-progress clock; a
        waypoint-only update (``restart_clock=False``) only re-measures the waypoint distance."""
        latest = self._require()
        self.goal = goal
        self._best_goal_distance = self._goal_distance(latest)
        if restart_clock:
            self._last_progress_frame = latest.frame

    def record(self, decision: Decision, run: ExecutionResult) -> None:
        """Fold one executed skill into memory. ``run`` must start from ``latest``."""
        start = self._require()
        if run.observation.episode_id != start.episode_id:
            raise ValueError(
                f"execution belongs to episode {run.observation.episode_id}, memory holds "
                f"{start.episode_id}; call reset() at the start of each episode"
            )
        previous = start
        for step in run.steps:
            if self._progressed(previous, step.observation):
                self._last_progress_frame = step.observation.frame
            previous = step.observation

        end = run.observation
        self._history.append(
            HistoryEntry(
                start_frame=start.frame,
                end_frame=end.frame,
                start_observation_id=start.observation_id,
                end_observation_id=end.observation_id,
                candidate_id=run.candidate_id,
                skill=run.skill,
                forced=decision.forced,
                outcome=run.outcome,
                reason=run.reason,
                start_position=start.player_position,
                end_position=end.player_position,
                end_state=end.player_state,
                events=tuple(e.event_type for e in run.events if e.event_type not in _BOOKKEEPING_EVENTS),
            )
        )
        self._remember(self._history[-1])
        while self._history[0].end_frame < end.frame - self.history_frames:
            self._history.popleft()
        self._consecutive_failures = self._consecutive_failures + 1 if run.outcome in FAILED_OUTCOMES else 0
        self.last_decision = decision
        self.latest = end

    def advance(self, observations: list[Observation]) -> None:
        """Real-time mode: the game ran on (no keys pressed) while a model decided. Moves
        ``latest`` and the progress clock; no skill ran, so the history is unchanged."""
        previous = self._require()
        for obs in observations:
            if self._progressed(previous, obs):
                self._last_progress_frame = obs.frame
            previous = obs
        self.latest = previous

    def experience(self, tile: TilePos) -> dict[str, Experience]:
        """Per skill, what starting it from ``tile`` did earlier this episode (empty when untried)."""
        return dict(self._experience.get((tile.col, tile.row), {}))

    def motion(self) -> Motion | None:
        latest = self._require()
        if not self._history or latest.player_position is None:
            return None
        origin, origin_frame = self._history[0].start_position, self._history[0].start_frame
        for entry in self._history:
            if "respawn" in entry.events or "death" in entry.events:
                origin, origin_frame = entry.end_position, entry.end_frame
        if origin is None:
            return None
        return Motion(
            dx=latest.player_position.x - origin.x,
            dy=latest.player_position.y - origin.y,
            frames=latest.frame - origin_frame,
        )

    def progress(self) -> Progress:
        latest = self._require()
        no_progress = latest.frame - self._last_progress_frame
        repeats = 0
        if self._history:
            skill = self._history[-1].skill
            for entry in reversed(self._history):
                if entry.skill != skill:
                    break
                repeats += 1
        here = player_tile(latest.player_position) if latest.player_position else None
        revisits = sum(1 for e in list(self._history)[:-1] if here is not None and e.end_tile == here)
        return Progress(
            no_progress_frames=no_progress,
            stuck=no_progress >= self.no_progress_limit,
            consecutive_failures=self._consecutive_failures,
            repeated_failures=self._consecutive_failures >= self.failure_limit,
            skill_repeats=repeats,
            tile_revisits=revisits,
            tiles_visited=len(self._visited),
        )

    def context(self) -> MemoryContext:
        latest = self._require()
        return MemoryContext(
            frame=latest.frame,
            level_id=latest.level_id,
            goal=self.goal,
            last=self._history[-1] if self._history else None,
            velocity=latest.player_velocity,
            motion=self.motion(),
            progress=self.progress(),
            recent=tuple(self._history)[-self.context_entries:],
        )

    # -- internals ------------------------------------------------------
    def _remember(self, entry: HistoryEntry) -> None:
        if entry.start_position is None:
            return
        start = player_tile(entry.start_position)
        table = self._experience.setdefault((start.col, start.row), {})
        old = table.get(entry.skill, Experience())
        burned = entry.reason == "hazard_contact"
        table[entry.skill] = Experience(
            attempts=old.attempts + 1,
            deaths=old.deaths + int(burned or "death" in entry.events),
            burned=old.burned + int(burned),
            no_move=old.no_move + int(entry.moved_px == [0, 0]),
            last_end=entry.end_tile,
        )

    def _require(self) -> Observation:
        if self.latest is None:
            raise ValueError("working memory is empty; call reset(observation) first")
        return self.latest

    def _visit(self, obs: Observation) -> bool:
        if obs.player_position is None:
            return False
        tile = player_tile(obs.player_position)
        key = (tile.col, tile.row)
        if key in self._visited:
            return False
        self._visited.add(key)
        return True

    def _goal_distance(self, obs: Observation) -> int | None:
        if self.goal is None or self.goal.next_waypoint is None or obs.player_position is None:
            return None
        tile, target = player_tile(obs.player_position), self.goal.next_waypoint
        return abs(tile.col - target.col) + abs(tile.row - target.row)

    def _progressed(self, before: Observation, after: Observation) -> bool:
        new_tile = self._visit(after)  # always track visits, even when a waypoint is set
        if after.score is not None and before.score is not None and after.score > before.score:
            return True
        if after.inventory != before.inventory:
            return True
        distance = self._goal_distance(after)
        if distance is None:
            return new_tile
        if self._best_goal_distance is None or distance < self._best_goal_distance:
            self._best_goal_distance = distance
            return True
        return False
