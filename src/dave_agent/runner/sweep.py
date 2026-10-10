"""The offline scoreboard: config values swept over levels and seeds with the route follower
(``scripts/sweep.py``, docs/benchmark.md "Sweep").

Each episode is the real goal manager, threat screen and executor with the rule planner and
``RouteFollower`` (``runner/follower.py``): no model call, so it is free and deterministic. A grid
maps dotted config keys to value lists; every combination is applied to the loaded config and run
on every (scenario, seed). Graph modes: ``none`` (arms A/B), ``cold`` (a fresh graph learning
within the episode) and ``warm`` (``train_episodes`` follower episodes per scenario build a graph,
then each evaluation episode reads a frozen copy, as the benchmark's warm regime).

Output: ``episodes.jsonl`` (one row per episode in grid order; nothing machine- or clock-dependent,
so a rerun with any number of workers is identical) and ``summary.csv`` (one row per config and scenario, plus ``*`` over all scenarios,
ordered by the rank key: completions desc, deaths asc, frames to complete asc).
"""

from __future__ import annotations

import copy
import csv
import itertools
import json
from collections import Counter
from collections.abc import Callable, Iterable
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from dave_agent.adapters import create_adapter
from dave_agent.config import AppConfig, ConfigError
from dave_agent.control.goals import GoalManager
from dave_agent.memory.graph import GraphStore
from dave_agent.memory.working import WorkingMemory, player_tile
from dave_agent.models.planner import RuleMockPlanner
from dave_agent.runner.episode import EpisodeResult, run_episode
from dave_agent.runner.follower import RouteFollower

GRAPH_MODES = ("none", "cold", "warm")
TRAIN_SEED_OFFSET = 1000  # warm training seeds never coincide with evaluation seeds
SUMMARY_FIELDS = ("config_id", "overrides", "scenario", "episodes", "completions", "deaths_mean",
                  "frames_to_complete_mean", "furthest_col_unfinished_mean", "goals_expired_mean",
                  "route_share_mean")


def override(config: AppConfig, key: str, value: Any) -> AppConfig:
    """``config`` with the dotted ``key`` (e.g. ``graph.weights.risk``) set to ``value``,
    validated by the same models as a loaded config."""
    parts = key.split(".")

    def apply(model: BaseModel, path: list[str]) -> BaseModel:
        head = path[0]
        if head not in type(model).model_fields:
            raise ConfigError(f"unknown config key {key!r}: {head!r} is not a field of {type(model).__name__}")
        if len(path) == 1:
            data = {name: getattr(model, name) for name in type(model).model_fields}
            data[head] = value
            try:
                return type(model).model_validate(data)
            except ValueError as exc:
                raise ConfigError(f"invalid value for {key}: {value!r}: {exc}") from exc
        child = getattr(model, head)
        if not isinstance(child, BaseModel):
            raise ConfigError(f"unknown config key {key!r}: {head!r} has no sub-keys")
        return model.model_copy(update={head: apply(child, path[1:])})

    return apply(config, parts)  # type: ignore[return-value]


def grid_configs(grid: dict[str, list[Any]]) -> list[dict[str, Any]]:
    """Every combination of the grid's values, keys in sorted order; ``[{}]`` for an empty grid."""
    keys = sorted(grid)
    for k in keys:
        if not isinstance(grid[k], list) or not grid[k]:
            raise ConfigError(f"grid key {k!r} needs a non-empty list of values")
    return [dict(zip(keys, combo, strict=True)) for combo in itertools.product(*(grid[k] for k in keys))]


def apply_overrides(config: AppConfig, overrides: dict[str, Any]) -> AppConfig:
    for key, value in overrides.items():
        config = override(config, key, value)
    return config


def config_id(overrides: dict[str, Any]) -> str:
    return ",".join(f"{k}={v}" for k, v in overrides.items()) or "baseline"


