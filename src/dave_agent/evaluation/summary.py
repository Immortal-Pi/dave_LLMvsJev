"""Benchmark summaries from per-episode records (docs/benchmark.md, "Summaries").

Groups are (environment, mode, regime, checkpoint, scenario, arm): mock and live, fixture and
Dave, cold and warm, and different warm checkpoints are never pooled. Every record counts,
whatever its outcome, so errors and budget stops are failures, not omissions. Paired
statistics match arms on (scenario, trial, seed) within a group; an episode whose partner did
not run is counted as unpaired and left out of the paired numbers.
"""

from __future__ import annotations

import csv
import itertools
import json
from collections import Counter, defaultdict
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any

from dave_agent.evaluation.statistics import bootstrap_mean_ci, mcnemar_exact, mean, percentile, wilson

GROUP_KEYS = ("environment", "mode", "regime", "checkpoint", "scenario")
PAIR_KEYS = ("trial", "seed")


def _r(value: float | None, digits: int = 4) -> float | None:
    return None if value is None else round(value, digits)


def _group(records: Sequence[dict[str, Any]], confidence: float) -> dict[str, Any]:
    n = len(records)
    done = [r for r in records if r["completed"]]
    ci = wilson(len(done), n, confidence)
    frames_done = [r["frames_to_completion"] for r in done]
    ok_latency = [v for r in records for v in r["ok_latencies_ms"]]
    decision_latency = [v for r in records for v in r["decision_latencies_ms"]]
    cost_known = all(r["cost_usd"] is not None for r in records)
    cost_total = sum(r["cost_usd"] for r in records) if cost_known else None
    failures = sum(r["failures"] for r in records)
    usage: dict[str, Counter] = {"tactical": Counter(), "planner": Counter()}
    for r in records:
        for purpose, counts in r["tokens"].items():
            usage[purpose].update(counts)
    return {
        "n": n,
        "completions": len(done),
        "completion_rate": _r(len(done) / n) if n else None,
        "completion_ci": None if ci is None else [_r(ci[0]), _r(ci[1])],
        "deaths_mean": _r(mean([r["deaths"] for r in records])),
        "deaths_total": sum(r["deaths"] for r in records),
        "frames_to_completion_median": percentile(frames_done, 50),
        "frames_to_completion_mean": _r(mean(frames_done), 1),
        "censored": n - len(done),
        "frames_mean": _r(mean([r["frames"] for r in records]), 1),
        "wall_seconds_mean": _r(mean([r["wall_seconds"] for r in records]), 3),
        "wall_seconds_total": _r(sum(r["wall_seconds"] for r in records), 3),
        "model_wait_seconds_mean": _r(mean([r["model_wait_seconds"] for r in records]), 3),
        "tactical_ok_latency_ms": {"n": len(ok_latency), "p50": _r(percentile(ok_latency, 50), 1),
                                   "p95": _r(percentile(ok_latency, 95), 1)},
        "decision_latency_ms": {"n": len(decision_latency), "p50": _r(percentile(decision_latency, 50), 1),
                                "p95": _r(percentile(decision_latency, 95), 1)},
        # Cost only when every call's cost is known (reported, estimated or offline); else None.
        "cost_usd_total": _r(cost_total, 6),
        "cost_usd_per_attempt": _r(cost_total / n, 6) if cost_known and n else None,
        "cost_usd_per_success": _r(cost_total / len(done), 6) if cost_known and done else None,
        "cost_reported_usd": _r(sum(r["cost_reported_usd"] for r in records), 6),
        "cost_estimated_usd": _r(sum(r["cost_estimated_usd"] for r in records), 6),
        "cost_unknown_calls": sum(r["cost_unknown_calls"] for r in records),
        "tokens": {purpose: dict(sorted(c.items())) for purpose, c in usage.items()},
        "tactical_calls": sum(r["tactical_calls"] for r in records),
        "planner_calls": sum(r["planner_calls"] for r in records),
        "decisions": sum(r["decisions"] for r in records),
        "fallback_decisions": sum(r["fallback_decisions"] for r in records),
        "invalid_outputs": sum(r["invalid_outputs"] for r in records),
        "transport_retries": sum(r["transport_retries"] for r in records),
        "tactical_reasks": sum(r["tactical_reasks"] for r in records),
        "planner_fallbacks": sum(r["planner_fallbacks"] for r in records),
        "route_replans": sum(r["route_replans"] for r in records),
        "skill_executions": sum(r["skill_executions"] for r in records),
        "failures": failures,
        "repeated_failure_rate": _r(sum(r["repeated_failures"] for r in records) / failures) if failures else None,
        "outcomes": dict(sorted(Counter(r["outcome"] for r in records).items())),
        "termination_reasons": dict(sorted(Counter(r["termination_reason"] for r in records).items())),
        "errors": sum(r["outcome"] == "error" for r in records),
    }


