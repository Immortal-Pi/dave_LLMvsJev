"""Manifest-driven benchmark runner (docs/benchmark.md).

A benchmark plays every arm on the same initial scenarios: for each scenario and trial ``k``
every arm starts from the same scenario with seed ``benchmark.seed + k``. Arm order is
shuffled per (scenario, trial) from that seed (``randomize_arm_order``) so time-of-day or
service effects do not always favour the same arm. A common start is not a common
trajectory: arms diverge once they choose differently.

Isolation: each episode gets a fresh adapter, fresh models, fresh working memory and its own
store run. Memory regimes are never mixed in one benchmark:

- ``cold``: graph arms start from an empty graph every trial and learn only within the episode
  (labeled ``in-episode``); the graph is saved per arm and trial for inspection only.
- ``warm``: each graph arm reads its own checkpoint, frozen (never updated or saved); the
  checkpoint's sha256 is recorded and checked again at the end.

Everything needed to reproduce the run goes into ``manifest.json`` before the first episode.
Each finished episode is appended to ``episodes.jsonl`` at once, so an interrupted benchmark
keeps its data; summaries are rebuilt from that file (``evaluation/summary.py``). Paid runs
stop scheduling episodes once the spent cost (reported plus estimated) reaches
``benchmark.paid_run_budget_usd``.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import platform
import random
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from importlib import metadata
from pathlib import Path
from typing import Any

from dave_agent.adapters import create_adapter
from dave_agent.config import AppConfig, ConfigError
from dave_agent.evaluation.metrics import episode_record
from dave_agent.evaluation.summary import write_summaries
from dave_agent.memory.episodes import EpisodeStore
from dave_agent.memory.graph import GraphStore
from dave_agent.memory.persistence import GraphCheckpointError, load_store, save_store, store_dir, store_exists
from dave_agent.models import azure, jev
from dave_agent.models.tactical import GAME_RULES, INPUT_GUIDE, TACTICAL_TASK
from dave_agent.runner.episode import EpisodeResult
from dave_agent.runner.session import MODES, Models, build_models, check_arm, effective_settings, run_trial

MANIFEST_VERSION = 1
ENVIRONMENT_LABELS = {
    "fixture": "fixture: synthetic test platformer, not Dangerous Dave",
    "dave": "dave: Dangerous Dave via the deadly-dave bridge",
}
PACKAGES = ("pydantic", "httpx", "networkx", "pyyaml")


@dataclass
class BenchmarkSpec:
    arms: tuple[str, ...]
    trials: int
    scenarios: tuple[str, ...]
    adapter: str
    out_dir: Path
    benchmark_id: str
    live_planner: bool = False
    live_tactical: bool = False
    regime: str = "cold"  # cold | warm
    checkpoints: dict[str, Path] = field(default_factory=dict)
    shared_checkpoint: bool = False
    reach_hints: bool = True
    allow_unpriced: bool = False
    config_path: Path | None = None


def sha256_file(path: Path) -> str:
    """The sha256 of a file, or of a graph store directory (every level checkpoint's name and
    bytes, in name order)."""
    path = Path(path)
    directory = store_dir(path)
    if directory.is_dir():
        digest = hashlib.sha256()
        for file in sorted(directory.glob("*.json")):
            digest.update(file.name.encode() + b"\0" + file.read_bytes())
        return digest.hexdigest()
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def schedule(spec: BenchmarkSpec, base_seed: int, randomize: bool) -> list[dict[str, Any]]:
    """One entry per (scenario, trial): the seed every arm uses and the arm order."""
    out = []
    for scenario in spec.scenarios:
        for k in range(spec.trials):
            order = list(spec.arms)
            if randomize:
                random.Random(f"{base_seed}:{scenario}:{k}").shuffle(order)
            out.append({"scenario": scenario, "trial": k, "seed": base_seed + k, "order": order})
    return out


def prompt_hashes() -> dict[str, str]:
    """Hashes of every model-facing text, so a prompt change shows in the manifest."""
    return {"game_rules": _sha(GAME_RULES), "input_guide": _sha(INPUT_GUIDE), "tactical_task": _sha(TACTICAL_TASK),
            "azure_planner_system": _sha(azure.SYSTEM_PROMPT), "azure_tactical_system": _sha(azure.TACTICAL_PROMPT),
            "jev_request_source": _sha(inspect.getsource(jev.JevTacticalModel.body))}


def code_revision() -> dict[str, Any]:
    try:
        rev = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()
        dirty = bool(subprocess.run(["git", "status", "--porcelain", "--untracked-files=no"], capture_output=True,
                                    text=True, check=True).stdout.strip())
    except (OSError, subprocess.CalledProcessError):
        return {"revision": None, "dirty": None}
    return {"revision": rev, "dirty": dirty}


def runtime() -> dict[str, Any]:
    versions = {}
    for name in ("dave-agent", *PACKAGES):
        try:
            versions[name] = metadata.version(name)
        except metadata.PackageNotFoundError:
            versions[name] = None
    return {"python": sys.version.split()[0], "platform": platform.platform(), "packages": versions}


def scenario_hash(observation) -> str:
    """sha256 of the canonical reset observation, without its per-episode ids."""
    data = observation.model_dump(mode="json", exclude={"episode_id", "observation_id"})
    return _sha(json.dumps(data, sort_keys=True))


def unpriced_roles(config: AppConfig, spec: BenchmarkSpec) -> list[str]:
    """Live roles whose cost is neither provider-reported nor covered by a price table."""
    roles = []
    if spec.live_planner and config.models.planner.price is None:
        roles.append("models.planner")
    if spec.live_tactical and config.models.tactical_llm.price is None \
            and any(config.arms[a].tactical == "llm" for a in spec.arms):
        roles.append("models.tactical_llm")
    return roles


class BenchmarkRunner:
    def __init__(self, config: AppConfig, spec: BenchmarkSpec,
                 adapter_factory: Callable = create_adapter,
                 models_factory: Callable[..., Models] = build_models,
                 notify: Callable[[dict], None] = lambda message: None) -> None:
        self.config, self.spec = config, spec
        self.adapter_factory, self.models_factory, self.notify = adapter_factory, models_factory, notify
        self.mode = MODES[(spec.live_planner, spec.live_tactical)]
        self.out = Path(spec.out_dir)
        self.manifest: dict[str, Any] = {}
        self.records: list[dict[str, Any]] = []
        self.checkpoint_sha: dict[str, str] = {}

    # -- checks before anything runs ---------------------------------------------------------
    def validate(self) -> None:
        cfg, spec = self.config, self.spec
        if spec.regime not in ("cold", "warm"):
            raise ConfigError(f"unknown memory regime {spec.regime!r}; expected cold or warm "
                              "(continual learning is not implemented)")
        if not spec.arms or len(set(spec.arms)) != len(spec.arms):
            raise ConfigError(f"--arms must list distinct arms, got {list(spec.arms)}")
        if spec.trials < 1 or not spec.scenarios:
            raise ConfigError("need at least one trial and one scenario")
        for name in spec.arms:
            if name not in cfg.arms:
                raise ConfigError(f"unknown arm {name!r}; configured arms: {sorted(cfg.arms)}")
            check_arm(name, cfg.arms[name], spec.live_planner, spec.live_tactical)
        graph_arms = [a for a in spec.arms if cfg.arms[a].graph_enabled]
        extra = sorted(set(spec.checkpoints) - set(graph_arms))
        if extra:
            raise ConfigError(f"--checkpoint given for {extra}, which are not graph-enabled arms in this benchmark")
        if spec.regime == "cold" and spec.checkpoints:
            raise ConfigError("the cold regime starts every trial from an empty graph; --checkpoint needs "
                              "--memory-regime warm")
        if spec.regime == "warm":
            missing = [a for a in graph_arms if a not in spec.checkpoints]
            if missing:
                raise ConfigError(f"--memory-regime warm needs a frozen checkpoint per graph arm: "
                                  f"pass --checkpoint {missing[0]}=PATH (make one with train-memory)")
        if self.mode != "mock":
            roles = unpriced_roles(cfg, spec)
            if roles and not spec.allow_unpriced:
                raise ConfigError(
                    f"no cost is reported or priced for {', '.join(roles)}, so the paid-run ceiling "
                    f"(benchmark.paid_run_budget_usd) cannot see that spend; set {roles[0]}.price in "
                    "configs/models.yaml or pass --allow-unpriced")
        if self.out.exists() and any(self.out.iterdir()):
            raise ConfigError(f"benchmark output directory {self.out} is not empty; choose a new --out")
        # Credentials: every arm's models are built (and closed) before the game starts.
        for name in spec.arms:
            self.models_factory(self.config, name, spec.live_planner, spec.live_tactical, 0).close()

    def _probe_scenarios(self) -> tuple[dict, dict[str, dict[str, str]]]:
        """Adapter capabilities and the reset hash of every (scenario, seed): proves the game
        runs before any paid call and pins what 'the same initial scenario' means."""
        adapter = self.adapter_factory(self.spec.adapter, self.config.environment)
        try:
            caps = adapter.capabilities()
            hashes: dict[str, dict[str, str]] = {}
            for scenario in self.spec.scenarios:
                hashes[scenario] = {}
                for k in range(self.spec.trials):
                    seed = self.config.benchmark.seed + k
                    hashes[scenario][str(seed)] = scenario_hash(adapter.reset(scenario, seed))
        finally:
            adapter.close()
        return {"adapter": caps.adapter, "build_id": caps.build_id,
                "frames_per_second": caps.frames_per_second}, hashes

    def _load_warm(self, caps: dict) -> dict[str, dict[str, Any]]:
        info = {}
        for arm, path in sorted(self.spec.checkpoints.items()):
            graph = load_store(path, caps["adapter"], caps["build_id"],
                               self.config.environment.observation_policy)
            others = sorted({e["arm"] for e in graph.lineage} - {arm})
            if others and not self.spec.shared_checkpoint:
                raise GraphCheckpointError(
                    f"{path}: checkpoint was trained by arm(s) {others}, not {arm}; arms must not share route "
                    "knowledge (pass --shared-checkpoint only for an explicit identical-pretrained-graph experiment)")
            self.checkpoint_sha[arm] = sha256_file(path)
            trained = sorted({e["scenario_id"] for e in graph.lineage})
            info[arm] = {"path": str(path), "sha256": self.checkpoint_sha[arm], "training_episodes": len(graph.lineage),
                         "training_scenarios": trained, "trained_by": sorted({e["arm"] for e in graph.lineage}),
                         "same_level_learning": sorted(set(trained) & set(self.spec.scenarios))}
        return info

    # -- manifest ----------------------------------------------------------------------------
    def _write_manifest(self) -> None:
        tmp = self.out / "manifest.json.tmp"
        tmp.write_text(json.dumps(self.manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        tmp.replace(self.out / "manifest.json")

    def _build_manifest(self, caps: dict, hashes: dict, warm: dict, plan: list[dict]) -> None:
        cfg, spec = self.config, self.spec
        models = {}
        for name in spec.arms:
            m = self.models_factory(cfg, name, spec.live_planner, spec.live_tactical, cfg.benchmark.seed)
            try:
                models[name] = effective_settings(cfg, m)
            finally:
                m.close()
        config_dump = cfg.model_dump(mode="json")
        self.manifest = {
            "manifest_version": MANIFEST_VERSION,
            "benchmark_id": spec.benchmark_id,
            "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "status": "running",
            "mode": self.mode,
            "paid": self.mode != "mock",
            "environment": spec.adapter,
            "environment_label": ENVIRONMENT_LABELS.get(spec.adapter, spec.adapter),
            "adapter": caps,
            "code": code_revision(),
            "runtime": runtime(),
            "config_path": None if spec.config_path is None else str(spec.config_path),
            "config_sha256": _sha(json.dumps(config_dump, sort_keys=True)),
            "config": config_dump,
            "prompt_hashes": prompt_hashes(),
            "observation_policy": cfg.environment.observation_policy,
            "execution_mode": cfg.environment.execution_mode,
            "reach_hints": spec.reach_hints,
            "arms": {name: {**cfg.arms[name].model_dump(mode="json"), "settings": models[name]} for name in spec.arms},
            "memory_regime": spec.regime,
            "memory": {name: self._memory_label(name) for name in spec.arms},
            "checkpoints": warm,
            "shared_checkpoint": spec.shared_checkpoint,
            "scenarios": list(spec.scenarios),
            "scenario_hashes": hashes,
            "trials": spec.trials,
            "base_seed": cfg.benchmark.seed,
            "randomize_arm_order": cfg.benchmark.randomize_arm_order,
            "budgets": {"per_episode": {"max_episode_frames": cfg.benchmark.max_episode_frames,
                                        "max_episode_wall_seconds": cfg.benchmark.max_episode_wall_seconds,
                                        "tactical": cfg.tactical.model_dump(mode="json", exclude={"fallback_skills"}),
                                        "planner_max_calls": cfg.planning.max_calls_per_episode},
                        "paid_run_budget_usd": cfg.benchmark.paid_run_budget_usd,
                        "unpriced_roles": unpriced_roles(cfg, spec) if self.mode != "mock" else []},
            "fallback": {"tactical_fallback_skills": list(cfg.tactical.fallback_skills),
                         "on_budget_exhausted": cfg.tactical.on_budget_exhausted,
                         "max_retries": cfg.models.max_retries},
            "prices": {role: None if getattr(cfg.models, role).price is None
                       else getattr(cfg.models, role).price.model_dump(mode="json")
                       for role in ("planner", "tactical_llm")},
            "schedule": plan,
            "episodes_run": 0,
            "not_run": [],
            "spent_usd": {"reported": 0.0, "estimated": 0.0},
        }

    def _memory_label(self, arm: str) -> str:
        if not self.config.arms[arm].graph_enabled:
            return "none"
        return "in-episode" if self.spec.regime == "cold" else "frozen-checkpoint"

    # -- running -----------------------------------------------------------------------------
    def run(self) -> dict[str, Any]:
        self.validate()
        caps, hashes = self._probe_scenarios()
        warm = self._load_warm(caps) if self.spec.regime == "warm" else {}
        plan = schedule(self.spec, self.config.benchmark.seed, self.config.benchmark.randomize_arm_order)
        self.out.mkdir(parents=True, exist_ok=True)
        self._build_manifest(caps, hashes, warm, plan)
        self._write_manifest()
        if self.mode != "mock":
            per_episode = len(self.spec.arms) * len(plan)
            self.notify({"paid_run": self.mode, "episodes": per_episode,
                         "worst_case_calls": {"tactical": per_episode * self.config.tactical.max_calls_per_episode,
                                              "planner": per_episode * self.config.planning.max_calls_per_episode},
                         "paid_run_budget_usd": self.config.benchmark.paid_run_budget_usd,
                         "unpriced_roles": self.manifest["budgets"]["unpriced_roles"]})
        store = EpisodeStore(self.out / "events.sqlite")
        status = "complete"
        pending = [(entry, pos, arm) for entry in plan for pos, arm in enumerate(entry["order"])]
        try:
            for i, (entry, pos, arm) in enumerate(pending):
                if self._over_budget():
                    status = "budget_stopped"
                    self.manifest["not_run"] = [{"scenario": e["scenario"], "trial": e["trial"], "arm": a,
                                                 "reason": "paid_run_budget_usd"} for e, _, a in pending[i:]]
                    break
                record = self._episode(store, entry, pos, arm, caps)
                self.records.append(record)
                with (self.out / "episodes.jsonl").open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(record, sort_keys=True) + "\n")
                spent = self.manifest["spent_usd"]
                spent["reported"] += record["cost_reported_usd"]
                spent["estimated"] += record["cost_estimated_usd"]
                self.manifest["episodes_run"] += 1
                self._write_manifest()
                self.notify({"episode": self.manifest["episodes_run"], "of": len(pending), "scenario": entry["scenario"],
                             "trial": entry["trial"], "arm": arm, "outcome": record["outcome"],
                             "frames": record["frames"], "cost_usd": record["cost_usd"]})
        except KeyboardInterrupt:
            status = "interrupted"
            raise
        finally:
            store.close()
            if status == "interrupted":
                self.manifest["not_run"] = [{"scenario": e["scenario"], "trial": e["trial"], "arm": a,
                                             "reason": "interrupted"} for e, _, a in pending[len(self.records):]]
            self._finish(status)
        return self.manifest

    def _over_budget(self) -> bool:
        if self.mode == "mock":
            return False
        spent = self.manifest["spent_usd"]
        return spent["reported"] + spent["estimated"] >= self.config.benchmark.paid_run_budget_usd

    def _episode(self, store: EpisodeStore, entry: dict, pos: int, arm: str, caps: dict) -> dict[str, Any]:
        cfg, spec = self.config, self.spec
        scenario, trial, seed = entry["scenario"], entry["trial"], entry["seed"]
        run_id = f"{spec.benchmark_id}-{scenario}-t{trial:03d}-{arm}"
        graph_arm = cfg.arms[arm].graph_enabled
        identity = {"benchmark_id": spec.benchmark_id, "environment": spec.adapter, "mode": self.mode,
                    "regime": spec.regime, "checkpoint": self._checkpoint_label(), "scenario": scenario,
                    "trial": trial, "seed": seed, "arm": arm, "order": pos, "run_id": run_id,
                    "episode_key": None, "memory": self._memory_label(arm), "reach_hints": spec.reach_hints,
                    "arm_checkpoint_sha256": self.checkpoint_sha.get(arm),
                    "same_level_learning": bool(self.manifest["checkpoints"].get(arm, {}).get("same_level_learning"))}
        models = self.models_factory(cfg, arm, spec.live_planner, spec.live_tactical, seed)
        adapter = self.adapter_factory(spec.adapter, cfg.environment)
        graph, learn, error = None, False, None
        try:
            if graph_arm:
                if spec.regime == "warm":  # a fresh copy each episode: nothing carries over between trials
                    graph = load_store(spec.checkpoints[arm], caps["adapter"], caps["build_id"],
                                       cfg.environment.observation_policy)
                else:
                    graph = GraphStore(caps["adapter"], caps["build_id"], cfg.environment.observation_policy,
                                       cfg.graph.evidence_per_item)
                    learn = cfg.memory.graph_updates
            result, recorder = run_trial(cfg, arm, models, adapter, spec.adapter, scenario, seed, store, run_id,
                                         "benchmark", graph, learn, spec.reach_hints,
                                         {"benchmark": {"benchmark_id": spec.benchmark_id, "trial": trial,
                                                        "order": pos, "regime": spec.regime}})
            identity["episode_key"] = recorder.episode_key
        except Exception as exc:  # every failure is a result: record it and go on
            error = f"{type(exc).__name__}: {exc}"
            result = getattr(exc, "episode_result", None) or EpisodeResult(
                episode_id="", adapter=spec.adapter, outcome="error", termination_reason=f"error:{error}",
                frames=0, score=None, lives=None)
            result.outcome = "error"
        finally:
            adapter.close()
            models.close()
        if learn and error is None:
            graph.add_lineage(run_id, identity["episode_key"], arm, scenario)
            save_store(graph, self.out / "graphs" / arm / f"{scenario}-t{trial:03d}")
        return episode_record(result, identity, error)

    def _checkpoint_label(self) -> str | None:
        if not self.checkpoint_sha:
            return None
        return _sha(json.dumps(self.checkpoint_sha, sort_keys=True))[:16]

    def _finish(self, status: str) -> None:
        changed = {arm: str(self.spec.checkpoints[arm]) for arm, sha in self.checkpoint_sha.items()
                   if sha256_file(self.spec.checkpoints[arm]) != sha}
        if changed:
            status = "checkpoint_changed"
        self.manifest.update(status=status, finished_at=datetime.now(UTC).isoformat(timespec="seconds"),
                             checkpoints_changed=changed)
        self._write_manifest()
        b = self.config.benchmark
        write_summaries(self.records, self.out, b.confidence, b.bootstrap_samples, b.seed)


def load_records(out_dir: Path) -> list[dict[str, Any]]:
    path = Path(out_dir) / "episodes.jsonl"
    if not path.exists():
        raise ConfigError(f"no episodes.jsonl in {out_dir}")
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def resummarize(out_dir: Path) -> dict[str, Any]:
    """Rebuild summary.json/csv and pairs.csv from episodes.jsonl and the manifest's settings."""
    out_dir = Path(out_dir)
    manifest = json.loads((out_dir / "manifest.json").read_text(encoding="utf-8"))
    bench = manifest["config"]["benchmark"]
    return write_summaries(load_records(out_dir), out_dir, bench["confidence"], bench["bootstrap_samples"],
                           bench["seed"])


