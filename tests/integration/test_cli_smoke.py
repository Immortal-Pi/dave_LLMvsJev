"""Offline end-to-end smoke tests through the CLI entry point (no network)."""

import json

import pytest

from dave_agent.cli import main

from ..conftest import CONFIG, ROOT

STORE = None  # set per test: play never writes into the repo's artifacts/ during tests


@pytest.fixture(autouse=True)
def _tmp_store(tmp_path):
    global STORE
    STORE = tmp_path / "events.sqlite"


def _run(capsys, *argv):
    if argv and argv[0] == "play" and "--store" not in argv:
        argv = (*argv, "--store", str(STORE))
    code = main(list(argv))
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def test_probe_fixture_changes_observation(capsys):
    code, out, _ = _run(capsys, "probe", "--adapter", "fixture", "--config", str(CONFIG))
    report = json.loads(out)
    assert code == 0 and report["observation_changed"] is True
    assert report["adapter"] == "fixture"


@pytest.mark.parametrize("arm", ["A", "B"])
def test_mock_play_reaches_deterministic_terminal(capsys, arm):
    _, first, _ = _run(capsys, "play", "--arm", arm, "--mock", "--config", str(CONFIG))
    _, second, _ = _run(capsys, "play", "--arm", arm, "--mock", "--config", str(CONFIG))
    a, b = json.loads(first), json.loads(second)
    for run_label in ("run_id", "episode_key"):
        assert a.pop(run_label) != b.pop(run_label)
    assert a == b
    assert a["mode"] == "mock" and a["adapter"] == "fixture"
    assert a["outcome"] in {"level_complete", "game_over", "truncated"}


def test_arms_share_mock_trajectory(capsys):
    _, out_a, _ = _run(capsys, "play", "--arm", "A", "--mock", "--config", str(CONFIG))
    _, out_b, _ = _run(capsys, "play", "--arm", "B", "--mock", "--config", str(CONFIG))
    a, b = json.loads(out_a), json.loads(out_b)
    for key in ("outcome", "frames", "decisions", "score", "deaths"):
        assert a[key] == b[key]


def test_default_play_is_offline_and_counts_model_decisions(capsys):
    code, out, _ = _run(capsys, "play", "--arm", "A", "--config", str(CONFIG))
    summary = json.loads(out)
    assert code == 0 and summary["mode"] == "mock"
    assert summary["model_decisions"] + summary["fallback_decisions"] + summary["forced_decisions"]         == summary["decisions"]
    assert summary["tactical_calls"] == summary["model_decisions"] and summary["fallback_decisions"] == 0
    assert summary["settings"]["tactical"] == {"provider": "mock", "model": "mock-llm"}


@pytest.mark.parametrize(("argv", "message"), [
    (("--arm", "A", "--mock", "--tactical", "live"), "--mock"),
])
def test_live_tactical_refused_for_wrong_arm_or_mock(capsys, argv, message):
    code, _, err = _run(capsys, "play", *argv, "--config", str(CONFIG))
    assert code == 2 and message in err
    assert not STORE.exists()


def test_live_tactical_needs_azure_settings_before_running(capsys, monkeypatch):
    from dave_agent.models import azure

    monkeypatch.setattr(azure, "load_dotenv", lambda: None)
    monkeypatch.delenv("AZURE_OPENAI_API_KEY", raising=False)
    code, _, err = _run(capsys, "play", "--arm", "A", "--tactical", "live", "--config", str(CONFIG))
    assert code == 2 and "AZURE_OPENAI_API_KEY" in err
    assert not STORE.exists()  # refused before the game or the store was touched


@pytest.mark.parametrize("arm", ["B", "C"])
def test_live_jev_tactical_needs_openrouter_key_before_running(capsys, monkeypatch, arm):
    from dave_agent.models import jev

    monkeypatch.setattr(jev, "load_dotenv", lambda: None)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    code, _, err = _run(capsys, "play", "--arm", arm, "--tactical", "live", "--config", str(CONFIG))
    assert code == 2 and "OPENROUTER_API_KEY" in err
    assert not STORE.exists()  # refused before the game or the store was touched


def test_probe_jev_planner_purpose_refused(capsys):
    code, _, err = _run(capsys, "probe-provider", "--provider", "jev", "--purpose", "planner",
                        "--config", str(CONFIG))
    assert code == 2 and "tactical" in err


def test_live_planner_needs_azure_settings_before_running(capsys, monkeypatch):
    from dave_agent.models import azure

    monkeypatch.setattr(azure, "load_dotenv", lambda: None)
    monkeypatch.delenv("AZURE_OPENAI_API_KEY", raising=False)
    code, _, err = _run(capsys, "play", "--arm", "A", "--mock", "--planner", "live", "--config", str(CONFIG))
    assert code == 2 and "AZURE_OPENAI_API_KEY" in err
    assert not STORE.exists()  # refused before the game or the store was touched


