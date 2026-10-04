"""Phase 4: SQLite episode store (foreign keys, versioning, batching, restart durability, JSONL replay)."""

import json
import sqlite3

import pytest

from dave_agent.adapters.fixture import FixtureAdapter
from dave_agent.memory.episodes import STORE_SCHEMA_VERSION, EpisodeStore, StoreError
from dave_agent.memory.working import WorkingMemory
from dave_agent.models.mock import SeededMockController
from dave_agent.runner.episode import run_episode
from dave_agent.runner.replay import load_jsonl, replay_episode
from dave_agent.schemas import ModelCallRecord

from ..conftest import LEVELS


def _play(config, store, run_id="r1", controller=None, batch_size=200, seed=0, arm="A"):
    controller = controller or SeededMockController(seed=seed, label="mock-llm")
    store.create_run(run_id, mode="mock", command="test", config_json=config.model_dump_json())
    recorder = store.recorder(run_id, arm, controller.model, "fixture_l1", seed, batch_size)
    adapter = FixtureAdapter(LEVELS)
    try:
        result = run_episode(adapter, controller, config.skills.for_adapter("fixture"), config.skills.executor,
                             WorkingMemory.from_config(config), "fixture_l1", seed, 600, recorder)
    finally:
        adapter.close()
    return result, recorder.episode_key


def _count(store, table, key):
    return store.query(f"SELECT count(*) FROM {table} WHERE episode_key = ?", (key,))[0][0]


def test_schema_version_and_foreign_keys(tmp_path):
    path = tmp_path / "s.sqlite"
    with EpisodeStore(path) as store:
        assert store.query("PRAGMA user_version")[0][0] == STORE_SCHEMA_VERSION
        assert store.query("PRAGMA foreign_keys")[0][0] == 1
        with pytest.raises(sqlite3.IntegrityError):
            store._conn.execute("INSERT INTO episodes (episode_key, run_id, episode_id, arm, controller, adapter, "
                                "build_id, scenario_id, seed, level_id, started_at) "
                                "VALUES ('x', 'no-such-run', 'e', 'A', 'm', 'fixture', 'b', 's', 0, 'l', 't')")
    con = sqlite3.connect(path)
    con.execute("PRAGMA user_version = 99")
    con.commit()
    con.close()
    with pytest.raises(StoreError, match="schema version 99"):
        EpisodeStore(path)


def test_foreign_store_file_is_refused(tmp_path):
    path = tmp_path / "other.sqlite"
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE notes (x)")
    con.commit()
    con.close()
    with pytest.raises(StoreError, match="not an episode store"):
        EpisodeStore(path)


def test_episode_survives_restart_with_complete_linkage(config, tmp_path):
    path = tmp_path / "s.sqlite"
    with EpisodeStore(path) as store:
        result, key = _play(config, store)
    with EpisodeStore(path) as store:  # a new connection, as after a process restart
        (episode,) = store.query("SELECT * FROM episodes WHERE episode_key = ?", (key,))
        assert (episode["outcome"], episode["termination_reason"]) == ("game_over", "terminal:game_over")
        assert (episode["frames"], episode["deaths"]) == (result.frames, 3)
        assert episode["decisions"] == _count(store, "decisions", key) == len(result.decisions)
        assert _count(store, "skill_executions", key) == len(result.executions)
        assert _count(store, "model_calls", key) == len(result.model_calls)
        assert _count(store, "events", key) == len(result.events)
        # Every transition links its skill to its decision and its start and end observations.
        linked = store.query(
            "SELECT count(*) FROM skill_executions x JOIN decisions d ON d.episode_key = x.episode_key "
            "AND d.seq = x.decision_seq JOIN observations s ON s.episode_key = x.episode_key "
            "AND s.observation_id = x.start_observation_id JOIN observations e ON e.episode_key = x.episode_key "
            "AND e.observation_id = x.end_observation_id WHERE x.episode_key = ? AND d.candidate_id = x.candidate_id",
            (key,))[0][0]
        assert linked == len(result.executions)
        assert store.query("PRAGMA foreign_key_check") == []
        types = {r[0] for r in store.query("SELECT DISTINCT event_type FROM events WHERE episode_key = ?", (key,))}
        assert {"episode_start", "skill_started", "skill_finished", "death", "respawn", "game_over",
                "area_discovered"} <= types


