"""Decision inspector: rebuild exactly what the models saw at each decision of a recorded episode.

The store keeps observations, offered ids, Jev's probabilities and a digest of every tactical
request, but not the request text itself (candidate notes included). The inspector replays the
episode on the real game with the recorded choices: the planner returns the recorded goals, the
tactical controller returns the recorded candidates. Every rebuilt request must reproduce the
recorded ``context_digest``; the first mismatch stops the inspection, so a bundle is never shown
as exact when it is not.

For each selected decision the bundle holds the exact tactical request, the exact Jev and Azure
request bodies, the recorded answer, the planner request behind the current goal, a screenshot,
and (unless disabled) what every offered candidate really does when executed from that state.
``ask`` sends the rebuilt request to a live model (paid; opt-in) and stores the answer.
See docs/inspector.md.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable

from dave_agent.adapters import create_adapter
from dave_agent.adapters.base import GameAdapter
from dave_agent.config import AppConfig, ConfigError
from dave_agent.control.goals import GoalManager
from dave_agent.control.skills import execute
from dave_agent.memory.graph import GraphStore
from dave_agent.memory.persistence import load_checkpoint, split_levels, store_dir
from dave_agent.memory.working import WorkingMemory, player_tile
from dave_agent.models.azure import AzureTacticalModel, request_payload
from dave_agent.models.jev import JevTacticalModel
from dave_agent.models.planner import PlanningRequest
from dave_agent.models.tactical import TacticalOutputError, TacticalRequest, context_digest, tactical_request
from dave_agent.runner.episode import run_episode
from dave_agent.runner.session import azure_tactical, jev_tactical
from dave_agent.schemas import Decision, ModelCallRecord, Observation, SkillCandidate, SnapshotRef

SCHEMA_VERSION = 1
ASK_BUILDERS = {"azure": azure_tactical, "jev": jev_tactical}


class InspectError(RuntimeError):
    """The recorded episode cannot be rebuilt exactly (or cannot be found)."""


class _Exhausted(Exception):
    """The replay asked for more decisions than were recorded (the run was interrupted)."""


# -- reading the store ---------------------------------------------------------------------------
@dataclass
class Recorded:
    run_id: str
    episode_key: str
    arm: str
    controller: str
    adapter: str
    scenario: str
    seed: int
    episode: dict[str, Any]
    config: AppConfig
    reach_hints: bool
    decisions: list[dict[str, Any]]
    calls: dict[int, dict[str, Any]]
    goal_events: list[dict[str, Any]]


def _connect(store: Path) -> sqlite3.Connection:
    if not store.exists():
        raise InspectError(f"episode store not found: {store}")
    conn = sqlite3.connect(f"file:{store.as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def load_recorded(store: Path, run_id: str) -> Recorded:
    conn = _connect(store)
    try:
        run = conn.execute("SELECT config_json FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        episodes = conn.execute("SELECT * FROM episodes WHERE run_id = ?", (run_id,)).fetchall()
        if run is None or not episodes:
            raise InspectError(f"run {run_id!r} not found in {store}")
        if len(episodes) != 1:
            raise InspectError(f"run {run_id!r} has {len(episodes)} episodes; inspect expects one")
        ep = dict(episodes[0])
        key = ep["episode_key"]
        raw = json.loads(run["config_json"])
        config = AppConfig.model_validate({k: v for k, v in raw.items() if k in AppConfig.model_fields})
        if config.environment.execution_mode == "real_time":
            raise InspectError(f"run {run_id!r} ran in real time (the game ran on while models thought); "
                               "it cannot be replayed exactly, so it cannot be inspected")
        decisions = [dict(r) for r in conn.execute(
            "SELECT d.*, s.skill, s.outcome AS skill_outcome, s.reason AS skill_reason, s.frames AS skill_frames, "
            "o.observation_json, eo.observation_json AS end_json FROM decisions d "
            "LEFT JOIN skill_executions s ON s.episode_key = d.episode_key AND s.decision_seq = d.seq "
            "LEFT JOIN observations o ON o.episode_key = d.episode_key AND o.observation_id = d.observation_id "
            "LEFT JOIN observations eo ON eo.episode_key = d.episode_key AND eo.observation_id = s.end_observation_id "
            "WHERE d.episode_key = ? ORDER BY d.seq", (key,))]
        calls = {r["seq"]: dict(r) for r in conn.execute(
            "SELECT * FROM model_calls WHERE episode_key = ? ORDER BY seq", (key,))}
        goal_events = [dict(r) for r in conn.execute(
            "SELECT seq, frame, event_type, payload_json FROM events WHERE episode_key = ? AND event_type LIKE 'goal%' "
            "ORDER BY seq", (key,))]
    finally:
        conn.close()
    for e in goal_events:
        e["payload"] = json.loads(e.pop("payload_json") or "{}")
    return Recorded(run_id=run_id, episode_key=key, arm=ep["arm"], controller=ep["controller"], adapter=ep["adapter"],
                    scenario=ep["scenario_id"], seed=ep["seed"], episode=ep, config=config,
                    reach_hints=raw.get("reach_hints", True), decisions=decisions, calls=calls,
                    goal_events=goal_events)


# -- replay models ---------------------------------------------------------------------------------
def planner_outputs(goal_events: list[dict[str, Any]]) -> list[str | None]:
    """The planner answers in call order, rebuilt from the recorded ``goal_set`` events: failed
    attempts become failed calls, the accepted attempt returns the recorded goal."""
    outputs: list[str | None] = []
    for e in goal_events:
        if e["event_type"] != "goal_set":
            continue
        p = e["payload"]
        attempts = int(p.get("attempts") or 0)
        if p.get("fallback"):
            outputs.extend([None] * attempts)
        elif attempts:
            outputs.extend([None] * (attempts - 1))
            outputs.append(json.dumps({"goal": p["target_ref"], "rationale": p.get("rationale") or "",
                                       "waypoints": p.get("waypoints") or []}))
    return outputs


class ReplayPlanner:
    provider, model = "replay", "replay-planner"

    def __init__(self, outputs: list[str | None]) -> None:
        self.outputs = list(outputs)
        self.requests: list[PlanningRequest] = []

    def propose(self, request: PlanningRequest, feedback: str | None = None) -> tuple[str | None, ModelCallRecord]:
        if not self.outputs:
            raise InspectError("the replay made more planner calls than were recorded; the replay diverged")
        self.requests.append(request)
        out = self.outputs.pop(0)
        return out, ModelCallRecord(provider=self.provider, model=self.model, purpose="planner", latency_ms=0.0,
                                    status="ok" if out is not None else "error")


@dataclass
class Captured:
    seq: int
    request: TacticalRequest
    candidates: list[SkillCandidate]
    snapshot: SnapshotRef
    planner_requests: int  # planner requests made before this decision


@dataclass
class ReplayController:
    """Returns the recorded choices and checks every rebuilt request against its recorded digest."""

    decisions: list[dict[str, Any]]
    adapter: GameAdapter
    planner: ReplayPlanner
    provider: str = "replay"
    model: str = "replay-tactical"
    captured: dict[int, Captured] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self._queue = [d for d in self.decisions if not d["forced"]]

    def decide(self, observation: Observation, goal, candidates: list[SkillCandidate],
               memory) -> tuple[Decision, tuple[ModelCallRecord, ...]]:
        if not self._queue:
            raise _Exhausted()
        d = self._queue.pop(0)
        request = tactical_request(observation, goal, candidates, memory)
        digest = context_digest(request)
        if d["context_digest"] is None:
            raise InspectError(f"decision {d['seq']} has no recorded context digest; cannot verify the rebuild")
        if digest != d["context_digest"]:
            raise InspectError(f"decision {d['seq']} (frame {observation.frame}): rebuilt request digest {digest} "
                               f"!= recorded {d['context_digest']}; the replay diverged")
        if d["candidate_id"] not in request.candidate_ids:
            raise InspectError(f"decision {d['seq']}: recorded {d['candidate_id']} is not offered in the replay")
        self.captured[d["seq"]] = Captured(d["seq"], request, list(candidates), self.adapter.save_snapshot(),
                                           len(self.planner.requests))
        return Decision(candidate_id=d["candidate_id"], observation_id=observation.observation_id,
                        goal_id=goal.goal_id if goal else None, provider_score=d["provider_score"],
                        provider_score_meaning=d["provider_score_meaning"], fallback=bool(d["fallback"]),
                        fallback_reason="replay" if d["fallback"] else None, context_digest=digest), ()


# -- graph -------------------------------------------------------------------------------------------
def default_graph_path(config: AppConfig, arm: str, adapter: str) -> Path:
    return (config.memory.graph_checkpoint
            or Path(config.memory.episode_store).parent / "graphs" / f"arm-{arm}" / f"{adapter}.json")


def _before(file: Path, run_id: str, caps, policy: str):
    """A checkpoint file as it was before ``run_id``: itself, or its ``.bak`` when the file already
    includes the run; None when neither does."""
    for candidate in (file, file.with_name(file.name + ".bak")):
        if candidate.exists():
            graph = load_checkpoint(candidate, caps.adapter, caps.build_id, policy)
            if all(entry["run_id"] != run_id for entry in graph.lineage):
                return graph
    return None


def graph_before(path: Path, run_id: str, adapter: GameAdapter, config: AppConfig) -> tuple[GraphStore, Path]:
    """The graph store as it was before ``run_id``, level by level: each level checkpoint, or its
    ``.bak`` when the file already includes the run; a level first learned in the run is left
    out (its lineage starts with the run). A legacy combined checkpoint is split by level.
    Refused when a level learned before the run has no saved version from before it."""
    caps = adapter.capabilities()
    policy = config.environment.observation_policy
    directory = store_dir(path)
    if directory.is_dir():
        store = GraphStore(caps.adapter, caps.build_id, policy, config.graph.evidence_per_item)
        files = sorted(directory.glob("*.json"))
        for file in files:
            graph = _before(file, run_id, caps, policy)
            if graph is None:
                lineage = load_checkpoint(file).lineage
                if lineage and lineage[0]["run_id"] == run_id:
                    continue  # the level was first learned in this run: absent before it
                break  # learned before the run, but no saved version predates it
            graph.level_id = graph.level_id or file.stem
            store.levels[graph.level_id] = graph
        else:
            if files:
                return store, directory
    elif path.suffix == ".json":
        graph = _before(path, run_id, caps, policy)
        if graph is not None:
            return split_levels(graph), path
    raise InspectError(f"no graph checkpoint from before run {run_id!r} at {path} (or its .bak); "
                       "pass --graph with the checkpoint the run started from, or --graph empty")


# -- the inspection ----------------------------------------------------------------------------------
def parse_selection(text: str | None) -> set[int] | None:
    if not text:
        return None
    out: set[int] = set()
    for part in text.split(","):
        part = part.strip()
        if "-" in part:
            lo, hi = (int(x) for x in part.split("-", 1))
            out.update(range(lo, hi + 1))
        elif part:
            out.add(int(part))
    return out


def _tile(obs: Observation | None) -> list[int] | None:
    if obs is None or obs.player_position is None:
        return None
    t = player_tile(obs.player_position)
    return [t.col, t.row]


def _obs_tile(observation_json: str | None) -> list[int] | None:
    if not observation_json:
        return None
    p = json.loads(observation_json).get("player_position")
    return None if p is None else [(p["x"] + 8) // 16, (p["y"] + 8) // 16]


def _bodies(request: TacticalRequest, config: AppConfig) -> dict[str, Any]:
    """The exact request bodies each provider gets, built without a network client."""
    jev = JevTacticalModel(SimpleNamespace(settings=SimpleNamespace(model_id=config.models.jev.model_id)))
    cfg = config.models.tactical_llm
    azure = AzureTacticalModel(SimpleNamespace(settings=SimpleNamespace(deployment="<deployment>")),
                               cfg.max_completion_tokens, cfg.reasoning_effort)
    return {"jev": jev.body(request), "azure": azure.body(request)}


def _outcomes(adapter: GameAdapter, snap: SnapshotRef, candidates: list[SkillCandidate],
              config: AppConfig, adapter_name: str) -> list[dict[str, Any]]:
    skills = config.skills.for_adapter(adapter_name)
    out = []
    for c in candidates:
        start = adapter.load_snapshot(snap)
        run = execute(adapter, c, skills, start, config.skills.executor)
        trajectory = [[s.observation.player_position.x, s.observation.player_position.y]
                      for s in run.steps if s.observation.player_position is not None]
        events = sorted({e.event_type for e in run.events} - {"skill_started", "skill_finished", "moved"})
        end = run.observation
        out.append({
            "candidate_id": c.candidate_id, "skill": c.skill, "outcome": run.outcome, "reason": run.reason,
            "fatal": run.reason in ("death", "hazard_contact") or "death" in events or end.player_state == "burning",
            "end_tile": _tile(end), "end_px": None if end.player_position is None else [end.player_position.x,
                                                                                         end.player_position.y],
            "end_state": end.player_state, "frames": run.frames, "events": events,
            "score_delta": None if end.score is None or start.score is None else end.score - start.score,
            "trajectory": trajectory[::2] + ([trajectory[-1]] if trajectory and len(trajectory) % 2 == 0 else []),
        })
    adapter.load_snapshot(snap)
    return out


def _screenshot(adapter: GameAdapter, snap: SnapshotRef, path: Path) -> str | None:
    if not hasattr(adapter, "screenshot"):
        return None
    adapter.load_snapshot(snap)
    adapter.screenshot(path)
    return path.name if path.exists() else None


def _recorded_answer(d: dict[str, Any], calls: dict[int, dict[str, Any]]) -> dict[str, Any] | None:
    call = calls.get(d["model_call_seq"]) if d["model_call_seq"] is not None else None
    if call is None:
        return None
    output = json.loads(call["output_json"]) if call["output_json"] else None
    return {"provider": call["provider"], "model": call["model"], "status": call["status"],
            "latency_ms": call["latency_ms"], "cost_usd": call["cost_usd"],
            "usage": json.loads(call["usage_json"]) if call["usage_json"] else None,
            "chosen": d["candidate_id"], "provider_score": d["provider_score"],
            "probabilities": (output or {}).get("probabilities"), "confidence": (output or {}).get("confidence")}


def _goal_at(goal_events: list[dict[str, Any]], frame: int) -> dict[str, Any] | None:
    current = None
    for e in goal_events:
        if e["event_type"] == "goal_set" and e["frame"] <= frame:
            current = {"frame": e["frame"], **e["payload"]}
    return current


def inspect_run(store: Path, run_id: str, out: Path, graph: str | None = None, select: set[int] | None = None,
                outcomes: bool = True, ask: tuple[str, ...] = (), notify: Callable[[dict], None] = lambda m: None,
                ) -> dict[str, Any]:
    rec = load_recorded(store, run_id)
    config = rec.config
    if ask:
        if select is None:
            raise ConfigError("--ask needs --decisions (each asked decision is a paid call)")
        asked_models = {name: ASK_BUILDERS[name](config) for name in ask}  # credentials checked here
    else:
        asked_models = {}
    planner = ReplayPlanner(planner_outputs(rec.goal_events))
    adapter = create_adapter(rec.adapter, config.environment)
    try:
        arm = config.arms.get(rec.arm)
        use_graph, graph_source = None, None
        if arm is not None and arm.graph_enabled:
            if graph == "empty":
                caps = adapter.capabilities()
                use_graph = GraphStore(caps.adapter, caps.build_id, config.environment.observation_policy,
                                       config.graph.evidence_per_item)
                graph_source = "empty"
            else:
                path = Path(graph) if graph else default_graph_path(config, rec.arm, rec.adapter)
                use_graph, source = graph_before(path, run_id, adapter, config)
                graph_source = str(source)
        controller = ReplayController(rec.decisions, adapter, planner)
        goals = GoalManager(planner, config.planning, config.models.max_retries, use_graph,
                            config.graph if use_graph is not None else None,
                            reach=config.skills.reach.get(rec.adapter) if rec.reach_hints else None,
                            threats=config.skills.executor.threats)
        notify({"replaying": run_id, "decisions": len(rec.decisions), "graph": graph_source})
        try:
            result = run_episode(adapter, controller, config.skills.for_adapter(rec.adapter), config.skills.executor,
                                 WorkingMemory.from_config(config), rec.scenario, rec.seed,
                                 max_frames=config.benchmark.max_episode_frames,
                                 graph=use_graph if config.memory.graph_updates else None, goals=goals,
                                 evidence=use_graph)
            replayed = result.decisions
        except _Exhausted as exc:
            replayed = exc.episode_result.decisions
        recorded_ids = [d["candidate_id"] for d in rec.decisions]
        if [d.candidate_id for d in replayed] != recorded_ids[:len(replayed)] or len(replayed) < len(recorded_ids):
            raise InspectError(f"the replay made {len(replayed)} decisions, the record has {len(recorded_ids)}")

        decisions_dir = out / "decisions"
        decisions_dir.mkdir(parents=True, exist_ok=True)
        chosen = [s for s in sorted(controller.captured) if select is None or s in select]
        by_seq = {d["seq"]: d for d in rec.decisions}
        for n, seq in enumerate(chosen, 1):
            d, cap = by_seq[seq], controller.captured[seq]
            path = decisions_dir / f"{seq}.json"
            previous = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
            planner_request = planner.requests[cap.planner_requests - 1] if cap.planner_requests else None
            entry = {
                "schema_version": SCHEMA_VERSION, "seq": seq, "frame": d["frame"], "tile": _tile_req(cap.request),
                "chosen": d["candidate_id"], "skill": d["skill"], "goal_id": d["goal_id"],
                "fallback": bool(d["fallback"]), "context_digest": d["context_digest"], "digest_match": True,
                "request": cap.request.model_dump(mode="json"),
                "bodies": _bodies(cap.request, config),
                "recorded": _recorded_answer(d, rec.calls),
                "executed": {"skill": d["skill"], "outcome": d["skill_outcome"], "reason": d["skill_reason"],
                             "frames": d["skill_frames"], "end_tile": _obs_tile(d["end_json"])},
                "planner": {"goal": _goal_at(rec.goal_events, d["frame"]),
                            "request": None if planner_request is None else request_payload(planner_request)},
                "outcomes": _outcomes(adapter, cap.snapshot, cap.candidates, config, rec.adapter) if outcomes else None,
                "screenshot": _screenshot(adapter, cap.snapshot, decisions_dir / f"{seq}.bmp"),
                "asked": previous.get("asked", []),
            }
            for name, model in asked_models.items():
                entry["asked"].append(_ask(model, cap.request))
            path.write_text(json.dumps(entry, indent=1), encoding="utf-8")
            notify({"decision": seq, "done": n, "of": len(chosen)})
    finally:
        adapter.close()
        for model in asked_models.values():
            model.client.close()

    run_info = {
        "schema_version": SCHEMA_VERSION, "run_id": run_id, "episode_key": rec.episode_key, "arm": rec.arm,
        "controller": rec.controller, "adapter": rec.adapter, "scenario": rec.scenario, "seed": rec.seed,
        "store": str(store), "graph": graph_source, "inspected_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "episode": {k: rec.episode[k] for k in ("outcome", "termination_reason", "frames", "score", "lives", "deaths",
                                                "decisions", "model_calls")},
        "digests_verified": len(controller.captured),
        "decisions": [{"seq": d["seq"], "frame": d["frame"], "tile": _obs_tile(d["observation_json"]),
                       "chosen": d["candidate_id"], "skill": d["skill"], "outcome": d["skill_outcome"],
                       "reason": d["skill_reason"], "end_tile": _obs_tile(d["end_json"]), "goal_id": d["goal_id"],
                       "forced": bool(d["forced"]), "fallback": bool(d["fallback"]),
                       "provider_score": d["provider_score"],
                       "fatal": d["skill_reason"] in ("death", "hazard_contact"),
                       "inspected": (out / "decisions" / f"{d['seq']}.json").exists()} for d in rec.decisions],
        "goals": [{"seq": e["seq"], "frame": e["frame"], "event_type": e["event_type"], **e["payload"]}
                  for e in rec.goal_events],
    }
    (out / "run.json").write_text(json.dumps(run_info, indent=1), encoding="utf-8")
    return {"run_id": run_id, "out": str(out), "digests_verified": len(controller.captured),
            "decisions_written": len(chosen), "graph": graph_source, "asked": list(ask)}


def _tile_req(request: TacticalRequest) -> list[int] | None:
    return request.player.get("tile")


def _ask(model, request: TacticalRequest) -> dict[str, Any]:
    text, call = model.propose(request)
    chosen, error = None, None
    if call.status == "ok":
        try:
            chosen = model.parse(text, request.candidate_ids).candidate_id
        except TacticalOutputError as exc:
            error = str(exc)
    return {"provider": call.provider, "model": call.model, "status": call.status, "chosen": chosen,
            "error": error, "probabilities": (call.output or {}).get("probabilities"),
            "confidence": (call.output or {}).get("confidence"), "usage": call.usage, "cost_usd": call.cost_usd,
            "cost_source": call.cost_source, "latency_ms": call.latency_ms,
            "asked_at": datetime.now(UTC).isoformat(timespec="seconds")}
