"""Re-ask the Azure planner the recorded planner requests of an inspection bundle with the current
prompt and planner settings, and print the recorded and the new choice side by side.

A cheap check of a planner prompt change on real situations, without playing the game: one paid
Azure call per planner request (at most ``--limit``). The requests are the ones recorded when the
bundle was made (``dave-agent inspect``, docs/inspector.md), so request fields added since (such as
``requires`` or ``player.fuel``) are missing from old bundles; only the prompt and settings are new.
Waypoints are printed as given, not checked: that needs the game state. Needs the AZURE_OPENAI_*
variables in the environment or .env.

Usage: uv run python scripts/replan.py artifacts/inspect/<run> [--limit 10] [--config configs/watch.yaml]
"""

import argparse
import json
import sys
from pathlib import Path

from dave_agent.config import ConfigError, load_config
from dave_agent.models.planner import PlanningRequest


def recorded_requests(bundle: Path) -> list[tuple[dict, dict | None]]:
    """(planner request, the goal set from it or None) per distinct request, in frame order."""
    found: dict[int, tuple[dict, dict | None]] = {}
    for path in sorted((bundle / "decisions").glob("*.json"), key=lambda p: int(p.stem)):
        planner = json.loads(path.read_text(encoding="utf-8")).get("planner") or {}
        request, goal = planner.get("request"), planner.get("goal")
        if request is None or request["frame"] in found:
            continue
        found[request["frame"]] = (request, goal if goal and goal.get("frame") == request["frame"] else None)
    return [found[f] for f in sorted(found)]


def main() -> int:
    from dave_agent.runner.session import azure_planner

    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("bundle", type=Path, help="an inspection bundle directory (with decisions/*.json)")
    parser.add_argument("--limit", type=int, default=10, help="at most this many planner calls (paid)")
    parser.add_argument("--config", type=Path, default=Path("configs/watch.yaml"))
    args = parser.parse_args()

    pairs = recorded_requests(args.bundle)
    if not pairs:
        print(f"no planner requests in {args.bundle}/decisions (inspect the run first)", file=sys.stderr)
        return 2
    pairs = pairs[:args.limit]
    try:
        planner = azure_planner(load_config(args.config))
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(f"{len(pairs)} paid planner call(s) to {planner.model}", file=sys.stderr)
    changed = 0
    try:
        for raw, goal in pairs:
            request = PlanningRequest.model_validate({**raw, "observation_id": 0})
            text, call = planner.propose(request)
            try:
                new = json.loads(text) if text else {}
            except json.JSONDecodeError:
                new = {"goal": f"(not JSON: {text[:60]!r})"}
            old = (goal or {}).get("target_ref", "?")
            changed += new.get("goal") != old
            print(f"frame {request.frame} {','.join(request.triggers)}")
            print(f"  recorded: {old} {(goal or {}).get('waypoints') or ''}")
            print(f"  now:      {new.get('goal', f'({call.status})')} {new.get('waypoints') or ''}")
            print(f"            {new.get('rationale', '')}")
    finally:
        planner.client.close()
    print(f"{changed}/{len(pairs)} choices changed", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
