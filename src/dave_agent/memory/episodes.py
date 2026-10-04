"""SQLite episode store: durable evidence of runs, episodes, decisions, skill executions,
model calls and events, with a JSONL export.

The control loop only writes here. ``EpisodeRecorder`` buffers rows and flushes them in
batches at decision boundaries (never inside the frame loop), and nothing in the loop reads
them back, so logging cannot give any arm extra planning context. Every skill execution
links to its decision and to its start and end observations.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from dave_agent.control.skills import CandidateSet, ExecutionResult
from dave_agent.schemas import Decision, Event, ModelCallRecord, Observation

STORE_SCHEMA_VERSION = 2  # 2: decisions.context_digest, model_calls.output_json

_SCHEMA = """
CREATE TABLE runs (
    run_id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    mode TEXT NOT NULL,                 -- mock | live
    command TEXT NOT NULL,
    config_json TEXT NOT NULL
);
CREATE TABLE episodes (
    episode_key TEXT PRIMARY KEY,       -- run_id/episode_id
    run_id TEXT NOT NULL REFERENCES runs(run_id),
    episode_id TEXT NOT NULL,
    arm TEXT NOT NULL,
    controller TEXT NOT NULL,
    adapter TEXT NOT NULL,
    build_id TEXT NOT NULL,
    scenario_id TEXT NOT NULL,
    seed INTEGER NOT NULL,
    level_id TEXT NOT NULL,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    outcome TEXT,                       -- NULL until finished (a crash leaves it NULL)
    termination_reason TEXT,
    frames INTEGER,
    score INTEGER,
    lives INTEGER,
    deaths INTEGER,
    decisions INTEGER,
    model_calls INTEGER,
    UNIQUE (run_id, episode_id)
);
CREATE TABLE observations (
    episode_key TEXT NOT NULL REFERENCES episodes(episode_key),
    observation_id INTEGER NOT NULL,
    frame INTEGER NOT NULL,
    observation_json TEXT NOT NULL,
    PRIMARY KEY (episode_key, observation_id)
);
CREATE TABLE model_calls (
    episode_key TEXT NOT NULL REFERENCES episodes(episode_key),
    seq INTEGER NOT NULL,
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    purpose TEXT NOT NULL,
    status TEXT NOT NULL,
    latency_ms REAL,
    retries INTEGER NOT NULL,
    usage_json TEXT,
    cost_usd REAL,
    cost_source TEXT,
    request_ref TEXT,
    response_ref TEXT,
    output_json TEXT,                   -- provider answer details as reported (e.g. Jev probabilities)
    PRIMARY KEY (episode_key, seq)
);
CREATE TABLE decisions (
    episode_key TEXT NOT NULL REFERENCES episodes(episode_key),
    seq INTEGER NOT NULL,
    observation_id INTEGER NOT NULL,
    frame INTEGER NOT NULL,
    candidate_id TEXT NOT NULL,
    goal_id TEXT,
    forced INTEGER NOT NULL,
    fallback INTEGER NOT NULL,
    provider_score REAL,
    provider_score_meaning TEXT,
    candidate_ids_json TEXT NOT NULL,
    masked_json TEXT NOT NULL,
    candidate_digest TEXT NOT NULL,
    context_digest TEXT,                -- digest of the shared tactical request; NULL when forced
    model_call_seq INTEGER,
    PRIMARY KEY (episode_key, seq),
    FOREIGN KEY (episode_key, observation_id) REFERENCES observations(episode_key, observation_id),
    FOREIGN KEY (episode_key, model_call_seq) REFERENCES model_calls(episode_key, seq)
);
CREATE TABLE skill_executions (
    episode_key TEXT NOT NULL,
    decision_seq INTEGER NOT NULL,
    candidate_id TEXT NOT NULL,
    skill TEXT NOT NULL,
    outcome TEXT NOT NULL,
    reason TEXT,
    frames INTEGER NOT NULL,
    input_ticks INTEGER NOT NULL,
    start_observation_id INTEGER NOT NULL,
    end_observation_id INTEGER NOT NULL,
    PRIMARY KEY (episode_key, decision_seq),
    FOREIGN KEY (episode_key, decision_seq) REFERENCES decisions(episode_key, seq),
    FOREIGN KEY (episode_key, start_observation_id) REFERENCES observations(episode_key, observation_id),
    FOREIGN KEY (episode_key, end_observation_id) REFERENCES observations(episode_key, observation_id)
);
CREATE TABLE events (
    episode_key TEXT NOT NULL REFERENCES episodes(episode_key),
    seq INTEGER NOT NULL,
    frame INTEGER NOT NULL,
    event_type TEXT NOT NULL,
    certainty TEXT NOT NULL,
    location_col INTEGER,
    location_row INTEGER,
    entity_refs_json TEXT NOT NULL,
    evidence_json TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    decision_seq INTEGER,               -- the skill execution the event happened during
    PRIMARY KEY (episode_key, seq),
    FOREIGN KEY (episode_key, decision_seq) REFERENCES skill_executions(episode_key, decision_seq)
);
CREATE INDEX events_by_type ON events(event_type);
"""

# Parent tables first, so foreign keys hold within each flushed batch.
_TABLE_ORDER = ("observations", "model_calls", "decisions", "skill_executions", "events")
_COLUMNS = {
    "observations": ("episode_key", "observation_id", "frame", "observation_json"),
    "model_calls": ("episode_key", "seq", "provider", "model", "purpose", "status", "latency_ms", "retries",
                    "usage_json", "cost_usd", "cost_source", "request_ref", "response_ref", "output_json"),
    "decisions": ("episode_key", "seq", "observation_id", "frame", "candidate_id", "goal_id", "forced",
                  "fallback", "provider_score", "provider_score_meaning", "candidate_ids_json", "masked_json",
                  "candidate_digest", "context_digest", "model_call_seq"),
    "skill_executions": ("episode_key", "decision_seq", "candidate_id", "skill", "outcome", "reason", "frames",
                         "input_ticks", "start_observation_id", "end_observation_id"),
    "events": ("episode_key", "seq", "frame", "event_type", "certainty", "location_col", "location_row",
               "entity_refs_json", "evidence_json", "payload_json", "decision_seq"),
}
_INSERT = {
    table: f"INSERT {'OR IGNORE ' if table == 'observations' else ''}INTO {table} "
           f"({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})"
    for table, cols in _COLUMNS.items()
}


class StoreError(RuntimeError):
    """The episode store cannot be used as requested."""


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, default=str)


class EpisodeStore:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        version = self._conn.execute("PRAGMA user_version").fetchone()[0]
        if version == 0:
            tables = self._conn.execute("SELECT count(*) FROM sqlite_master WHERE type='table'").fetchone()[0]
            if tables:
                raise StoreError(f"{self.path} is not an episode store (unversioned tables present)")
            with self._conn:
                self._conn.executescript(_SCHEMA)
                self._conn.execute(f"PRAGMA user_version = {STORE_SCHEMA_VERSION}")
        elif version != STORE_SCHEMA_VERSION:
            raise StoreError(
                f"{self.path} has store schema version {version}, this code writes version "
                f"{STORE_SCHEMA_VERSION}; export it to JSONL and start a new store file"
            )

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> EpisodeStore:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- writing --------------------------------------------------------
    def create_run(self, run_id: str, mode: str, command: str, config_json: str) -> None:
        with self._conn:
            self._conn.execute(
                "INSERT INTO runs (run_id, created_at, mode, command, config_json) VALUES (?, ?, ?, ?, ?)",
                (run_id, _now(), mode, command, config_json),
            )

    def recorder(self, run_id: str, arm: str, controller: str, scenario_id: str, seed: int,
                 batch_size: int) -> EpisodeRecorder:
        return EpisodeRecorder(self, run_id, arm, controller, scenario_id, seed, batch_size)

    def _write(self, pending: dict[str, list[tuple]]) -> None:
        with self._conn:
            for table in _TABLE_ORDER:
                if pending[table]:
                    self._conn.executemany(_INSERT[table], pending[table])

    # -- reading (never from the control loop) ---------------------------
    def query(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        return self._conn.execute(sql, params).fetchall()

    def iter_records(self, run_id: str | None = None) -> Iterator[dict[str, Any]]:
        """Every row as a dict tagged with ``record``, JSON columns decoded, in replay order:
        the run, then per episode its row, observations, model calls, decisions, executions, events."""
        where, params = ("WHERE run_id = ?", (run_id,)) if run_id else ("", ())
        for run in self.query(f"SELECT * FROM runs {where} ORDER BY created_at, run_id", params):
            yield {"record": "run", **_decode(run)}
            episodes = self.query("SELECT * FROM episodes WHERE run_id = ? ORDER BY started_at, episode_key",
                                  (run["run_id"],))
            for episode in episodes:
                key = episode["episode_key"]
                yield {"record": "episode", **_decode(episode)}
                for table, order in (("observations", "observation_id"), ("model_calls", "seq"),
                                     ("decisions", "seq"), ("skill_executions", "decision_seq"),
                                     ("events", "seq")):
                    record = table.removesuffix("s")
                    for row in self.query(f"SELECT * FROM {table} WHERE episode_key = ? ORDER BY {order}", (key,)):
                        yield {"record": record, **_decode(row)}

    def export_jsonl(self, out: Path | str, run_id: str | None = None) -> int:
        out = Path(out)
        out.parent.mkdir(parents=True, exist_ok=True)
        count = 0
        with out.open("w", encoding="utf-8") as fh:
            for record in self.iter_records(run_id):
                fh.write(json.dumps(record, sort_keys=True) + "\n")
                count += 1
        if count == 0:
            raise StoreError(f"no runs to export{f' for run_id {run_id!r}' if run_id else ''} in {self.path}")
        return count


def _decode(row: sqlite3.Row) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key in row.keys():
        value = row[key]
        if key.endswith("_json"):
            out[key.removesuffix("_json")] = None if value is None else json.loads(value)
        else:
            out[key] = value
    return out


class EpisodeRecorder:
    """Write-only log of one episode. Identical for every arm."""

    def __init__(self, store: EpisodeStore, run_id: str, arm: str, controller: str, scenario_id: str,
                 seed: int, batch_size: int) -> None:
        self.store, self.run_id, self.arm, self.controller = store, run_id, arm, controller
        self.scenario_id, self.seed, self.batch_size = scenario_id, seed, batch_size
        self.episode_key: str | None = None
        self._pending: dict[str, list[tuple]] = {t: [] for t in _TABLE_ORDER}
        self._event_seq = self._decision_seq = self._call_seq = 0
        self._deaths = 0

    def start(self, observation: Observation, events: list[Event]) -> None:
        self.episode_key = f"{self.run_id}/{observation.episode_id}"
        with self.store._conn:
            self.store._conn.execute(
                "INSERT INTO episodes (episode_key, run_id, episode_id, arm, controller, adapter, build_id, "
                "scenario_id, seed, level_id, started_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (self.episode_key, self.run_id, observation.episode_id, self.arm, self.controller,
                 observation.adapter, observation.build_id, self.scenario_id, self.seed, observation.level_id,
                 _now()),
            )
        self._observation(observation)
        self._events(events, None)
        self.flush()

    def record(self, observation: Observation, offered: CandidateSet, decision: Decision,
               calls: Sequence[ModelCallRecord], run: ExecutionResult, events: list[Event]) -> None:
        """One decision point: the observation it was made on, what was offered, the choice,
        every model call made for it (none when forced; retries included), the execution and
        every event during it. ``model_call_seq`` links the decision to its last call."""
        key = self._key()
        call_seq = None
        for call in calls:
            call_seq = self._call(call)
        seq = self._decision_seq
        self._decision_seq += 1
        self._observation(observation)
        self._pending["decisions"].append(
            (key, seq, observation.observation_id, observation.frame, decision.candidate_id, decision.goal_id,
             int(decision.forced), int(decision.fallback), decision.provider_score,
             decision.provider_score_meaning, _json(list(offered.ids)), _json(offered.masked), offered.digest(),
             decision.context_digest, call_seq)
        )
        self._observation(run.observation)
        self._pending["skill_executions"].append(
            (key, seq, run.candidate_id, run.skill, run.outcome, run.reason, run.frames, run.input_ticks,
             observation.observation_id, run.observation.observation_id)
        )
        self._events(events, seq)
        if sum(len(rows) for rows in self._pending.values()) >= self.batch_size:
            self.flush()

    def record_planning(self, calls: list[ModelCallRecord], events: list[Event]) -> None:
        """Planner calls and goal events at a decision boundary (not tied to one skill)."""
        for call in calls:
            self._call(call)
        self._events(events, None)

    def finish(self, outcome: str, reason: str, observation: Observation | None, events: list[Event]) -> None:
        key = self._key()
        self._events(events, None)
        self.flush()
        with self.store._conn:
            self.store._conn.execute(
                "UPDATE episodes SET ended_at = ?, outcome = ?, termination_reason = ?, frames = ?, score = ?, "
                "lives = ?, deaths = ?, decisions = ?, model_calls = ? WHERE episode_key = ?",
                (_now(), outcome, reason, None if observation is None else observation.frame,
                 None if observation is None else observation.score,
                 None if observation is None else observation.lives,
                 self._deaths, self._decision_seq, self._call_seq, key),
            )

    def flush(self) -> None:
        if any(self._pending.values()):
            self.store._write(self._pending)
            self._pending = {t: [] for t in _TABLE_ORDER}

    def _key(self) -> str:
        if self.episode_key is None:
            raise StoreError("recorder not started; call start(observation, events) first")
        return self.episode_key

    def _call(self, call: ModelCallRecord) -> int:
        seq = self._call_seq
        self._call_seq += 1
        self._pending["model_calls"].append(
            (self._key(), seq, call.provider, call.model, call.purpose, call.status, call.latency_ms, call.retries,
             None if call.usage is None else _json(call.usage), call.cost_usd, call.cost_source, call.request_ref,
             call.response_ref, None if call.output is None else _json(call.output))
        )
        return seq

    def _observation(self, observation: Observation) -> None:
        self._pending["observations"].append(
            (self._key(), observation.observation_id, observation.frame, observation.model_dump_json())
        )

    def _events(self, events: list[Event], decision_seq: int | None) -> None:
        key = self._key()
        for event in events:
            self._deaths += event.event_type == "death"
            loc = event.location
            self._pending["events"].append(
                (key, self._event_seq, event.frame, event.event_type, event.certainty,
                 None if loc is None else loc.col, None if loc is None else loc.row,
                 _json(list(event.entity_refs)), _json(list(event.evidence)), _json(event.payload), decision_seq)
            )
            self._event_seq += 1
