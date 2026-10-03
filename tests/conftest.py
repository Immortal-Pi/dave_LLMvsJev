from pathlib import Path

import pytest

from dave_agent.adapters.fixture import FixtureAdapter
from dave_agent.config import load_config

ROOT = Path(__file__).resolve().parents[1]
LEVELS = ROOT / "tests" / "fixtures" / "levels"
CONFIG = ROOT / "configs" / "experiments.yaml"


@pytest.fixture
def adapter():
    a = FixtureAdapter(LEVELS)
    yield a
    a.close()


@pytest.fixture
def config():
    return load_config(CONFIG)
