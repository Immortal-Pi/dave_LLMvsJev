"""Phase 4 on the real game: episode logging, unknown death causes and exact JSONL replay.
Skipped when the deadly-dave bridge is not built."""

import json

import pytest

from dave_agent.adapters.dave import BRIDGE_EXE, DaveBridgeAdapter
from dave_agent.memory.episodes import EpisodeStore
from dave_agent.memory.working import WorkingMemory
from dave_agent.models.mock import SeededMockController
from dave_agent.runner.episode import run_episode
from dave_agent.runner.replay import load_jsonl, replay_episode

from ..conftest import ROOT

DAVE_DIR = ROOT / "external" / "deadly-dave"
pytestmark = [
    pytest.mark.dave,
    pytest.mark.skipif(not (DAVE_DIR / BRIDGE_EXE).exists(), reason="deadly-dave bridge not built"),
]


def test_level2_episode_logged_with_unknown_death_cause_and_replays(config, tmp_path):
    catalog, executor = config.skills.for_adapter("dave"), config.skills.executor
    with EpisodeStore(tmp_path / "dave.sqlite") as store:
        store.create_run("dave", mode="mock", command="test", config_json=config.model_dump_json())
        recorder = store.recorder("dave", "A", "mock-llm", "level2", 0, config.memory.store_batch_size)
        adapter = DaveBridgeAdapter(DAVE_DIR)
        try:
            result = run_episode(adapter, SeededMockController(0, "mock-llm"), catalog, executor,
                                 WorkingMemory.from_config(config), "level2", 0, 600, recorder)
        finally:
            adapter.close()
        key = recorder.episode_key
        deaths = [json.loads(r[0]) for r in store.query(
            "SELECT payload_json FROM events WHERE episode_key = ? AND event_type = 'death'", (key,))]
        forced = store.query("SELECT count(*) FROM decisions WHERE episode_key = ? AND forced = 1", (key,))[0][0]
        store.export_jsonl(tmp_path / "dave.jsonl")

    # The bridge does not report what ignited Dave, so nothing may claim a cause.
    assert deaths and all(d == {"cause": "unknown"} for d in deaths)
    assert forced == sum(d.forced for d in result.decisions) > 0

    adapter = DaveBridgeAdapter(DAVE_DIR)
    try:
        report = replay_episode(adapter, catalog, executor, load_jsonl(tmp_path / "dave.jsonl")[key])
    finally:
        adapter.close()
    assert report.ok, report.mismatches
    assert report.decisions == len(result.decisions)
