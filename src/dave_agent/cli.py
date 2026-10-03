"""dave-agent command line.

Modes: --mock runs offline mock controllers (no network, no cost). Live controller
modes arrive in Phases 7-8. The 'fixture' adapter is a synthetic test platformer,
never Dangerous Dave; the 'dave' adapter drives deadly-dave through the stepping bridge.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
import sys
from pathlib import Path

from dave_agent.adapters import create_adapter
from dave_agent.adapters.base import AdapterError
from dave_agent.config import ConfigError, load_config
from dave_agent.logging_setup import configure_logging
from dave_agent.models.mock import SeededMockController
from dave_agent.runner.episode import run_episode

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
    adapter_name = args.adapter or config.environment.adapter
    adapter = create_adapter(adapter_name, config.environment, args.watch, args.watch_delay)
    # Both arms use the same seeded mock so offline runs are directly comparable.
    controller = SeededMockController(seed=args.seed, label=f"mock-{arm.tactical}")
    try:
        result = run_episode(
            adapter,
            controller,
            config.skills.for_adapter(adapter_name),
            config.skills.executor,
            scenario_id=args.scenario or config.scenario,
            seed=args.seed,
            max_frames=config.benchmark.max_episode_frames,
        )
    finally:
        adapter.close()
    summary = {
        "mode": "mock",
        "adapter": result.adapter,
        "arm": args.arm,
        "tactical": controller.model,
        "episode_id": result.episode_id,
        "outcome": result.outcome,
        "frames": result.frames,
        "decisions": len(result.decisions),
        "forced_decisions": sum(d.forced for d in result.decisions),
        "model_calls": len(result.model_calls),
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
    }
    print(json.dumps(summary, indent=2))
    return 0


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
    play.add_argument("--mock", action="store_true", help="offline mock controllers (required for now)")
    play.add_argument("--adapter", choices=["fixture", "dave"])
    play.add_argument("--scenario")
    play.add_argument("--seed", type=int, default=0)
    _add_watch_args(play)
    play.set_defaults(func=_cmd_play)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    configure_logging(args.log_level)
    try:
        return args.func(args)
    except (ConfigError, AdapterError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
