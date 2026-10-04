"""Per-episode benchmark records (docs/benchmark.md, "Metrics").

``episode_record`` turns one ``EpisodeResult`` into a flat JSON record: identity (scenario,
trial, seed, arm, regime), outcome, the primary metrics and the secondary metrics. Every
episode gets a record, including errors and budget stops, so failures are always counted.
Nothing is invented: a cost the provider did not report and no price table covers stays
unknown (``cost_usd`` None, counted in ``cost_unknown_calls``), never zero.
"""

from __future__ import annotations

from collections import Counter
from typing import Any

from dave_agent.evaluation.statistics import percentile
from dave_agent.runner.episode import EpisodeResult
from dave_agent.runner.session import candidate_trace, tokens

COMPLETED = "level_complete"
# Providers that make no network call and cost nothing (the offline mocks and test scripts).
OFFLINE_PROVIDERS = frozenset({"mock", "scripted"})
# Fields that depend on the machine or the clock, not on the experiment: excluded when checking
# that a rerun reproduces a benchmark (manifest, records and summaries).
VOLATILE_FIELDS = frozenset({
    "created_at", "finished_at", "wall_seconds", "model_wait_seconds", "overhead_seconds", "route_ms",
    "wall_seconds_mean", "wall_seconds_total", "model_wait_seconds_mean", "out_dir", "store", "code",
    "runtime", "elapsed_seconds",
})


def failure_key(start: tuple[int, int] | None, skill: str, outcome: str, reason: str | None) -> str:
    """The fixed repeated-failure key: (start tile, skill, failure type), where the failure type
    is the non-completed outcome plus the reason prefix (e.g. ``interrupted:hazard_contact``)."""
    region = "?" if start is None else f"c{start[0]}r{start[1]}"
    kind = outcome if not reason else f"{outcome}:{reason.split(':')[0]}"
    return f"{region}|{skill}|{kind}"


def _cost(calls) -> dict[str, Any]:
    reported = sum((c.cost_usd for c in calls if c.cost_source == "provider_reported"), 0.0)
    estimated = sum((c.cost_usd for c in calls if c.cost_source == "estimated"), 0.0)
    unknown = sum(c.cost_usd is None and c.provider not in OFFLINE_PROVIDERS for c in calls)
    return {"cost_reported_usd": reported, "cost_estimated_usd": estimated, "cost_unknown_calls": unknown,
            "cost_usd": None if unknown else reported + estimated}


def _quantiles(values: list[float]) -> dict[str, float | None]:
    return {"n": len(values), "p50": percentile(values, 50), "p95": percentile(values, 95)}


def episode_record(result: EpisodeResult, identity: dict[str, Any], error: str | None = None) -> dict[str, Any]:
    """One benchmark record. ``identity`` carries the benchmark id, environment, mode, regime,
    scenario, trial, seed, arm, order position, run id, episode key and checkpoint label."""
    calls = result.model_calls
    tactical = [c for c in calls if c.purpose == "tactical"]
    planner = [c for c in calls if c.purpose == "planner"]
    chosen = [d for d in result.decisions if not d.forced]
    completed = result.outcome == COMPLETED
    failures = [failure_key(start, run.skill, run.outcome, run.reason)
                for start, run in zip(result.execution_starts, result.executions, strict=True)
                if run.outcome != "completed"]
    repeated = sum(n - 1 for n in Counter(failures).values())
    wait = sum(c.latency_ms for c in calls if c.latency_ms is not None) / 1000
    ok_latency = [c.latency_ms for c in tactical if c.status == "ok" and c.latency_ms is not None]
    decision_latency = [v for v in result.decision_latency_ms if v is not None]
    return {
        **identity,
        "outcome": result.outcome,
        "termination_reason": result.termination_reason,
        "error": error,
        "completed": completed,
        "frames": result.frames,
        # Censored (None) unless the level was completed.
        "frames_to_completion": result.frames if completed else None,
        "deaths": sum(e.event_type == "death" for e in result.events),
        "lives_left": result.lives,
        "score": result.score,
        "wall_seconds": round(result.wall_seconds, 3),
        "model_wait_seconds": round(wait, 3),
        "overhead_seconds": round(result.wall_seconds - wait, 3),
        "decisions": len(result.decisions),
        "forced_decisions": len(result.decisions) - len(chosen),
        "model_decisions": sum(not d.fallback for d in chosen),
        "fallback_decisions": sum(d.fallback for d in chosen),
        "fallback_reasons": dict(sorted(Counter(d.fallback_reason for d in chosen if d.fallback).items())),
        "tactical_calls": len(tactical),
        "planner_calls": len(planner),
        "call_status": dict(sorted(Counter(f"{c.purpose}:{c.status}" for c in calls if c.status != "ok").items())),
        "invalid_outputs": sum(c.status == "invalid_output" for c in calls),
        "transport_retries": sum(c.retries for c in calls),
        # Extra calls after the first for one tactical decision (invalid output or failed call).
        "tactical_reasks": sum(max(0, n - 1) for n in result.decision_calls),
        "tactical_ok_latency_ms": _quantiles(ok_latency),
        "decision_latency_ms": _quantiles(decision_latency),
        "ok_latencies_ms": ok_latency,
        "decision_latencies_ms": decision_latency,
        "tokens": {"tactical": tokens(tactical), "planner": tokens(planner)},
        **_cost(calls),
        "skill_executions": len(result.executions),
        "skill_outcomes": dict(sorted(Counter(r.outcome for r in result.executions).items())),
        "planning_episodes": len(result.planning),
        "planner_fallbacks": sum(r.fallback for r in result.planning),
        "planning_triggers": dict(sorted(Counter(t for r in result.planning for t in r.triggers).items())),
        "route_replans": sum("route_invalidated" in r.triggers for r in result.planning),
        "route_ms": round(sum(r.route_ms for r in result.planning), 3),
        "failures": len(failures),
        "repeated_failures": repeated,
        "repeated_failure_keys": sorted(k for k, n in Counter(failures).items() if n > 1),
        "candidate_trace": candidate_trace(result),
    }