def episode(config: AppConfig, adapter_name: str, scenario: str, seed: int, frames: int,
            graph: GraphStore | None, learn: bool, adapter_factory: Callable = create_adapter
            ) -> tuple[EpisodeResult, RouteFollower]:
    """One follower episode, wired as ``session.run_trial`` wires a graph-enabled arm when
    ``graph`` is given (routes and past-run notes; learning only when ``learn``)."""
    adapter = adapter_factory(adapter_name, config.environment)
    goals = GoalManager(RuleMockPlanner(), config.planning, config.models.max_retries,
                        graph, config.graph if graph is not None else None,
                        reach=config.skills.reach.get(adapter_name), threats=config.skills.executor.threats)
    follower = RouteFollower()
    try:
        result = run_episode(adapter, follower, config.skills.for_adapter(adapter_name), config.skills.executor,
                             WorkingMemory.from_config(config), scenario, seed, frames,
                             graph=graph if learn else None, goals=goals, evidence=graph)
    finally:
        adapter.close()
    return result, follower


def new_graph(config: AppConfig, adapter_name: str, adapter_factory: Callable) -> GraphStore:
    adapter = adapter_factory(adapter_name, config.environment)
    try:
        caps = adapter.capabilities()
    finally:
        adapter.close()
    return GraphStore(caps.adapter, caps.build_id, config.environment.observation_policy,
                      config.graph.evidence_per_item)


def furthest_col(result: EpisodeResult) -> int | None:
    cols = [player_tile(run.observation.player_position).col for run in result.executions
            if run.observation.player_position is not None]
    return max(cols) if cols else None


def row(result: EpisodeResult, follower: RouteFollower, cid: str, overrides: dict[str, Any],
        scenario: str, seed: int, graph_mode: str) -> dict[str, Any]:
    events = Counter(e.event_type for e in result.events)
    ended = [e.payload.get("status") for e in result.events if e.event_type == "goal_failed"]
    chosen = follower.followed + follower.unguided
    return {
        "config_id": cid, "overrides": overrides, "scenario": scenario, "seed": seed, "graph": graph_mode,
        "outcome": result.outcome, "termination_reason": result.termination_reason,
        "completed": result.outcome == "level_complete", "frames": result.frames,
        "deaths": events["death"], "score": result.score, "lives_left": result.lives,
        "furthest_col": furthest_col(result), "decisions": len(result.decisions),
        "goals_set": events["goal_set"], "goals_achieved": events["goal_achieved"],
        "goals_expired": ended.count("expired"), "goals_failed": len(ended) - ended.count("expired"),
        "goal_trace": [e.payload.get("target_ref") for e in result.events if e.event_type == "goal_set"],
        "planning_triggers": dict(sorted(Counter(t for r in result.planning for t in r.triggers).items())),
        "route_share": round(follower.followed / chosen, 3) if chosen else None,
    }


def _mean(values: Iterable[float | None]) -> float | None:
    vals = [v for v in values if v is not None]
    return round(sum(vals) / len(vals), 1) if vals else None