def _pairs(first: str, second: str, a: dict, b: dict, confidence: float, samples: int, seed: int) -> dict:
    keys = sorted(a.keys() & b.keys())
    pairs = [(a[k], b[k]) for k in keys]
    comp = [int(x["completed"]) - int(y["completed"]) for x, y in pairs]
    deaths = [x["deaths"] - y["deaths"] for x, y in pairs]
    frames = [x["frames_to_completion"] - y["frames_to_completion"] for x, y in pairs
              if x["completed"] and y["completed"]]
    only_first = sum(x["completed"] and not y["completed"] for x, y in pairs)
    only_second = sum(y["completed"] and not x["completed"] for x, y in pairs)
    ci = bootstrap_mean_ci(comp, confidence, samples, seed)
    deaths_ci = bootstrap_mean_ci(deaths, confidence, samples, seed)
    return {
        "arms": [first, second],
        "n_pairs": len(pairs),
        "unpaired": len(a.keys() ^ b.keys()),
        "both_completed": sum(x["completed"] and y["completed"] for x, y in pairs),
        "only_first_completed": only_first,
        "only_second_completed": only_second,
        "neither_completed": sum(not x["completed"] and not y["completed"] for x, y in pairs),
        # first minus second; the CI resamples whole pairs.
        "completion_diff": _r(mean(comp)),
        "completion_diff_ci": None if ci is None else [_r(ci[0]), _r(ci[1])],
        "mcnemar_p": _r(mcnemar_exact(only_first, only_second)),
        "deaths_diff": _r(mean(deaths)),
        "deaths_diff_ci": None if deaths_ci is None else [_r(deaths_ci[0]), _r(deaths_ci[1])],
        "frames_diff_both_completed": _r(mean(frames), 1),
        "frames_diff_n": len(frames),
    }


def summarize(records: Iterable[dict[str, Any]], confidence: float = 0.95, bootstrap_samples: int = 2000,
              seed: int = 0) -> dict[str, Any]:
    by_group: dict[tuple, dict[str, list[dict]]] = defaultdict(lambda: defaultdict(list))
    for record in records:
        by_group[tuple(record.get(k) for k in GROUP_KEYS)][record["arm"]].append(record)
    groups, pairs = [], []
    for key in sorted(by_group, key=lambda k: json.dumps(k)):
        label = dict(zip(GROUP_KEYS, key, strict=True))
        arms = by_group[key]
        for arm in sorted(arms):
            groups.append({**label, "arm": arm, **_group(arms[arm], confidence)})
        indexed = {arm: {tuple(r[k] for k in PAIR_KEYS): r for r in rs} for arm, rs in arms.items()}
        for first, second in itertools.combinations(sorted(arms), 2):
            pairs.append({**label, **_pairs(first, second, indexed[first], indexed[second], confidence,
                                            bootstrap_samples, seed)})
    return {"unit": "episode", "confidence": confidence, "bootstrap_samples": bootstrap_samples,
            "bootstrap_seed": seed, "groups": groups, "pairs": pairs}


def _flat(row: dict[str, Any]) -> dict[str, Any]:
    out = {}
    for key, value in row.items():
        if isinstance(value, dict):
            for sub, inner in value.items():
                out[f"{key}.{sub}"] = json.dumps(inner, sort_keys=True) if isinstance(inner, (dict, list)) else inner
        elif isinstance(value, list):
            out[key] = json.dumps(value)
        else:
            out[key] = value
    return out


def write_csv(rows: Sequence[dict[str, Any]], path: Path) -> None:
    flat = [_flat(r) for r in rows]
    fields = list(dict.fromkeys(k for r in flat for k in r))
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        writer.writerows(flat)


def write_summaries(records: Sequence[dict[str, Any]], out_dir: Path, confidence: float, bootstrap_samples: int,
                    seed: int) -> dict[str, Any]:
    """summary.json, summary.csv (one row per group and arm) and pairs.csv in ``out_dir``."""
    summary = summarize(records, confidence, bootstrap_samples, seed)
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    write_csv(summary["groups"], out_dir / "summary.csv")
    write_csv(summary["pairs"], out_dir / "pairs.csv")
    return summary
