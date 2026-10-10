"""dave-agent command line.

Modes: by default 'play' is fully offline: a seeded mock tactical model and
the rule planner (no network, no cost). --planner live calls the Azure OpenAI planner and
--tactical live the arm's live tactical model (Azure OpenAI for tactical: llm, Jev via
OpenRouter for tactical: jev). Live runs are paid, check credentials and print their budget
before any call, and are labeled live-planner, live-tactical or live. 'probe-provider' makes
one small paid call (Azure planner or tactical, Jev tactical) to check the contract.
The 'fixture' adapter is a synthetic test platformer,
never Dangerous Dave; the 'dave' adapter drives deadly-dave through the stepping bridge.
Every 'play' episode is logged to the SQLite episode store; 'export' writes it as JSONL
and 'replay' re-runs an exported episode and checks it reproduces the recorded evidence.
Graph-enabled arms learn into a per-arm graph checkpoint; 'graph' inspects one.
'benchmark' runs paired trials of several arms and writes a manifest, per-episode records and
summaries; 'summarize' rebuilds the summaries; 'train-memory' builds a warm graph checkpoint.
'inspect' replays a recorded episode with its recorded choices, proves every rebuilt tactical
request matches the recorded digest, and writes what the models saw per decision (for the
viewer in frontend/); --ask sends a rebuilt request to a live model (paid).
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path

from dave_agent.adapters import create_adapter
from dave_agent.adapters.base import AdapterError
from dave_agent.config import AppConfig, ConfigError, load_config
from dave_agent.control.goals import TargetMemory, goal_candidates
from dave_agent.control.skills import generate_candidates
from dave_agent.logging_setup import configure_logging
from dave_agent.memory.episodes import EpisodeStore, StoreError
from dave_agent.memory.graph import GraphStore
from dave_agent.memory.persistence import GraphCheckpointError, export_store_yaml, load_store, save_store, store_dir
from dave_agent.memory.routes import find_route
from dave_agent.memory.working import WorkingMemory
from dave_agent.models.planner import PlanningRequest, parse_plan
from dave_agent.models.tactical import tactical_request
from dave_agent.runner.session import (
    azure_planner,
    azure_tactical,
    budget_notice,
    build_models,
    episode_summary,
    jev_tactical,
    open_graph,
    run_trial,
)
from dave_agent.runner.benchmark import BenchmarkRunner, BenchmarkSpec, resummarize, train_memory
from dave_agent.runner.inspect import InspectError, inspect_run, parse_selection
from dave_agent.runner.replay import load_jsonl, replay_episode

DEFAULT_CONFIG = Path("configs/experiments.yaml")
JEV_TACTICAL_FIXTURE = Path("tests/fixtures/jev/tactical_response.json")


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
    live_planner, live_tactical = args.planner == "live", args.tactical == "live"
    if args.mock and live_tactical:
        raise ConfigError("--mock selects the mock tactical controller; drop it to use --tactical live")
    # Validate credentials before starting the game or spending anything.
    models = build_models(config, args.arm, live_planner, live_tactical, args.seed)
    if models.mode != "mock":
        print(json.dumps(budget_notice(config, models.mode)), file=sys.stderr)
    adapter_name = args.adapter or config.environment.adapter
    adapter = create_adapter(adapter_name, config.environment, args.watch, args.watch_delay)
    scenario = args.scenario or config.scenario
    run_id = args.run_id or f"run-{datetime.now(UTC):%Y%m%dT%H%M%S}-{uuid.uuid4().hex[:6]}"
    store = EpisodeStore(args.store or config.memory.episode_store)
    graph, graph_path, learn = None, None, False
    try:
        graph, graph_path, learn = open_graph(config, args.arm, adapter, adapter_name, args.graph)
        result, recorder = run_trial(config, args.arm, models, adapter, adapter_name, scenario, args.seed, store,
                                     run_id, "play", graph, learn)
    except KeyboardInterrupt as exc:
        # The recorder already wrote the episode; keep what the graph learned too.
        recorder = getattr(exc, "episode_recorder", None)
        if learn and recorder is not None:
            graph.add_lineage(run_id, recorder.episode_key, args.arm, scenario)
            save_store(graph, graph_path)
        print(json.dumps({"interrupted": True, "run_id": run_id, "store": str(store.path),
                          "episode_key": recorder and recorder.episode_key,
                          "graph_checkpoint": str(graph_path) if learn and recorder else None}), file=sys.stderr)
        return 130
    finally:
        adapter.close()
        store.close()
        models.close()
    if learn:
        graph.add_lineage(run_id, recorder.episode_key, args.arm, scenario)
        save_store(graph, graph_path)
    summary = episode_summary(result, mode=models.mode, run_id=run_id, episode_key=recorder.episode_key,
                              store=store.path, arm=args.arm, models=models, graph=graph, graph_path=graph_path,
                              learn=learn)
    print(json.dumps(summary, indent=2))
    return 0


def _cmd_probe_provider(args: argparse.Namespace) -> int:
    """One small paid call on the fixture start observation: checks the live contract."""
    config = load_config(args.config)
    purpose = args.purpose or ("tactical" if args.provider == "jev" else "planner")
    if args.provider == "jev" and purpose != "tactical":
        raise ConfigError("Jev is a tactical model; use --purpose tactical")
    if args.save_fixture and args.provider != "jev":
        raise ConfigError("--save-fixture is only for --provider jev")
    if purpose == "tactical":
        return _probe_tactical(config, args.provider, args.save_fixture)
    planner = azure_planner(config)
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


def _probe_tactical(config: AppConfig, provider: str, save_fixture: bool) -> int:
    model = jev_tactical(config) if provider == "jev" else azure_tactical(config)
    adapter = create_adapter("fixture", config.environment)
    try:
        obs = adapter.reset("fixture_l1", 0)
        candidates = list(generate_candidates(config.skills.for_adapter("fixture"),
                                              adapter.capabilities().buttons, obs).candidates)
    finally:
        adapter.close()
    memory = WorkingMemory.from_config(config)
    memory.reset(obs)
    request = tactical_request(obs, None, candidates, memory.context())
    try:
        text, call = model.propose(request)
    finally:
        model.client.close()
    report: dict = {"provider": call.provider, "model": call.model, "purpose": call.purpose, "status": call.status,
                    "latency_ms": call.latency_ms, "retries": call.retries, "usage": call.usage,
                    "cost_usd": call.cost_usd, "response_ref": call.response_ref, "output": call.output,
                    "candidates": list(request.candidate_ids)}
    try:
        choice = model.parse(text, request.candidate_ids)
        report.update(choice=choice.candidate_id, provider_score=choice.provider_score, valid=True)
    except ValueError as exc:
        report.update(valid=False, error=str(exc), raw=None if text is None else text[:300])
    if save_fixture and report["valid"]:
        # The request body carries no credentials (the key is only in the Authorization header).
        JEV_TACTICAL_FIXTURE.parent.mkdir(parents=True, exist_ok=True)
        JEV_TACTICAL_FIXTURE.write_text(json.dumps({
            "_meta": {"endpoint": config.models.jev.endpoint, "captured_at": datetime.now(UTC).isoformat(
                timespec="seconds"), "latency_ms": call.latency_ms,
                "note": "Sanitized live response from 'dave-agent probe-provider --provider jev --purpose "
                        "tactical --save-fixture'; no credentials included."},
            "request": model.body(request), "response": json.loads(text)}, indent=2) + "\n", encoding="utf-8")
        report["fixture"] = str(JEV_TACTICAL_FIXTURE)
    print(json.dumps(report, indent=2))
    return 0 if report["valid"] else 1


def _arm_paths(values: list[str] | None) -> dict[str, Path]:
    out = {}
    for value in values or []:
        arm, sep, path = value.partition("=")
        if not sep or not arm or not path:
            raise ConfigError(f"--checkpoint expects ARM=PATH, got {value!r}")
        out[arm] = Path(path)
    return out


def _split(value: str | None) -> tuple[str, ...]:
    return tuple(v.strip() for v in (value or "").split(",") if v.strip())


def _notify(message: dict) -> None:
    print(json.dumps(message), file=sys.stderr, flush=True)


def _cmd_benchmark(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    if args.seed is not None:
        config = config.model_copy(update={"benchmark": config.benchmark.model_copy(update={"seed": args.seed})})
    if args.mock and args.tactical == "live":
        raise ConfigError("--mock selects the mock tactical controller; drop it to use --tactical live")
    adapter = args.adapter or config.environment.adapter
    scenarios = _split(args.scenarios)
    if adapter == "dave" and not scenarios:
        raise ConfigError("--adapter dave needs --scenarios (e.g. level1)")
    bench_id = args.id or f"bench-{datetime.now(UTC):%Y%m%dT%H%M%S}-{uuid.uuid4().hex[:6]}"
    spec = BenchmarkSpec(
        arms=_split(args.arms), trials=args.trials or config.benchmark.pilot_trials,
        scenarios=scenarios or (config.scenario,), adapter=adapter,
        out_dir=args.out or config.memory.episode_store.parent / "benchmarks" / bench_id, benchmark_id=bench_id,
        live_planner=args.planner == "live", live_tactical=args.tactical == "live", regime=args.memory_regime,
        checkpoints=_arm_paths(args.checkpoint), shared_checkpoint=args.shared_checkpoint,
        reach_hints=args.reach_hints == "on", allow_unpriced=args.allow_unpriced, config_path=args.config)
    manifest = BenchmarkRunner(config, spec, notify=_notify).run()
    summary = json.loads((spec.out_dir / "summary.json").read_text(encoding="utf-8"))
    print(json.dumps({
        "benchmark_id": bench_id, "out": str(spec.out_dir), "status": manifest["status"], "mode": manifest["mode"],
        "environment": manifest["environment_label"], "memory_regime": manifest["memory_regime"],
        "episodes_run": manifest["episodes_run"], "not_run": len(manifest["not_run"]),
        "spent_usd": manifest["spent_usd"],
        "groups": [{k: g[k] for k in ("scenario", "arm", "n", "completions", "completion_rate", "completion_ci",
                                      "deaths_mean", "frames_to_completion_median", "cost_usd_per_attempt")}
                   for g in summary["groups"]],
        "pairs": [{k: p[k] for k in ("scenario", "arms", "n_pairs", "completion_diff", "completion_diff_ci",
                                     "mcnemar_p")} for p in summary["pairs"]]}, indent=2))
    return 0 if manifest["status"] == "complete" else 1


def _cmd_summarize(args: argparse.Namespace) -> int:
    summary = resummarize(args.benchmark)
    print(json.dumps({"out": str(args.benchmark), "groups": len(summary["groups"]), "pairs": len(summary["pairs"])},
                     indent=2))
    return 0


def _cmd_train_memory(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    adapter = args.adapter or config.environment.adapter
    scenarios = _split(args.scenarios) or (config.scenario,)
    store = args.store or config.memory.episode_store.parent / "train-memory.sqlite"
    report = train_memory(config, args.arm, args.episodes, scenarios, adapter, args.out, store,
                          args.planner == "live", args.tactical == "live", notify=_notify)
    print(json.dumps(report, indent=2))
    return 0


def _cmd_live(args: argparse.Namespace) -> int:
    from dave_agent.runner.live import LiveServer, serve

    config = load_config(args.config)
    server = LiveServer(config, args.adapter, args.store, args.allow_paid,
                        args.tick_ms, args.frame_every)

    def ready(address) -> None:
        _notify({"live": f"http://{address[0]}:{address[1]}", "store": str(server.store_path),
                 "allow_paid": args.allow_paid,
                 "viewer": "cd frontend && npm run dev, then open http://localhost:3000/live"})

    try:
        serve(server, args.host, args.port, ready)
    except KeyboardInterrupt:
        return 130
    return 0


def _cmd_inspect(args: argparse.Namespace) -> int:
    out = args.out or Path("artifacts") / "inspect" / args.run_id
    if args.ask:
        print(json.dumps({"paid_run": "inspect --ask", "providers": args.ask, "decisions": args.decisions,
                          "calls": "one per provider per selected decision"}), file=sys.stderr)
    report = inspect_run(args.store, args.run_id, out, graph=args.graph, select=parse_selection(args.decisions),
                         outcomes=not args.no_outcomes, ask=tuple(args.ask or ()), notify=_notify)
    print(json.dumps(report, indent=2))
    return 0


def _cmd_graph(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    store = load_store(args.checkpoint)
    if args.rekey:
        _rekey(store, args.checkpoint, config, args.rekey)
    report: dict = {"checkpoint": str(store_dir(args.checkpoint)), "adapter": store.adapter,
                    "build_id": store.build_id, "observation_policy": store.observation_policy,
                    "execution_mode": store.execution_mode, "episodes": len(store.lineage), **store.counts(),
                    "per_level": {level: g.counts() for level, g in sorted(store.levels.items())}}
    if args.yaml:
        report["yaml"] = str(export_store_yaml(store, args.yaml))
    if args.route:
        items = {name: 1 for name in args.items.split(",") if name} if args.items else {}
        level = args.route[0].split(":")[0]  # node ids start with their level
        if level not in store.levels:
            raise GraphCheckpointError(f"no graph for level {level!r}; levels: {sorted(store.levels)}")
        route = find_route(store.levels[level], args.route[0], args.route[1], items, config.graph)
        report["route"] = {"status": route.status, "reason": route.reason, "cost": route.cost,
                           "steps": [[s.source, s.skill, s.target] for s in route.steps],
                           "frontier": list(route.frontier)}
    print(json.dumps(report, indent=2))
    return 0


def _rekey(store: GraphStore, checkpoint: Path, config: AppConfig, adapter_name: str) -> None:
    """Tie a graph store to the current game build, keeping every node and edge. Only for a
    build whose physics and levels did not change (e.g. an older build id scheme); each level
    file is first copied to ``<level>.json.prekey``."""
    adapter = create_adapter(adapter_name, config.environment)
    try:
        caps = adapter.capabilities()
    finally:
        adapter.close()
    policy = config.environment.observation_policy
    if (store.adapter, store.observation_policy) != (caps.adapter, policy):
        raise GraphCheckpointError(
            f"{checkpoint}: adapter/policy {store.adapter}/{store.observation_policy} differ from "
            f"{caps.adapter}/{policy}; only the build id can be re-keyed")
    directory = store_dir(checkpoint)
    for file in directory.glob("*.json"):
        shutil.copy2(file, file.with_name(file.name + ".prekey"))
    print(f"rekey {directory}: {store.build_id} -> {caps.build_id}", file=sys.stderr)
    store.build_id = caps.build_id
    for graph in store.levels.values():
        graph.build_id = caps.build_id
    save_store(store, directory)


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
    play.add_argument("--mock", action="store_true", help="mock tactical controller (the default); excludes --tactical live")
    play.add_argument("--planner", choices=["mock", "live"], default="mock",
                      help="strategic planner: offline rule planner (default) or Azure OpenAI (paid)")
    play.add_argument("--tactical", choices=["mock", "live"], default="mock",
                      help="tactical controller: seeded mock (default) or the arm's live model (paid): "
                           "Azure OpenAI for tactical: llm, Jev for tactical: jev")
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
    probe_provider.add_argument("--provider", choices=["azure", "jev"], required=True)
    probe_provider.add_argument("--purpose", choices=["planner", "tactical"],
                                help="default: planner for azure, tactical for jev")
    probe_provider.add_argument("--save-fixture", action="store_true",
                                help="jev: write the sanitized request/response to tests/fixtures/jev/")
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
    graph.add_argument("--rekey", metavar="ADAPTER", choices=("dave", "fixture"),
                       help="tie the checkpoint to ADAPTER's current build id, keeping what it learned "
                            "(only when the game's physics and levels did not change)")
    graph.set_defaults(func=_cmd_graph)

    replay = sub.add_parser("replay", help="re-run exported episodes and verify the recorded evidence")
    replay.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    replay.add_argument("--jsonl", type=Path, required=True)
    replay.add_argument("--episode", help="episode_key (default: every episode in the file)")
    replay.set_defaults(func=_cmd_replay)

    bench = sub.add_parser("benchmark", help="paired trials of several arms: manifest, episode records and "
                                             "summaries (docs/benchmark.md)")
    bench.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    bench.add_argument("--arms", required=True, help="comma-separated, e.g. A,B,C")
    bench.add_argument("--trials", type=int, help="trials per scenario (default: benchmark.pilot_trials)")
    bench.add_argument("--scenarios", help="comma-separated (default: the config scenario; required for dave)")
    bench.add_argument("--adapter", choices=["fixture", "dave"])
    bench.add_argument("--mock", action="store_true", help="mock tactical controller (the default)")
    bench.add_argument("--planner", choices=["mock", "live"], default="mock", help="live: Azure OpenAI (paid)")
    bench.add_argument("--tactical", choices=["mock", "live"], default="mock",
                       help="live: each arm's live tactical model (paid)")
    bench.add_argument("--memory-regime", choices=["cold", "warm"], default="cold")
    bench.add_argument("--checkpoint", action="append", metavar="ARM=PATH",
                       help="warm regime: the frozen graph checkpoint for a graph arm (repeatable)")
    bench.add_argument("--shared-checkpoint", action="store_true",
                       help="allow a checkpoint trained by another arm (identical-pretrained-graph experiment only)")
    bench.add_argument("--reach-hints", choices=["on", "off"], default="on",
                       help="off: no reachability waypoints or estimated end tiles (ablation, all arms)")
    bench.add_argument("--seed", type=int, help="base seed (default: benchmark.seed)")
    bench.add_argument("--id", help="benchmark id (default: bench-<UTC time>-<random>)")
    bench.add_argument("--out", type=Path, help="output directory (default: artifacts/benchmarks/<id>); must be new")
    bench.add_argument("--allow-unpriced", action="store_true",
                       help="run live roles with no reported or priced cost; the ceiling cannot see their spend")
    bench.set_defaults(func=_cmd_benchmark)

    summarize = sub.add_parser("summarize", help="rebuild a benchmark's summaries from its episodes.jsonl")
    summarize.add_argument("--benchmark", type=Path, required=True, help="benchmark output directory")
    summarize.set_defaults(func=_cmd_summarize)

    train = sub.add_parser("train-memory", help="training episodes that extend one graph checkpoint "
                                                "(for --memory-regime warm)")
    train.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    train.add_argument("--arm", required=True)
    train.add_argument("--episodes", type=int, required=True)
    train.add_argument("--scenarios", help="comma-separated training scenarios, cycled")
    train.add_argument("--adapter", choices=["fixture", "dave"])
    train.add_argument("--planner", choices=["mock", "live"], default="mock")
    train.add_argument("--tactical", choices=["mock", "live"], default="mock")
    train.add_argument("--out", type=Path, required=True, help="checkpoint to create or extend")
    train.add_argument("--store", type=Path, help="episode store (default: artifacts/train-memory.sqlite)")
    train.set_defaults(func=_cmd_train_memory)

    inspect = sub.add_parser("inspect", help="rebuild exactly what the models saw at each decision of a recorded "
                                             "episode (for the viewer in frontend/)")
    inspect.add_argument("--store", type=Path, required=True, help="episode store holding the run")
    inspect.add_argument("--run-id", required=True)
    inspect.add_argument("--decisions", help="decision numbers to write, e.g. 90-95,100 (default: all)")
    inspect.add_argument("--graph", help="graph checkpoint the run started from, or 'empty' (graph arms; default: "
                                         "the arm's checkpoint, or its .bak when the file already includes the run)")
    inspect.add_argument("--no-outcomes", action="store_true",
                         help="skip executing every offered candidate from each decision's state")
    inspect.add_argument("--ask", action="append", choices=["azure", "jev"],
                         help="PAID: send the rebuilt request to this live model (repeatable; needs --decisions)")
    inspect.add_argument("--out", type=Path, help="bundle directory (default: artifacts/inspect/<run-id>)")
    inspect.set_defaults(func=_cmd_inspect)

    live = sub.add_parser("live", help="serve the live viewer: choose a level in the browser and watch the game "
                                       "and every decision as it happens (frontend/ /live)")
    live.add_argument("--config", type=Path, default=Path("configs/watch.yaml"),
                      help="config (default configs/watch.yaml: whole-level frame budget)")
    live.add_argument("--adapter", choices=["fixture", "dave"], default="dave")
    live.add_argument("--store", type=Path, help="episode store (default: live.sqlite next to memory.episode_store)")
    live.add_argument("--host", default="127.0.0.1")
    live.add_argument("--port", type=int, default=8765)
    live.add_argument("--allow-paid", action="store_true",
                      help="PAID: let the viewer start runs with the live Azure planner / Azure or Jev tactical")
    live.add_argument("--tick-ms", type=float, default=14.0,
                      help="wall time per game tick (14 = the game's own speed; 0 = as fast as possible)")
    live.add_argument("--frame-every", type=int, default=3, help="publish a game frame every N ticks")
    live.set_defaults(func=_cmd_live)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    configure_logging(args.log_level)
    try:
        return args.func(args)
    except (ConfigError, AdapterError, StoreError, GraphCheckpointError, InspectError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
