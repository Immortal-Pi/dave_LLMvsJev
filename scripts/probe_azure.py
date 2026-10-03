"""Live probe of the Azure OpenAI planner contract; saves a sanitized response fixture.

Sends one small planner request (the fixture level's start state) and writes the raw response
body, without any headers, to tests/fixtures/azure/planner_response.json. One small paid call;
needs the AZURE_OPENAI_* variables in the environment or .env.

Usage: uv run python scripts/probe_azure.py [--no-save]
"""

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "azure" / "planner_response.json"


def main() -> int:
    from dave_agent.adapters.fixture import FixtureAdapter
    from dave_agent.config import ConfigError, load_config
    from dave_agent.control.goals import TargetMemory, goal_candidates
    from dave_agent.models.azure import AzureChatClient, AzurePlanner, AzureSettings
    from dave_agent.models.planner import PlanningRequest, parse_plan

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--no-save", action="store_true")
    args = parser.parse_args()
    config = load_config(ROOT / "configs" / "experiments.yaml")
    try:
        settings = AzureSettings.from_env(config.models.planner.deployment_env)
    except ConfigError as exc:
        print(exc, file=sys.stderr)
        return 2
    adapter = FixtureAdapter(config.environment.levels_dir)
    try:
        obs = adapter.reset("fixture_l1", 0)
    finally:
        adapter.close()
    targets = TargetMemory()
    targets.reset(obs)
    request = PlanningRequest(
        episode_id=obs.episode_id, level_id=obs.level_id, frame=obs.frame, observation_id=obs.observation_id,
        triggers=("no_goal",), player={"state": obs.player_state, "grounded": obs.grounded, "facing": obs.facing},
        lives=obs.lives, inventory=obs.inventory, score=obs.score,
        view_cols=(obs.region.min.col, obs.region.max.col), nearby=(), recent=(), previous_goal=None,
        current_goal=None, candidates=goal_candidates(obs, targets, config.planning), graph_routes=False)
    client = AzureChatClient(settings, config.models.timeout_seconds, 0)
    try:
        response = client._client.post(client.url, params={"api-version": settings.api_version},
                                       headers={"api-key": settings.api_key}, json=AzurePlanner(client).body(request))
    finally:
        client.close()
    print("HTTP", response.status_code)
    if response.status_code != 200:
        print(response.text[:500], file=sys.stderr)
        return 1
    data = response.json()
    choice = parse_plan(data["choices"][0]["message"]["content"], request.candidate_ids)
    print(json.dumps({"choice": choice.model_dump(), "usage": data.get("usage")}, indent=2))
    if not args.no_save:
        FIXTURE.parent.mkdir(parents=True, exist_ok=True)
        data["_meta"] = {"captured_at": datetime.now(UTC).isoformat(timespec="seconds"),
                         "api_version": settings.api_version, "deployment": settings.deployment,
                         "candidate_ids": list(request.candidate_ids),
                         "note": "response body only; no request headers or credentials"}
        FIXTURE.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        print("saved", FIXTURE.relative_to(ROOT))
    return 0


if __name__ == "__main__":
    sys.exit(main())
