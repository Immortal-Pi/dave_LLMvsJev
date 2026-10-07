"""Episode setup shared by ``play``, ``benchmark`` and ``train-memory``.

``build_models`` creates an arm's planner and tactical controller. The live builders check
credentials, so callers build before starting the game or spending anything. ``run_trial``
wires one episode exactly as ``play`` always has: the same goal manager, working memory,
executor and budgets for every arm, with the graph handed to graph-enabled arms only.
``episode_summary`` is the JSON summary ``play`` prints.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from dave_agent.adapters.base import GameAdapter
from dave_agent.config import AppConfig, ArmConfig, ConfigError
from dave_agent.control.goals import GoalManager
from dave_agent.memory.episodes import EpisodeRecorder, EpisodeStore
from dave_agent.memory.graph import GraphStore
from dave_agent.memory.persistence import load_store, store_dir, store_exists
from dave_agent.memory.working import WorkingMemory
from dave_agent.models.azure import AzureChatClient, AzurePlanner, AzureSettings, AzureTacticalModel
from dave_agent.models.jev import JevClient, JevSettings, JevTacticalModel
from dave_agent.models.planner import RuleMockPlanner
from dave_agent.models.tactical import ModelController, SeededMockModel
from dave_agent.runner.episode import EpisodeResult, run_episode


def azure_planner(config: AppConfig) -> AzurePlanner:
    cfg = config.models.planner
    client = AzureChatClient(AzureSettings.from_env(cfg.deployment_env), config.models.timeout_seconds,
                             config.models.max_retries)
    return AzurePlanner(client, cfg.max_completion_tokens, cfg.reasoning_effort, cfg.price)


def azure_tactical(config: AppConfig) -> AzureTacticalModel:
    cfg = config.models.tactical_llm
    client = AzureChatClient(AzureSettings.from_env(cfg.deployment_env), config.models.timeout_seconds,
                             config.models.max_retries)
    return AzureTacticalModel(client, cfg.max_completion_tokens, cfg.reasoning_effort, cfg.price)


def jev_tactical(config: AppConfig) -> JevTacticalModel:
    client = JevClient(JevSettings.from_env(config.models.jev), config.models.timeout_seconds,
                       config.models.max_retries)
    return JevTacticalModel(client)


# Live tactical model per arm ``tactical`` value; each builder checks credentials first.
LIVE_TACTICAL = {"llm": azure_tactical, "jev": jev_tactical}
MODES = {(False, False): "mock", (True, False): "live-planner", (False, True): "live-tactical", (True, True): "live"}


def check_arm(name: str, arm: ArmConfig, live_planner: bool, live_tactical: bool) -> None:
    if live_planner and arm.planner != "llm":
        raise ConfigError(f"arm {name} uses planner {arm.planner!r}; --planner live needs an LLM planner arm")
    if live_tactical and arm.tactical not in LIVE_TACTICAL:
        raise ConfigError(f"arm {name} uses tactical {arm.tactical!r}; --tactical live supports "
                          f"tactical: {' or '.join(sorted(LIVE_TACTICAL))}")


@dataclass
class Models:
    planner: object
    model: object
    controller: ModelController
    mode: str

    def close(self) -> None:
        for live in (self.planner, self.model):
            if hasattr(live, "client"):
                live.client.close()


def build_models(config: AppConfig, arm_name: str, live_planner: bool, live_tactical: bool, seed: int) -> Models:
    """The arm's planner and tactical controller. Live builders raise ``ConfigError`` on missing
    credentials before anything runs. Offline, every arm gets the same seeded mock, so mock runs
    of different arms are directly comparable."""
    if arm_name not in config.arms:
        raise ConfigError(f"unknown arm {arm_name!r}; configured arms: {sorted(config.arms)}")
    arm = config.arms[arm_name]
    check_arm(arm_name, arm, live_planner, live_tactical)
    planner = azure_planner(config) if live_planner else RuleMockPlanner()
    try:
        model = (LIVE_TACTICAL[arm.tactical](config) if live_tactical
                 else SeededMockModel(seed=seed, label=f"mock-{arm.tactical}"))
    except Exception:
        if hasattr(planner, "client"):
            planner.client.close()
        raise
    controller = ModelController(model, config.tactical, config.models.max_retries)
    return Models(planner, model, controller, MODES[(live_planner, live_tactical)])


def settings(model) -> dict:
    """Effective settings of a planner or tactical model, without credentials."""
    return model.settings() if hasattr(model, "settings") else {"provider": model.provider, "model": model.model}


def effective_settings(config: AppConfig, models: Models) -> dict:
    return {"planner": settings(models.planner), "tactical": settings(models.model),
            "tactical_policy": config.tactical.model_dump(mode="json"), "max_retries": config.models.max_retries,
            "max_episode_frames": config.benchmark.max_episode_frames,
            "max_episode_wall_seconds": config.benchmark.max_episode_wall_seconds}


def budget_notice(config: AppConfig, mode: str) -> dict:
    """The per-episode budget printed before a paid run starts."""
    tactical = config.tactical.model_dump(mode="json")
    return {"paid_run": mode, "budget": {
        "tactical": {k: tactical[k] for k in ("max_calls_per_episode", "max_tokens_per_episode",
                                              "max_cost_usd_per_episode", "on_budget_exhausted")},
        "planner_max_calls_per_episode": config.planning.max_calls_per_episode,
        "max_episode_frames": config.benchmark.max_episode_frames,
        "max_episode_wall_seconds": config.benchmark.max_episode_wall_seconds}}


def open_graph(config: AppConfig, arm_name: str, adapter: GameAdapter, adapter_name: str,
               path: Path | None = None) -> tuple[GraphStore | None, Path | None, bool]:
    """(store, store directory, learn) for ``play`` and ``live``: a graph arm loads or creates its
    own store (``path``, else ``memory.graph_checkpoint``, else ``graphs/arm-<ARM>/<adapter>``
    next to the episode store; a legacy ``X.json`` is split by level), so no route knowledge
    leaks between arms. Other arms get (None, None, False)."""
    if not config.arms[arm_name].graph_enabled:
        return None, None, False
    path = (path or config.memory.graph_checkpoint
            or config.memory.episode_store.parent / "graphs" / f"arm-{arm_name}" / f"{adapter_name}.json")
    caps = adapter.capabilities()
    policy = config.environment.observation_policy
    graph = (load_store(path, caps.adapter, caps.build_id, policy) if store_exists(path)
             else GraphStore(caps.adapter, caps.build_id, policy, config.graph.evidence_per_item))
    return graph, store_dir(path), config.memory.graph_updates


def run_trial(config: AppConfig, arm_name: str, models: Models, adapter: GameAdapter, adapter_name: str,
              scenario: str, seed: int, store: EpisodeStore, run_id: str, command: str,
              graph: GraphStore | None, learn: bool, reach_hints: bool = True,
              extra_config: dict | None = None, on_event=None) -> tuple[EpisodeResult, EpisodeRecorder]:
    """One logged episode. ``graph`` is read by the goal manager on graph-enabled arms; it is
    updated only when ``learn`` is true (a frozen warm checkpoint is read but never written).
    ``reach_hints`` off removes the reachability waypoints and estimated end tiles (an ablation;
    the same for every arm in a run)."""
    arm = config.arms[arm_name]
    use_graph = graph if arm.graph_enabled else None
    goals = GoalManager(models.planner, config.planning, config.models.max_retries,
                        use_graph, config.graph if use_graph is not None else None,
                        reach=config.skills.reach.get(adapter_name) if reach_hints else None,
                        threats=config.skills.executor.threats)
    store.create_run(run_id, mode=models.mode, command=command,
                     config_json=json.dumps({**config.model_dump(mode="json"),
                                             "effective_settings": effective_settings(config, models),
                                             **(extra_config or {})}))
    recorder = store.recorder(run_id, arm_name, models.controller.model, scenario, seed,
                              config.memory.store_batch_size)
    try:
        result = run_episode(
            adapter,
            models.controller,
            config.skills.for_adapter(adapter_name),
            config.skills.executor,
            WorkingMemory.from_config(config),
            scenario_id=scenario,
            seed=seed,
            max_frames=config.benchmark.max_episode_frames,
            recorder=recorder,
            graph=use_graph if learn else None,
            goals=goals,
            max_wall_seconds=config.benchmark.max_episode_wall_seconds,
            evidence=use_graph,  # "past runs" notes, also from a frozen warm checkpoint
            on_event=on_event,  # the live viewer (runner/live.py); write-only
            realtime=config.environment.execution_mode == "real_time",
        )
    except BaseException as exc:
        exc.episode_recorder = recorder  # callers of an interrupted run still know its episode key
        raise
    return result, recorder


def tokens(calls) -> dict[str, float]:
    total: Counter = Counter()
    for c in calls:
        total.update(c.usage or {})
    return dict(sorted(total.items()))


def latency(calls) -> dict | None:
    values = sorted(c.latency_ms for c in calls if c.latency_ms is not None)
    if not values:
        return None
    return {"mean": round(sum(values) / len(values), 1), "p50": values[len(values) // 2], "max": values[-1]}


def candidate_trace(result: EpisodeResult) -> str:
    """Hash of every candidate list offered, in order: equal across arms by construction."""
    return hashlib.sha256("".join(c.digest for c in result.candidate_sets).encode()).hexdigest()[:16]


def episode_summary(result: EpisodeResult, *, mode: str, run_id: str, episode_key: str | None, store: Path,
                    arm: str, models: Models, graph: GraphStore | None, graph_path: Path | None,
                    learn: bool) -> dict:
    planner, controller = models.planner, models.controller
    planner_calls = [c for c in result.model_calls if c.purpose == "planner"]
    tactical_calls = [c for c in result.model_calls if c.purpose == "tactical"]
    chosen = [d for d in result.decisions if not d.forced]
    ended = Counter(e.payload.get("status") for e in result.events if e.event_type in ("goal_achieved", "goal_failed"))
    return {
        "mode": mode,
        "run_id": run_id,
        "episode_key": episode_key,
        "store": str(store),
        "adapter": result.adapter,
        "arm": arm,
        "tactical": controller.model,
        "episode_id": result.episode_id,
        "outcome": result.outcome,
        "termination_reason": result.termination_reason,
        "frames": result.frames,
        "decisions": len(result.decisions),
        "forced_decisions": sum(d.forced for d in result.decisions),
        # A fallback is never a model decision.
        "model_decisions": sum(not d.fallback for d in chosen),
        "fallback_decisions": sum(d.fallback for d in chosen),
        "fallback_reasons": dict(sorted(Counter(d.fallback_reason for d in chosen if d.fallback).items())),
        "tactical_calls": len(tactical_calls),
        "tactical_failures": dict(sorted(Counter(c.status for c in tactical_calls if c.status != "ok").items())),
        "tactical_latency_ms": latency(tactical_calls),
        "planner": f"{planner.provider}:{planner.model}",
        "planner_calls": len(planner_calls),
        "planner_failures": sum(c.status != "ok" for c in planner_calls),
        "tokens": {"tactical": tokens(tactical_calls), "planner": tokens(planner_calls)},
        "cost_usd": None if all(c.cost_usd is None for c in result.model_calls)
        else round(sum(c.cost_usd or 0.0 for c in result.model_calls), 6),
        "goals": {"set": len(result.planning), "achieved": ended["achieved"], "failed": ended["failed"],
                  "expired": ended["expired"]},
        "planning_triggers": dict(sorted(Counter(t for r in result.planning for t in r.triggers).items())),
        # Planning is event-driven (no timer): tactical decisions per planning episode.
        "decisions_per_planning": round(len(result.decisions) / len(result.planning), 2) if result.planning
        else None,
        "fallback_goals": sum(r.fallback for r in result.planning),
        "goal_trace": [r.chosen for r in result.planning],
        "route_ms": round(sum(r.route_ms for r in result.planning), 3),
        "skill_outcomes": dict(sorted(Counter(r.outcome for r in result.executions).items())),
        "interruptions": dict(
            sorted(Counter(r.reason.split(":")[0] for r in result.executions if r.outcome == "interrupted").items())
        ),
        "candidate_trace": candidate_trace(result),
        "score": result.score,
        "lives_left": result.lives,
        "deaths": sum(e.event_type == "death" for e in result.events),
        "last_observation_id": result.observation_ids[-1],
        "graph": None if graph is None else {"checkpoint": str(graph_path), "updated": learn, **graph.counts()},
        "settings": {"planner": settings(planner), "tactical": settings(models.model)},
    }
