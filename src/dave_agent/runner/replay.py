"""Re-run an exported episode and check it reproduces the recorded evidence.

The export holds the scenario, seed and every chosen candidate, so replay needs no
controller. For each decision it checks:
- the offered candidate list and its digest;
- the skill outcome and reason;
- the end observation, ignoring only the per-process ``observation_id`` and ``episode_id``.
"""

from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from dave_agent.adapters.base import GameAdapter
from dave_agent.config import ExecutorConfig, SkillSpec
from dave_agent.control.skills import execute, generate_candidates
from dave_agent.schemas import Observation

_VOLATILE = {"observation_id": 0, "episode_id": "replay"}
REALTIME_EVENTS = frozenset({"decision_latency", "decision_stale"})  # runner/episode.py real-time mode


@dataclass
class ReplayReport:
    episode_key: str
    decisions: int = 0
    mismatches: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.mismatches


def load_jsonl(path: Path | str) -> dict[str, dict[str, list[dict[str, Any]]]]:
    """Group exported records as ``{episode_key: {record_type: [rows]}}`` (runs are skipped)."""
    episodes: dict[str, dict[str, list[dict[str, Any]]]] = defaultdict(lambda: defaultdict(list))
    with Path(path).open(encoding="utf-8") as fh:
        for line in fh:
            record = json.loads(line)
            kind = record.pop("record")
            if kind != "run":
                episodes[record["episode_key"]][kind].append(record)
    return episodes


def _comparable(obs: Observation) -> Observation:
    return obs.model_copy(update=_VOLATILE)


def replay_episode(
    adapter: GameAdapter,
    skills: tuple[SkillSpec, ...],
    executor: ExecutorConfig,
    records: dict[str, list[dict[str, Any]]],
) -> ReplayReport:
    (episode,) = records["episode"]
    report = ReplayReport(episode["episode_key"])
    if any(e["event_type"] in REALTIME_EVENTS for e in records["event"]):
        report.mismatches.append("real-time episode: the game ran on while models thought, so it cannot be "
                                 "replayed exactly (the waits depend on model latency)")
        return report
    observations = {o["observation_id"]: Observation.model_validate(o["observation"])
                    for o in records["observation"]}
    executions = {e["decision_seq"]: e for e in records["skill_execution"]}
    buttons = adapter.capabilities().buttons

    obs = adapter.reset(episode["scenario_id"], episode["seed"])
    for decision in sorted(records["decision"], key=lambda d: d["seq"]):
        seq = decision["seq"]
        recorded_start = observations[decision["observation_id"]]
        if _comparable(obs) != _comparable(recorded_start):
            report.mismatches.append(f"decision {seq}: start observation differs at frame {obs.frame}")
            break
        offered = generate_candidates(skills, buttons, obs)
        if list(offered.ids) != decision["candidate_ids"] or offered.digest() != decision["candidate_digest"]:
            report.mismatches.append(f"decision {seq}: offered candidates differ ({list(offered.ids)})")
            break
        candidate = next(c for c in offered.candidates if c.candidate_id == decision["candidate_id"])
        run = execute(adapter, candidate, skills, obs, executor)
        recorded = executions[seq]
        if (run.outcome, run.reason) != (recorded["outcome"], recorded["reason"]):
            report.mismatches.append(
                f"decision {seq}: outcome {run.outcome}/{run.reason} != recorded {recorded['outcome']}/{recorded['reason']}"
            )
        if _comparable(run.observation) != _comparable(observations[recorded["end_observation_id"]]):
            report.mismatches.append(f"decision {seq}: end observation differs at frame {run.observation.frame}")
        report.decisions += 1
        obs = run.observation
        if report.mismatches:
            break
    if report.ok and episode["frames"] is not None and obs.frame != episode["frames"]:
        report.mismatches.append(f"final frame {obs.frame} != recorded {episode['frames']}")
    return report
