from dave_agent.adapters.base import AdapterError, GameAdapter
from dave_agent.config import EnvironmentConfig


def create_adapter(name: str, env: EnvironmentConfig, watch: bool = False, watch_delay_ms: int = 14) -> GameAdapter:
    if name == "fixture":
        from dave_agent.adapters.fixture import FixtureAdapter

        return FixtureAdapter(env.levels_dir)
    if name == "dave":
        from dave_agent.adapters.dave import DaveBridgeAdapter

        return DaveBridgeAdapter(env.dave_dir, watch=watch, watch_delay_ms=watch_delay_ms)
    raise AdapterError(f"unknown adapter {name!r}; expected 'fixture' or 'dave'")