def test_dave_probe(capsys):
    """Real game when the bridge is built; otherwise an actionable setup error."""
    code, out, err = _run(capsys, "probe", "--adapter", "dave", "--scenario", "level1", "--config", str(CONFIG))
    if (ROOT / "external" / "deadly-dave" / "deadly-dave-bridge.exe").exists():
        assert code == 0 and json.loads(out)["observation_changed"] is True
    else:
        assert code == 2 and "setup_dave.bat" in err


def test_unknown_arm_is_actionable(capsys):
    code, _, err = _run(capsys, "play", "--arm", "Z", "--mock", "--config", str(CONFIG))
    assert code == 2 and "configured arms" in err


def test_play_export_replay_roundtrip(capsys, tmp_path):
    code, out, _ = _run(capsys, "play", "--arm", "B", "--mock", "--config", str(CONFIG), "--run-id", "smoke")
    assert code == 0 and json.loads(out)["episode_key"] == "smoke/fixture_l1-s0-e1"
    jsonl = tmp_path / "smoke.jsonl"
    code, out, _ = _run(capsys, "export", "--config", str(CONFIG), "--store", str(STORE), "--run-id", "smoke",
                        "--out", str(jsonl))
    assert code == 0 and json.loads(out)["records"] > 0
    records = [json.loads(line) for line in jsonl.read_text(encoding="utf-8").splitlines()]
    planner_calls = [r for r in records if r["record"] == "model_call" and r["purpose"] == "planner"]
    goal_sets = [r for r in records if r["record"] == "event" and r["event_type"] == "goal_set"]
    assert planner_calls and goal_sets and goal_sets[0]["payload"]["triggers"] == ["no_goal"]
    decisions = [r for r in records if r["record"] == "decision"]
    assert all(d["goal_id"] == "g1" for d in decisions[:1])  # decisions carry the active goal
    code, out, _ = _run(capsys, "replay", "--config", str(CONFIG), "--jsonl", str(jsonl))
    (report,) = json.loads(out)
    assert code == 0 and report["ok"] and report["decisions"] == 17


def test_graph_arm_learns_checkpoint_and_graph_command_inspects_it(capsys, tmp_path):
    ckpt = tmp_path / "arm-C" / "fixture.json"
    for _ in range(2):
        code, out, _ = _run(capsys, "play", "--arm", "C", "--mock", "--config", str(CONFIG), "--graph", str(ckpt))
        assert code == 0 and json.loads(out)["graph"]["nodes"] > 0
    code, out, _ = _run(capsys, "graph", "--config", str(CONFIG), "--checkpoint", str(ckpt),
                        "--yaml", str(tmp_path / "g.yaml"), "--route", "fixture_l1:r4:c1", "nowhere")
    report = json.loads(out)
    assert code == 0 and report["episodes"] == 2 and (tmp_path / "g.yaml").exists()
    assert report["route"]["status"] == "unreachable" and report["route"]["reason"] == "unknown_target"
    # Graph-disabled arms never create or read a checkpoint.
    code, out, _ = _run(capsys, "play", "--arm", "A", "--mock", "--config", str(CONFIG))
    assert code == 0 and json.loads(out)["graph"] is None


def test_interrupted_play_keeps_its_episode_and_graph(capsys, tmp_path, monkeypatch):
    """Ctrl-C mid-episode: the recorded decisions are written, the episode row is finished as
    truncated/interrupted, and a graph arm still saves what it learned."""
    import dataclasses
    import sqlite3

    from dave_agent import cli
    from dave_agent.runner import session

    class StopAfter:
        def __init__(self, inner, n):
            self.inner, self.n, self.provider, self.model = inner, n, inner.provider, inner.model

        def decide(self, *args):
            self.n -= 1
            if self.n < 0:
                raise KeyboardInterrupt
            return self.inner.decide(*args)

    def interrupting(*args):
        models = session.build_models(*args)
        return dataclasses.replace(models, controller=StopAfter(models.controller, 4))
    monkeypatch.setattr(cli, "build_models", interrupting)
    ckpt = tmp_path / "g.json"
    code, out, err = _run(capsys, "play", "--arm", "C", "--mock", "--config", str(CONFIG), "--graph", str(ckpt),
                          "--run-id", "stopped")
    assert code == 130 and out == ""
    report = json.loads(err)
    store = tmp_path / "g"  # a store path X.json is the directory X, one checkpoint per level
    assert report["interrupted"] and report["graph_checkpoint"] == str(store) and store.is_dir()
    with sqlite3.connect(STORE) as db:
        outcome, reason, decisions = db.execute(
            "SELECT outcome, termination_reason, decisions FROM episodes WHERE run_id = 'stopped'").fetchone()
        rows = db.execute("SELECT COUNT(*) FROM decisions").fetchone()[0]
    assert (outcome, reason) == ("truncated", "interrupted") and decisions == rows >= 4
    (level,) = store.glob("*.json")
    assert json.loads(level.read_text(encoding="utf-8"))["lineage"][0]["run_id"] == "stopped"