def train_memory(config: AppConfig, arm: str, episodes: int, scenarios: tuple[str, ...], adapter_name: str,
                 checkpoint: Path, store_path: Path, live_planner: bool = False, live_tactical: bool = False,
                 run_prefix: str | None = None, adapter_factory: Callable = create_adapter,
                 models_factory: Callable[..., Models] = build_models,
                 notify: Callable[[dict], None] = lambda message: None) -> dict[str, Any]:
    """Training episodes for a warm checkpoint: one arm, updates saved after every episode.
    Scenarios cycle; episode ``i`` uses seed ``benchmark.seed + i``."""
    if arm not in config.arms or not config.arms[arm].graph_enabled:
        raise ConfigError(f"train-memory needs a graph-enabled arm, got {arm!r}")
    models_factory(config, arm, live_planner, live_tactical, 0).close()  # credentials first
    prefix = run_prefix or f"train-{datetime.now(UTC):%Y%m%dT%H%M%S}"
    store = EpisodeStore(store_path)
    outcomes = []
    try:
        for i in range(episodes):
            scenario, seed = scenarios[i % len(scenarios)], config.benchmark.seed + i
            models = models_factory(config, arm, live_planner, live_tactical, seed)
            adapter = adapter_factory(adapter_name, config.environment)
            try:
                caps = adapter.capabilities()
                policy = config.environment.observation_policy
                graph = (load_store(checkpoint, caps.adapter, caps.build_id, policy) if store_exists(checkpoint)
                         else GraphStore(caps.adapter, caps.build_id, policy, config.graph.evidence_per_item))
                run_id = f"{prefix}-{arm}-e{i:03d}"
                result, recorder = run_trial(config, arm, models, adapter, adapter_name, scenario, seed, store,
                                             run_id, "train-memory", graph, True)
            finally:
                adapter.close()
                models.close()
            graph.add_lineage(run_id, recorder.episode_key, arm, scenario)
            checkpoint = save_store(graph, checkpoint)
            outcomes.append({"run_id": run_id, "scenario": scenario, "seed": seed, "outcome": result.outcome,
                             "frames": result.frames, **graph.counts()})
            notify(outcomes[-1])
    finally:
        store.close()
    return {"arm": arm, "checkpoint": str(checkpoint), "sha256": sha256_file(checkpoint), "episodes": outcomes}
