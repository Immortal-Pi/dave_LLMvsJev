"""Decision inspector: a recorded mock episode is rebuilt exactly (every request digest matches),
each decision bundle holds the exact request bodies and what every offered candidate really
does, and anything that would make the rebuild inexact is refused."""

import json
import shutil
import sqlite3

import pytest

from dave_agent.adapters.dave import BRIDGE_EXE
from dave_agent.cli import main
from dave_agent.config import ConfigError
from dave_agent.models.jev import QUESTION_ID
from dave_agent.runner.inspect import InspectError, inspect_run, parse_selection

from ..conftest import CONFIG, ROOT


def play(tmp_path, run_id, arm="A", *extra):
    store = tmp_path / "events.sqlite"
    assert main(["play", "--arm", arm, "--mock", "--config", str(CONFIG), "--store", str(store),
                 "--run-id", run_id, *extra]) == 0
    return store


def read(out):
    run = json.loads((out / "run.json").read_text(encoding="utf-8"))
    decisions = {int(p.stem): json.loads(p.read_text(encoding="utf-8"))
                 for p in sorted((out / "decisions").glob("*.json"))}
    return run, decisions


def test_rebuild_is_exact_and_the_bundle_shows_what_the_models_saw(tmp_path, capsys):
    store = play(tmp_path, "r1")
    capsys.readouterr()
    report = inspect_run(store, "r1", tmp_path / "out")
    run, decisions = read(tmp_path / "out")
    model_chosen = [d for d in run["decisions"] if not d["forced"]]
    assert report["digests_verified"] == run["digests_verified"] == len(model_chosen) == len(decisions) > 0
    assert run["arm"] == "A" and run["graph"] is None and run["episode"]["decisions"] == len(run["decisions"])
    for seq, d in decisions.items():
        offered = [c["id"] for c in d["request"]["candidates"]]
        assert d["digest_match"] and d["chosen"] in offered
        # The exact provider bodies: Jev's state is the request; Azure's user message is the same JSON.
        assert set(d["bodies"]["jev"]["questions"][QUESTION_ID]["criteria"]) == set(offered)
        user = json.loads(d["bodies"]["azure"]["messages"][1]["content"])
        assert user["candidates"] == d["request"]["candidates"]
        # Every offered candidate was really executed from this state; the chosen one ends where
        # the recorded execution ended.
        assert [o["candidate_id"] for o in d["outcomes"]] == offered
        chosen = next(o for o in d["outcomes"] if o["candidate_id"] == d["chosen"])
        assert chosen["end_tile"] == d["executed"]["end_tile"] and chosen["outcome"] == d["executed"]["outcome"]
        assert d["screenshot"] is None  # the fixture adapter has no renderer
        assert run["decisions"][[x["seq"] for x in run["decisions"]].index(seq)]["inspected"]


def test_inspection_is_deterministic_and_selection_limits_the_bundle(tmp_path, capsys):
    store = play(tmp_path, "r1")
    inspect_run(store, "r1", tmp_path / "a")
    inspect_run(store, "r1", tmp_path / "b")
    (run_a, dec_a), (run_b, dec_b) = read(tmp_path / "a"), read(tmp_path / "b")
    for run in (run_a, run_b):
        run.pop("inspected_at")
    assert run_a == run_b and dec_a == dec_b
    first = min(dec_a)
    report = inspect_run(store, "r1", tmp_path / "c", select={first}, outcomes=False)
    _, only = read(tmp_path / "c")
    assert report["decisions_written"] == 1 and list(only) == [first] and only[first]["outcomes"] is None
    assert parse_selection("90-92,95") == {90, 91, 92, 95} and parse_selection(None) is None


def test_a_wrong_recorded_digest_stops_the_inspection(tmp_path, capsys):
    store = play(tmp_path, "r1")
    broken = tmp_path / "broken.sqlite"
    shutil.copy(store, broken)
    with sqlite3.connect(broken) as db:
        seq = db.execute("SELECT MIN(seq) FROM decisions WHERE forced = 0").fetchone()[0]
        db.execute("UPDATE decisions SET context_digest = 'sha256:0000000000000000' WHERE seq = ?", (seq,))
    with pytest.raises(InspectError, match=f"decision {seq} .*diverged"):
        inspect_run(broken, "r1", tmp_path / "out")
    with pytest.raises(InspectError, match="not found"):
        inspect_run(store, "nope", tmp_path / "out")


def test_graph_arm_uses_the_checkpoint_from_before_the_run(tmp_path, capsys):
    ckpt = tmp_path / "graph.json"
    store = play(tmp_path, "c1", "C", "--graph", str(ckpt))
    play(tmp_path, "c2", "C", "--graph", str(ckpt))
    # graph/<level>.json now includes c1 and c2, its .bak only c1: c2 is rebuilt from the .bak.
    report = inspect_run(store, "c2", tmp_path / "out2", graph=str(ckpt))
    assert report["graph"] == str(tmp_path / "graph") and report["digests_verified"] > 0
    # c1 started from an empty graph: its level's lineage starts with c1, so nothing predates it.
    assert inspect_run(store, "c1", tmp_path / "out1", graph=str(ckpt))["digests_verified"] > 0
    assert inspect_run(store, "c1", tmp_path / "out1e", graph="empty")["graph"] == "empty"
    # A third run: the level file and its .bak both include c2, and the level predates c2.
    play(tmp_path, "c3", "C", "--graph", str(ckpt))
    with pytest.raises(InspectError, match="from before run 'c2'"):
        inspect_run(store, "c2", tmp_path / "out2b", graph=str(ckpt))


def test_ask_is_refused_without_a_selection_or_credentials(tmp_path, capsys, monkeypatch):
    from dave_agent.models import jev

    store = play(tmp_path, "r1")
    with pytest.raises(ConfigError, match="--ask needs --decisions"):
        inspect_run(store, "r1", tmp_path / "out", ask=("jev",))
    monkeypatch.setattr(jev, "load_dotenv", lambda: None)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    with pytest.raises(ConfigError, match="OPENROUTER_API_KEY"):
        inspect_run(store, "r1", tmp_path / "out", select={1}, ask=("jev",))
    assert not (tmp_path / "out").exists()  # refused before the replay


def test_cli_inspect(tmp_path, capsys):
    store = play(tmp_path, "r1")
    capsys.readouterr()
    out = tmp_path / "bundle"
    assert main(["inspect", "--store", str(store), "--run-id", "r1", "--out", str(out), "--decisions", "0-3"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["decisions_written"] >= 1 and (out / "run.json").exists()
    assert main(["inspect", "--store", str(store), "--run-id", "missing", "--out", str(out)]) == 2
    assert "not found" in capsys.readouterr().err


@pytest.mark.dave
@pytest.mark.skipif(not (ROOT / "external" / "deadly-dave" / BRIDGE_EXE).exists(), reason="deadly-dave bridge not built")
def test_dave_level2_rebuild_with_screenshots(tmp_path, capsys):
    store = play(tmp_path, "d1", "A", "--adapter", "dave", "--scenario", "level2")
    report = inspect_run(store, "d1", tmp_path / "out", select={0, 1, 2, 3})
    run, decisions = read(tmp_path / "out")
    assert report["digests_verified"] == sum(not d["forced"] for d in run["decisions"]) > 0
    for seq, d in decisions.items():
        assert d["screenshot"] == f"{seq}.bmp" and (tmp_path / "out" / "decisions" / d["screenshot"]).stat().st_size > 0
        assert len(d["outcomes"]) == len(d["request"]["candidates"])