def summarize(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One row per (config, scenario) and per config over all scenarios (``*``), best first by
    the rank key within each scenario."""
    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for r in rows:
        groups.setdefault((r["config_id"], r["scenario"]), []).append(r)
        groups.setdefault((r["config_id"], "*"), []).append(r)
    out = []
    for (cid, scenario), rs in groups.items():
        out.append({
            "config_id": cid, "overrides": json.dumps(rs[0]["overrides"], sort_keys=True), "scenario": scenario,
            "episodes": len(rs), "completions": sum(r["completed"] for r in rs),
            "deaths_mean": _mean(r["deaths"] for r in rs),
            "frames_to_complete_mean": _mean(r["frames"] for r in rs if r["completed"]),
            "furthest_col_unfinished_mean": _mean(r["furthest_col"] for r in rs if not r["completed"]),
            "goals_expired_mean": _mean(r["goals_expired"] for r in rs),
            "route_share_mean": _mean(r["route_share"] for r in rs),
        })
    return sorted(out, key=lambda s: (s["scenario"], *rank_key(s)))


def rank_key(summary: dict[str, Any]) -> tuple:
    """Lower is better: more completions, then fewer deaths, then fewer frames to complete, then
    further along on the levels not finished."""
    inf = float("inf")
    frames = summary["frames_to_complete_mean"]
    col = summary["furthest_col_unfinished_mean"]
    return (-summary["completions"], summary["deaths_mean"] if summary["deaths_mean"] is not None else inf,
            frames if frames is not None else inf, -(col if col is not None else -inf))


def unit(cfg: AppConfig, overrides: dict[str, Any], scenario: str, seeds: tuple[int, ...], frames: int,
         graph_mode: str, adapter_name: str, train_episodes: int,
         adapter_factory: Callable = create_adapter) -> list[dict[str, Any]]:
    """One config on one scenario: the warm graph's training (if any), then every seed. A
    top-level function, so it can run in a worker process."""
    cid = config_id(overrides)
    trained = None
    if graph_mode == "warm":
        trained = new_graph(cfg, adapter_name, adapter_factory)
        for i in range(train_episodes):
            episode(cfg, adapter_name, scenario, TRAIN_SEED_OFFSET + i, frames, trained, True, adapter_factory)
    rows = []
    for seed in seeds:
        if graph_mode == "warm":
            graph, learn = copy.deepcopy(trained), False
        elif graph_mode == "cold":
            graph, learn = new_graph(cfg, adapter_name, adapter_factory), True
        else:
            graph, learn = None, False
        result, follower = episode(cfg, adapter_name, scenario, seed, frames, graph, learn, adapter_factory)
        rows.append(row(result, follower, cid, overrides, scenario, seed, graph_mode))
    return rows


def run_sweep(config: AppConfig, grid: dict[str, list[Any]], scenarios: tuple[str, ...], seeds: tuple[int, ...],
              frames: int, graph_mode: str, out: Path, adapter_name: str = "dave", train_episodes: int = 3,
              adapter_factory: Callable = create_adapter, workers: int = 1,
              notify: Callable[[dict], None] = lambda message: None) -> list[dict[str, Any]]:
    """Run the sweep and return the summary rows. Each (config, scenario) is one unit of work,
    run in ``workers`` processes (each episode starts its own game process). Rows go to
    ``episodes.partial.jsonl`` as units finish, then to ``episodes.jsonl`` in grid order, so the
    result does not depend on ``workers``."""
    if graph_mode not in GRAPH_MODES:
        raise ConfigError(f"graph mode must be one of {GRAPH_MODES}, got {graph_mode!r}")
    out = Path(out)
    if out.exists() and any(out.iterdir()):
        raise ConfigError(f"sweep output directory {out} is not empty; choose a new --out")
    combos = grid_configs(grid)
    configs = [(o, apply_overrides(config, o)) for o in combos]  # every value checked before any episode
    out.mkdir(parents=True, exist_ok=True)
    (out / "sweep.json").write_text(json.dumps({
        "grid": grid, "scenarios": scenarios, "seeds": seeds, "frames": frames, "graph": graph_mode,
        "adapter": adapter_name, "train_episodes": train_episodes if graph_mode == "warm" else 0,
        "base_config": config.model_dump(mode="json")}, indent=2, default=str), encoding="utf-8")
    units = [(i, (cfg, overrides, scenario, seeds, frames, graph_mode, adapter_name, train_episodes,
                  adapter_factory))
             for i, ((overrides, cfg), scenario) in enumerate(itertools.product(configs, scenarios))]
    done: dict[int, list[dict[str, Any]]] = {}
    with (out / "episodes.partial.jsonl").open("w", encoding="utf-8", newline="\n") as partial:
        def finished(i: int, rows: list[dict[str, Any]]) -> None:
            done[i] = rows
            for r in rows:
                partial.write(json.dumps(r, sort_keys=True) + "\n")
                notify(r)
            partial.flush()

        if workers <= 1:
            for i, args in units:
                finished(i, unit(*args))
        else:
            with ProcessPoolExecutor(max_workers=workers) as pool:
                futures = {pool.submit(unit, *args): i for i, args in units}
                for future in as_completed(futures):
                    finished(futures[future], future.result())
    rows = [r for i in sorted(done) for r in done[i]]
    with (out / "episodes.jsonl").open("w", encoding="utf-8", newline="\n") as f:
        f.writelines(json.dumps(r, sort_keys=True) + "\n" for r in rows)
    (out / "episodes.partial.jsonl").unlink()
    summary = summarize(rows)
    with (out / "summary.csv").open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=SUMMARY_FIELDS)
        writer.writeheader()
        writer.writerows(summary)
    return summary