def test_death_causes_are_logged_as_reported_never_inferred(config, tmp_path):
    with EpisodeStore(tmp_path / "s.sqlite") as store:
        _, key = _play(config, store)
        causes = [json.loads(r[0])["cause"] for r in store.query(
            "SELECT payload_json FROM events WHERE episode_key = ? AND event_type = 'death'", (key,))]
    # The fixture's rules define the cause; Dave reports "unknown" (tests/integration/test_dave_store.py).
    assert causes == ["hazard_tile:fire"] * 3


class _Probe:
    """Controller that observes how many decisions have reached the database (test-side only)."""

    def __init__(self, store, inner):
        self.store, self.inner, self.provider, self.model = store, inner, inner.provider, inner.model
        self.persisted = []

    def decide(self, observation, goal, candidates, memory):
        self.persisted.append(self.store.query("SELECT count(*) FROM decisions")[0][0])
        return self.inner.decide(observation, goal, candidates, memory)


@pytest.mark.parametrize(("batch_size", "flushes_during_episode"), [(10_000, False), (10, True)])
def test_writes_are_batched(config, tmp_path, batch_size, flushes_during_episode):
    with EpisodeStore(tmp_path / "s.sqlite") as store:
        probe = _Probe(store, SeededMockController(0, "mock-llm"))
        result, key = _play(config, store, controller=probe, batch_size=batch_size)
        assert (max(probe.persisted) > 0) is flushes_during_episode
        assert _count(store, "decisions", key) == len(result.decisions)  # finish() flushes the rest


class _Failing:
    provider, model = "mock", "failing"

    def __init__(self, fail_at):
        self.inner, self.fail_at, self.calls = SeededMockController(0, "x"), fail_at, 0

    def decide(self, observation, goal, candidates, memory):
        self.calls += 1
        if self.calls == self.fail_at:
            raise RuntimeError("provider exploded")
        decision, _ = self.inner.decide(observation, goal, candidates, memory)
        return decision, (ModelCallRecord(provider="mock", model="failing", purpose="tactical", latency_ms=None,
                                          status="error" if self.calls == 1 else "ok"),)


def test_model_failures_and_errored_episodes_are_logged(config, tmp_path):
    with EpisodeStore(tmp_path / "s.sqlite") as store:
        with pytest.raises(RuntimeError, match="provider exploded"):
            _play(config, store, controller=_Failing(fail_at=4))
        (episode,) = store.query("SELECT * FROM episodes")
        assert episode["outcome"] == "error" and "provider exploded" in episode["termination_reason"]
        assert episode["decisions"] == 3  # partial evidence is kept
        (failure,) = store.query("SELECT payload_json FROM events WHERE event_type = 'model_failure'")
        assert json.loads(failure[0])["status"] == "error"


def test_jsonl_export_replays_exactly(config, tmp_path):
    with EpisodeStore(tmp_path / "s.sqlite") as store:
        result, key = _play(config, store)
        count = store.export_jsonl(tmp_path / "out.jsonl")
    lines = (tmp_path / "out.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == count and json.loads(lines[0])["record"] == "run"
    records = load_jsonl(tmp_path / "out.jsonl")[key]
    adapter = FixtureAdapter(LEVELS)
    try:
        report = replay_episode(adapter, config.skills.for_adapter("fixture"), config.skills.executor, records)
    finally:
        adapter.close()
    assert report.ok, report.mismatches
    assert report.decisions == len(result.decisions)


def test_replay_detects_tampered_evidence(config, tmp_path):
    with EpisodeStore(tmp_path / "s.sqlite") as store:
        _, key = _play(config, store)
        store.export_jsonl(tmp_path / "out.jsonl")
    records = load_jsonl(tmp_path / "out.jsonl")[key]
    records["skill_execution"][2]["outcome"] = "failed"
    adapter = FixtureAdapter(LEVELS)
    try:
        report = replay_episode(adapter, config.skills.for_adapter("fixture"), config.skills.executor, records)
    finally:
        adapter.close()
    assert not report.ok and report.mismatches[0].startswith("decision 2: outcome")


def test_export_of_unknown_run_is_actionable(tmp_path):
    with EpisodeStore(tmp_path / "s.sqlite") as store, pytest.raises(StoreError, match="no runs"):
        store.export_jsonl(tmp_path / "x.jsonl", run_id="nope")
