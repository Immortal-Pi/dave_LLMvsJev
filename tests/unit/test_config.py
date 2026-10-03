import shutil

import pytest
import yaml

from dave_agent.config import ConfigError, load_config

from ..conftest import CONFIG


def test_repo_config_loads(config):
    assert config.environment.adapter == "fixture"
    assert config.models.jev.model_id == "typesafe/jev-1.13"
    assert set(config.arms) >= {"A", "B"}
    assert config.environment.levels_dir.is_absolute()


@pytest.fixture
def config_copy(tmp_path):
    target = tmp_path / "configs"
    shutil.copytree(CONFIG.parent, target)
    return target


def _edit(path, mutate):
    data = yaml.safe_load(path.read_text())
    mutate(data)
    path.write_text(yaml.safe_dump(data))


def test_missing_file_is_named(config_copy):
    (config_copy / "models.yaml").unlink()
    with pytest.raises(ConfigError, match="models.yaml"):
        load_config(config_copy / "experiments.yaml")


def test_invalid_value_reports_location(config_copy):
    _edit(config_copy / "environment.yaml", lambda d: d["environment"].update(decision_frames=0))
    with pytest.raises(ConfigError, match="environment.decision_frames"):
        load_config(config_copy / "experiments.yaml")


def test_unknown_key_rejected(config_copy):
    _edit(config_copy / "experiments.yaml", lambda d: d["planning"].update(confidence_threshold=0.7))
    with pytest.raises(ConfigError, match="planning.confidence_threshold"):
        load_config(config_copy / "experiments.yaml")


def test_duplicate_section_rejected(config_copy):
    _edit(config_copy / "experiments.yaml", lambda d: d.update(models={}))
    with pytest.raises(ConfigError, match="belong in another config file"):
        load_config(config_copy / "experiments.yaml")


def test_unknown_predicate_rejected(config_copy):
    def mutate(d):
        d["skills"]["catalogs"]["dave"][0]["preconditions"] = ["can_fly"]

    _edit(config_copy / "skills.yaml", mutate)
    with pytest.raises(ConfigError, match="unknown predicates"):
        load_config(config_copy / "experiments.yaml")


def test_catalogs_exist_for_both_adapters(config):
    assert {s.name for s in config.skills.for_adapter("fixture")} >= {"move_left", "wait"}
    dave = {s.name: s for s in config.skills.for_adapter("dave")}
    assert "jetpack" not in str(dave) and "climb_up" not in dave  # unverified: excluded
    assert all(s.max_frames <= 600 for s in dave.values())
