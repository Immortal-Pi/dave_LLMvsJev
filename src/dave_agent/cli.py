"""dave-agent command line.

Modes: --mock runs offline mock tactical controllers (no network, no cost). The strategic
planner is the offline rule planner unless --planner live is given, which calls Azure OpenAI
(paid; the run is labeled live-planner). Live tactical controllers arrive in Phases 7-8.
'probe-provider --provider azure' makes one small paid planner call to check the contract.
The 'fixture' adapter is a synthetic test platformer,
never Dangerous Dave; the 'dave' adapter drives deadly-dave through the stepping bridge.
Every 'play' episode is logged to the SQLite episode store; 'export' writes it as JSONL
and 'replay' re-runs an exported episode and checks it reproduces the recorded evidence.
Graph-enabled arms learn into a per-arm graph checkpoint; 'graph' inspects one.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import uuid
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

from dave_agent.adapters import create_adapter
from dave_agent.adapters.base import AdapterError
from dave_agent.config import AppConfig, ConfigError, load_config
from dave_agent.control.goals import GoalManager, TargetMemory, goal_candidates
from dave_agent.logging_setup import configure_logging
from dave_agent.memory.episodes import EpisodeStore, StoreError
from dave_agent.memory.graph import WorldGraph
from dave_agent.memory.persistence import GraphCheckpointError, export_yaml, load_checkpoint, save_checkpoint
from dave_agent.memory.routes import find_route
from dave_agent.memory.working import WorkingMemory
from dave_agent.models.azure import AzureChatClient, AzurePlanner, AzureSettings
from dave_agent.models.mock import SeededMockController
from dave_agent.models.planner import PlanningRequest, RuleMockPlanner, parse_plan
from dave_agent.runner.episode import run_episode
from dave_agent.runner.replay import load_jsonl, replay_episode

DEFAULT_CONFIG = Path("configs/experiments.yaml")


def _cmd_probe(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    adapter = create_adapter(args.adapter, config.environment, args.watch, args.watch_delay)
    try:
        caps = adapter.capabilities()
        first = adapter.reset(args.scenario or config.scenario, args.seed)
        step = adapter.step(frozenset({"right"}), 1)
        second = step.observation
    finally:
        adapter.close()
    changed = first.player_position != second.player_position or first.frame != second.frame
    report = {
        "adapter": caps.adapter,
        "build_id": caps.build_id,
        "capabilities": caps.model_dump(mode="json"),
        "before": {"frame": first.frame, "player_position": first.player_position.model_dump()},
        "action": {"buttons": sorted(step.applied_buttons), "frames_advanced": step.frames_advanced},
        "after": {"frame": second.frame, "player_position": second.player_position.model_dump()},
        "observation_changed": changed,
    }
    print(json.dumps(report, indent=2))
    return 0 if changed else 1


def _cmd_play(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    if args.arm not in config.arms:
        raise ConfigError(f"unknown arm {args.arm!r}; configured arms: {sorted(config.arms)}")
    if not args.mock:
        raise ConfigError(
            "live controllers are not implemented yet (LLM: Phase 7, Jev: Phase 8); rerun with --mock"
        )
    arm = config.arms[args.arm]
    live_planner = args.planner == "live"
    if live_planner and arm.planner != "llm":
        raise ConfigError(f"arm {args.arm} uses planner {arm.planner!r}; --planner live needs an LLM planner arm")
    # Validate credentials before starting the game or spending anything.
    planner = _azure_planner(config) if live_planner else RuleMockPlanner()
    adapter_name = args.adapter or config.environment.adapter
    adapter = create_adapter(adapter_name, config.environment, args.watch, args.watch_delay)
    # Both arms use the same seeded mock so offline runs are directly comparable.
    controller = SeededMockController(seed=args.seed, label=f"mock-{arm.tactical}")
    scenario = args.scenario or config.scenario
    run_id = args.run_id or f"run-{datetime.now(UTC):%Y%m%dT%H%M%S}-{uuid.uuid4().hex[:6]}"
    store = EpisodeStore(args.store or config.memory.episode_store)
    graph, graph_path = None, None
    if arm.graph_enabled:
        # Separate checkpoint per arm (and per adapter): no route knowledge leaks between arms.
        graph_path = (args.graph or config.memory.graph_checkpoint
                      or config.memory.episode_store.parent / "graphs" / f"arm-{args.arm}" / f"{adapter_name}.json")
        caps = adapter.capabilities()
        policy = config.environment.observation_policy
        graph = (load_checkpoint(graph_path, caps.adapter, caps.build_id, policy) if graph_path.exists()
                 else WorldGraph(caps.adapter, caps.build_id, policy, config.graph.evidence_per_item))
    learn = graph is not None and config.memory.graph_updates
    mode = "live-planner" if live_planner else "mock"
    # Same planner code and trigger settings for every arm; only graph arms get learned routes.
    goals = GoalManager(planner, config.planning, config.models.max_retries,
                        graph if arm.graph_enabled else None, config.graph if arm.graph_enabled else None)
    try:
        store.create_run(run_id, mode=mode, command="play", config_json=config.model_dump_json())
        recorder = store.recorder(run_id, args.arm, controller.model, scenario, args.seed,
                                  config.memory.store_batch_size)
        result = run_episode(
            adapter,
            controller,
            config.skills.for_adapter(adapter_name),
            config.skills.executor,
            WorkingMemory.from_config(config),
            scenario_id=scenario,
            seed=args.seed,
            max_frames=config.benchmark.max_episode_frames,
            recorder=recorder,
            graph=graph if learn else None,
            goals=goals,
        )
    finally:
        adapter.close()
        store.close()
        if live_planner:
            planner.client.close()  # type: ignore[attr-defined]
    if learn:
        graph.add_lineage(run_id, recorder.episode_key, args.arm, scenario)
        save_checkpoint(graph, graph_path)
    planner_calls = [c for c in result.model_calls if c.purpose == "planner"]
    ended = Counter(e.payload.get("status") for e in result.events if e.event_type in ("goal_achieved", "goal_failed"))
    summary = {
        "mode": mode,
        "run_id": run_id,
        "episode_key": recorder.episode_key,
        "store": str(store.path),
        "adapter": result.adapter,
        "arm": args.arm,
        "tactical": controller.model,
        "episode_id": result.episode_id,
        "outcome": result.outcome,
        "termination_reason": result.termination_reason,
        "frames": result.frames,
        "decisions": len(result.decisions),
        "forced_decisions": sum(d.forced for d in result.decisions),
        "model_calls": len(result.model_calls) - len(planner_calls),
        "planner": f"{planner.provider}:{planner.model}",
        "planner_calls": len(planner_calls),
        "planner_failures": sum(c.status != "ok" for c in planner_calls),
        "goals": {"set": len(result.planning), "achieved": ended["achieved"], "failed": ended["failed"],
                  "expired": ended["expired"]},
        "planning_triggers": dict(sorted(Counter(t for r in result.planning for t in r.triggers).items())),
        "fallback_goals": sum(r.fallback for r in result.planning),
        "goal_trace": [r.chosen for r in result.planning],
        "route_ms": round(sum(r.route_ms for r in result.planning), 3),
        "skill_outcomes": dict(sorted(Counter(r.outcome for r in result.executions).items())),
        "interruptions": dict(
            sorted(Counter(r.reason.split(":")[0] for r in result.executions if r.outcome == "interrupted").items())
        ),
        # Hash of every candidate list offered, in order: equal across arms by construction.
        "candidate_trace": hashlib.sha256(
            "".join(c.digest for c in result.candidate_sets).encode()
        ).hexdigest()[:16],
        "score": result.score,
        "lives_left": result.lives,
        "deaths": sum(e.event_type == "death" for e in result.events),
        "last_observation_id": result.observation_ids[-1],
        "graph": None if graph is None else {"checkpoint": str(graph_path), "updated": learn, **graph.counts()},
    }
    print(json.dumps(summary, indent=2))
    return 0


def _azure_planner(config: AppConfig) -> AzurePlanner:
    settings = AzureSettings.from_env(config.models.planner.deployment_env)
    client = AzureChatClient(settings, config.models.timeout_seconds, config.models.max_retries)
    return AzurePlanner(client)


def _cmd_probe_provider(args: argparse.Namespace) -> int:
    """One small paid planner call on the fixture start observation: checks the live contract."""
    config = load_config(args.config)
    planner = _azure_planner(config)
    adapter = create_adapter("fixture", config.environment)
    try:
        obs = adapter.reset("fixture_l1", 0)
    finally:
        adapter.close()
    targets = TargetMemory()
    targets.reset(obs)
    candidates = goal_candidates(obs, targets, config.planning)
    request = PlanningRequest(
        episode_id=obs.episode_id, level_id=obs.level_id, frame=obs.frame, observation_id=obs.observation_id,
        triggers=("no_goal",), player={"state": obs.player_state, "grounded": obs.grounded, "facing": obs.facing},
        lives=obs.lives, inventory=obs.inventory, score=obs.score,
        view_cols=(obs.region.min.col, obs.region.max.col), nearby=(), recent=(), previous_goal=None,
        current_goal=None, candidates=candidates, graph_routes=False)
    try:
        text, call = planner.propose(request)
    finally:
        planner.client.close()
    report: dict = {"provider": call.provider, "model": call.model, "status": call.status,
                    "latency_ms": call.latency_ms, "retries": call.retries, "usage": call.usage,
                    "response_ref": call.response_ref, "candidates": list(request.candidate_ids)}
    try:
        report["choice"] = parse_plan(text, request.candidate_ids).model_dump()
        report["valid"] = True
    except ValueError as exc:
        report.update(valid=False, error=str(exc), raw=None if text is None else text[:300])
    print(json.dumps(report, indent=2))
    return 0 if report["valid"] else 1


def _cmd_graph(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    graph = load_checkpoint(args.checkpoint)
    report: dict = {"checkpoint": str(args.checkpoint), "adapter": graph.adapter, "build_id": graph.build_id,
                    "observation_policy": graph.observation_policy, "episodes": len(graph.lineage),
                    **graph.counts()}
    if args.yaml:
        report["yaml"] = str(export_yaml(graph, args.yaml))
    if args.route:
        items = {name: 1 for name in args.items.split(",") if name} if args.items else {}
        route = find_route(graph, args.route[0], args.route[1], items, config.graph)
        report["route"] = {"status": route.status, "reason": route.reason, "cost": route.cost,
                           "steps": [[s.source, s.skill, s.target] for s in route.steps],
                           "frontier": list(route.frontier)}
    print(json.dumps(report, indent=2))
    return 0


def _cmd_export(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    with EpisodeStore(args.store or config.memory.episode_store) as store:
        count = store.export_jsonl(args.out, args.run_id)
    print(json.dumps({"out": str(args.out), "records": count, "run_id": args.run_id}, indent=2))
    return 0


def _cmd_replay(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    episodes = load_jsonl(args.jsonl)
    keys = [args.episode] if args.episode else sorted(episodes)
    reports = []
    for key in keys:
        if key not in episodes:
            raise StoreError(f"episode {key!r} not in {args.jsonl}; have {sorted(episodes)}")
        records = episodes[key]
        adapter_name = records["episode"][0]["adapter"]
        adapter = create_adapter(adapter_name, config.environment)
        try:
            report = replay_episode(adapter, config.skills.for_adapter(adapter_name), config.skills.executor,
                                    records)
        finally:
            adapter.close()
        reports.append({"episode_key": key, "decisions": report.decisions, "ok": report.ok,
                        "mismatches": report.mismatches})
    print(json.dumps(reports, indent=2))
    return 0 if all(r["ok"] for r in reports) else 1


def _add_watch_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--watch", action="store_true", help="dave adapter: show the game in a window (display only)")
    parser.add_argument("--watch-delay", type=int, default=14, metavar="MS",
                        help="pause after each shown tick; 14 = real speed, higher = slower")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="dave-agent", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--log-level", default="WARNING")
    sub = parser.add_subparsers(dest="command", required=True)

    probe = sub.add_parser("probe", help="reset -> observe -> step -> observe on an adapter")
    probe.add_argument("--adapter", choices=["fixture", "dave"], default="fixture")
    probe.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    probe.add_argument("--scenario")
    probe.add_argument("--seed", type=int, default=0)
    _add_watch_args(probe)
    probe.set_defaults(func=_cmd_probe)

    play = sub.add_parser("play", help="run one episode for an experiment arm")
    play.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    play.add_argument("--arm", required=True)
    play.add_argument("--mock", action="store_true", help="offline mock tactical controllers (required for now)")
    play.add_argument("--planner", choices=["mock", "live"], default="mock",
                      help="strategic planner: offline rule planner (default) or Azure OpenAI (paid)")
    play.add_argument("--adapter", choices=["fixture", "dave"])
    play.add_argument("--scenario")
    play.add_argument("--seed", type=int, default=0)
    play.add_argument("--store", type=Path, help="episode store (default: memory.episode_store)")
    play.add_argument("--run-id", help="default: run-<UTC time>-<random>")
    play.add_argument("--graph", type=Path,
                      help="graph checkpoint for graph-enabled arms (default: artifacts/graphs/arm-<ARM>/<adapter>.json)")
    _add_watch_args(play)
    play.set_defaults(func=_cmd_play)

    probe_provider = sub.add_parser("probe-provider", help="one small paid call to check a live provider contract")
    probe_provider.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    probe_provider.add_argument("--provider", choices=["azure"], required=True)
    probe_provider.set_defaults(func=_cmd_probe_provider)

    export = sub.add_parser("export", help="write the episode store (or one run) as JSONL")
    export.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    export.add_argument("--store", type=Path, help="episode store (default: memory.episode_store)")
    export.add_argument("--run-id")
    export.add_argument("--out", type=Path, required=True)
    export.set_defaults(func=_cmd_export)

    graph = sub.add_parser("graph", help="inspect a learned graph checkpoint (counts, YAML export, route)")
    graph.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    graph.add_argument("--checkpoint", type=Path, required=True)
    graph.add_argument("--yaml", type=Path, help="write a YAML inspection export here")
    graph.add_argument("--route", nargs=2, metavar=("FROM", "TO"), help="node ids")
    graph.add_argument("--items", help="held items for --route, comma-separated (e.g. trophy,gun)")
    graph.set_defaults(func=_cmd_graph)

    replay = sub.add_parser("replay", help="re-run exported episodes and verify the recorded evidence")
    replay.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    replay.add_argument("--jsonl", type=Path, required=True)
    replay.add_argument("--episode", help="episode_key (default: every episode in the file)")
    replay.set_defaults(func=_cmd_replay)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    configure_logging(args.log_level)
    try:
        return args.func(args)
    except (ConfigError, AdapterError, StoreError, GraphCheckpointError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
