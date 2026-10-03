"""Offline end-to-end smoke tests through the CLI entry point (no network)."""

import json

import pytest

from dave_agent.cli import main

from ..conftest import CONFIG, ROOT


def _run(capsys, *argv):
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
    assert a == b
    assert a["mode"] == "mock" and a["adapter"] == "fixture"
    assert a["outcome"] in {"level_complete", "game_over", "truncated"}


def test_arms_share_mock_trajectory(capsys):
    _, out_a, _ = _run(capsys, "play", "--arm", "A", "--mock", "--config", str(CONFIG))
    _, out_b, _ = _run(capsys, "play", "--arm", "B", "--mock", "--config", str(CONFIG))
    a, b = json.loads(out_a), json.loads(out_b)
    for key in ("outcome", "frames", "decisions", "score", "deaths"):
        assert a[key] == b[key]


def test_live_mode_refused_without_spending(capsys):
    code, _, err = _run(capsys, "play", "--arm", "A", "--config", str(CONFIG))
    assert code == 2 and "--mock" in err


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
