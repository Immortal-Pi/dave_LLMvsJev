"""Read-only: the numbers and charts for docs/article.md, from a live episode store.

    uv run --with matplotlib python scripts/article_figures.py --store artifacts/live.sqlite --out docs/images

Setups are taken from each run's recorded settings:
- hybrid: Jev tactical + Azure LLM planner;
- jev_rule: Jev tactical + the hand-written rule planner (no LLM anywhere);
- llm_only: Azure LLM tactical + Azure LLM planner (arm A).
Runs with a mock tactical model are left out. matplotlib is not a project dependency, hence --with.
Writes numbers.json (every figure the article quotes) and the PNG charts.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import statistics
from collections import defaultdict
from pathlib import Path

# One hybrid run per level for the article's case studies (both models priced).
CASE_RUNS = {
    "level1": "live-20261008T014020-24a001",
    "level2": "live-20261008T204413-99ed80",
    "level3": "live-20261008T214808-1ac49a",
    "level4": "live-20261008T215038-4b17d1",
}
# Level 2 with the LLM asked for every goal (llm_calls: always), for the escalate contrast.
CONTRAST_RUN = "live-20261008T014051-836fa4"
# configs/models.yaml models.tactical_llm.price (USD per million tokens), used to estimate
# arm A's unpriced tactical calls.
PRICE_IN, PRICE_OUT = 2.0, 10.0
LEVELS = [f"level{i}" for i in range(1, 9)]

# Reference palette, light mode (dataviz skill).
SURFACE, INK, INK_2, GRID = "#fcfcfb", "#0b0b0b", "#52514e", "#e4e3df"
JEV, LLM, THIRD, FOURTH = "#2a78d6", "#eb6834", "#1baf7a", "#eda100"


def setup_of(config: dict) -> str | None:
    settings = config.get("effective_settings") or {}
    planner = (settings.get("planner") or {}).get("provider")
    tactical = (settings.get("tactical") or {}).get("provider")
    if tactical == "openrouter_jev":
        return "hybrid" if planner == "azure_openai" else "jev_rule"
    if tactical == "azure_openai":
        return "llm_only"
    return None


def load(store: Path) -> tuple[dict, list[dict]]:
    conn = sqlite3.connect(f"file:{store.as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    runs = {}
    for row in conn.execute("SELECT run_id, config_json FROM runs"):
        config = json.loads(row["config_json"])
        runs[row["run_id"]] = {
            "setup": setup_of(config),
            "llm_calls": (config.get("planning") or {}).get("llm_calls") or "always",
        }
    episodes = {}
    for row in conn.execute("SELECT * FROM episodes"):
        ep = dict(row)
        ep.update(runs[ep["run_id"]])
        ep["calls"] = []
        episodes[ep["episode_key"]] = ep
    for row in conn.execute(
        "SELECT episode_key, provider, model, purpose, status, latency_ms, usage_json, cost_usd FROM model_calls"
    ):
        call = dict(row)
        call["usage"] = json.loads(call.pop("usage_json") or "{}")
        episodes[call["episode_key"]]["calls"].append(call)
    conn.close()
    return runs, [ep for ep in episodes.values() if ep["setup"]]


def kind(call: dict) -> str | None:
    if call["provider"] == "openrouter_jev":
        return "jev"
    if call["provider"] == "azure_openai":
        return "llm_tactical" if call["purpose"] == "tactical" else "llm_planner"
    return None


def estimated_cost(call: dict) -> float:
    usage = call["usage"]
    return (usage.get("prompt_tokens", 0) * PRICE_IN + usage.get("completion_tokens", 0) * PRICE_OUT) / 1e6


def summarize_calls(episodes: list[dict]) -> dict:
    by_kind: dict[str, list[dict]] = defaultdict(list)
    for ep in episodes:
        for call in ep["calls"]:
            if kind(call):
                by_kind[kind(call)].append(call)
    out = {}
    for name, calls in by_kind.items():
        lat = sorted(c["latency_ms"] for c in calls if c["latency_ms"] is not None)
        priced = [c["cost_usd"] for c in calls if c["cost_usd"] is not None]
        est = [estimated_cost(c) for c in calls if c["provider"] == "azure_openai"]
        out[name] = {
            "calls": len(calls),
            "latency_ms_mean": round(statistics.mean(lat)),
            "latency_ms_p50": round(statistics.median(lat)),
            "latency_ms_p90": round(lat[int(0.9 * (len(lat) - 1))]),
            "cost_usd_reported_total": round(sum(priced), 4) if priced else None,
            "cost_usd_per_call_reported": round(sum(priced) / len(priced), 6) if priced else None,
            "priced_calls": len(priced),
            "cost_usd_per_call_estimated": round(statistics.mean(est), 6) if est else None,
            "models": sorted({c["model"] for c in calls}),
        }
    return out, by_kind


def episode_stats(ep: dict) -> dict:
    s = {"jev_calls": 0, "jev_cost": 0.0, "jev_ms": 0.0, "llm_calls": 0, "llm_cost": 0.0, "llm_ms": 0.0,
         "llm_tactical_calls": 0}
    for call in ep["calls"]:
        k = kind(call)
        if k == "jev":
            s["jev_calls"] += 1
            s["jev_cost"] += call["cost_usd"] or 0.0
            s["jev_ms"] += call["latency_ms"] or 0.0
        elif k == "llm_planner":
            s["llm_calls"] += 1
            s["llm_cost"] += call["cost_usd"] if call["cost_usd"] is not None else estimated_cost(call)
            s["llm_ms"] += call["latency_ms"] or 0.0
        elif k == "llm_tactical":
            s["llm_tactical_calls"] += 1
    return s


def outcome_class(ep: dict) -> str:
    if ep["outcome"] == "level_complete":
        return "completed"
    if ep["outcome"] == "game_over":
        return "game over"
    return "stopped"  # stopped by hand, or a frame or token cap


def style(ax) -> None:
    ax.set_facecolor(SURFACE)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color(GRID)
    ax.tick_params(colors=INK_2, labelsize=9)
    ax.yaxis.label.set_color(INK_2)
    ax.xaxis.label.set_color(INK_2)
    ax.title.set_color(INK)


def figure(plt, w: float, h: float, ncols: int = 1):
    fig, axes = plt.subplots(1, ncols, figsize=(w, h), dpi=160)
    fig.patch.set_facecolor(SURFACE)
    return fig, axes


def chart_latency(plt, by_kind: dict, out: Path) -> None:
    order = [("jev", "Jev (System One)\ntactical move"), ("llm_tactical", "LLM\ntactical move"),
             ("llm_planner", "LLM\nstrategic plan")]
    fig, ax = figure(plt, 8, 3.4)
    data = [[c["latency_ms"] / 1000 for c in by_kind[k] if c["latency_ms"]] for k, _ in order]
    box = ax.boxplot(data, orientation="horizontal", widths=0.5, showfliers=False, patch_artist=True,
                     medianprops={"color": INK, "linewidth": 2})
    for patch, color in zip(box["boxes"], (JEV, LLM, LLM)):
        patch.set_facecolor(color)
        patch.set_edgecolor(SURFACE)
        patch.set_linewidth(2)
    for part in ("whiskers", "caps"):
        for line in box[part]:
            line.set_color(INK_2)
    ax.set_xscale("log")
    ax.set_xticks([0.1, 0.2, 0.5, 1, 2, 5, 10])
    ax.set_xticklabels(["0.1 s", "0.2 s", "0.5 s", "1 s", "2 s", "5 s", "10 s"])
    ax.set_yticks([1, 2, 3])
    ax.set_yticklabels([label for _, label in order], color=INK)
    for i, d in enumerate(data, start=1):
        med = statistics.median(d)
        ax.text(med, i + 0.36, f"median {med:.2f} s  (n={len(d):,})", ha="center", fontsize=8.5, color=INK_2)
    ax.grid(axis="x", color=GRID, linewidth=0.8)
    ax.set_axisbelow(True)
    ax.set_title("Time per model call (box: middle half of calls; whiskers: 1.5 IQR)", fontsize=11, loc="left")
    style(ax)
    fig.tight_layout()
    fig.savefig(out / "latency.png", facecolor=SURFACE)
    plt.close(fig)


def chart_cost_per_call(plt, calls: dict, out: Path) -> None:
    rows = [
        ("Jev tactical move", calls["jev"]["cost_usd_per_call_reported"], JEV, ""),
        ("LLM strategic plan", calls["llm_planner"]["cost_usd_per_call_reported"], LLM, ""),
        ("LLM tactical move (arm A)", calls["llm_tactical"]["cost_usd_per_call_estimated"], LLM, ""),
    ]
    fig, ax = figure(plt, 8, 2.8)
    y = range(len(rows))
    bars = ax.barh(list(y), [r[1] * 1000 for r in rows], color=[r[2] for r in rows], height=0.55,
                   edgecolor=SURFACE, linewidth=2)
    for bar, r in zip(bars, rows):
        bar.set_hatch(r[3])
        ax.text(bar.get_width(), bar.get_y() + bar.get_height() / 2, f"  ${r[1]:.5f}", va="center",
                fontsize=9, color=INK)
    ax.set_yticks(list(y))
    ax.set_yticklabels([r[0] for r in rows], color=INK)
    ax.invert_yaxis()
    ax.set_xlabel("USD per 1,000 calls")
    ax.grid(axis="x", color=GRID, linewidth=0.8)
    ax.set_axisbelow(True)
    ax.set_title("Cost per model call", fontsize=11, loc="left")
    fig.text(0.01, 0.02, "LLM: estimated from recorded tokens at the configs/models.yaml price (Azure reports no "
             "cost). Jev: reported by OpenRouter.",
             fontsize=7.5, color=INK_2)
    style(ax)
    fig.tight_layout(rect=(0, 0.06, 1, 1))
    fig.savefig(out / "cost-per-call.png", facecolor=SURFACE)
    plt.close(fig)


def chart_calls_per_level(plt, per_level: dict, out: Path) -> None:
    levels = [lv for lv in LEVELS if lv in per_level]
    fig, ax = figure(plt, 8, 3.6)
    x = list(range(len(levels)))
    w = 0.38
    jev = [per_level[lv]["jev_calls_median"] for lv in levels]
    llm = [per_level[lv]["llm_calls_median"] for lv in levels]
    b1 = ax.bar([i - w / 2 - 0.01 for i in x], jev, w, color=JEV, label="Jev calls (tactical)")
    b2 = ax.bar([i + w / 2 + 0.01 for i in x], llm, w, color=LLM, label="LLM calls (strategic)")
    for bars in (b1, b2):
        for bar in bars:
            ax.text(bar.get_x() + bar.get_width() / 2, bar.get_height(), f"{bar.get_height():.0f}",
                    ha="center", va="bottom", fontsize=8.5, color=INK)
    ax.set_xticks(x)
    ax.set_xticklabels([f"Level {lv[5:]}\n(n={per_level[lv]['episodes']})" for lv in levels], color=INK)
    ax.set_ylabel("calls per episode (median)")
    ax.grid(axis="y", color=GRID, linewidth=0.8)
    ax.set_axisbelow(True)
    ax.legend(frameon=False, fontsize=9, labelcolor=INK)
    ax.set_title("Hybrid runs: Jev and LLM calls per episode, by level", fontsize=11, loc="left")
    style(ax)
    fig.tight_layout()
    fig.savefig(out / "calls-per-level.png", facecolor=SURFACE)
    plt.close(fig)


def chart_case_studies(plt, cases: dict, out: Path) -> None:
    levels = list(cases)
    fig, (ax1, ax2) = figure(plt, 10, 3.9, ncols=2)
    x = list(range(len(levels)))
    w = 0.38
    for ax, (jk, lk, pk, unit, title) in (
        (ax1, ("jev_cost", "llm_cost", "llm_only_cost_projected", "USD", "Model cost for the run")),
        (ax2, ("jev_seconds", "llm_seconds", "llm_only_seconds_projected", "seconds",
               "Time spent waiting on models")),
    ):
        left = [i - w / 2 - 0.01 for i in x]
        right = [i + w / 2 + 0.01 for i in x]
        jev = [cases[lv][jk] for lv in levels]
        llm = [cases[lv][lk] for lv in levels]
        proj = [cases[lv][pk] for lv in levels]
        ax.bar(left, jev, w, color=JEV, label="Jev (hybrid)", edgecolor=SURFACE, linewidth=1)
        ax.bar(left, llm, w, bottom=jev, color=LLM, label="LLM planner (hybrid)", edgecolor=SURFACE,
               linewidth=1)
        ax.bar(right, proj, w, color=SURFACE, edgecolor=LLM, hatch="///", linewidth=1.2,
               label="LLM only (projected)")
        for xi, total in zip(left, [a + b for a, b in zip(jev, llm)]):
            ax.text(xi, total, f"{total:.2f}" if unit == "USD" else f"{total:.0f}", ha="center",
                    va="bottom", fontsize=8.5, color=INK)
        for xi, total in zip(right, proj):
            ax.text(xi, total, f"{total:.2f}" if unit == "USD" else f"{total:.0f}", ha="center",
                    va="bottom", fontsize=8.5, color=INK)
        ax.set_xticks(x)
        ax.set_xticklabels([f"Level {lv[5:]}" + ("" if cases[lv]["completed"] else "\n(not finished)")
                            for lv in levels], color=INK)
        ax.set_ylabel(unit)
        ax.grid(axis="y", color=GRID, linewidth=0.8)
        ax.set_axisbelow(True)
        ax.set_title(title, fontsize=11, loc="left")
        style(ax)
    ax1.legend(frameon=False, fontsize=8.5, labelcolor=INK, loc="upper left")
    fig.text(0.01, 0.01, "One recorded hybrid run per level. LLM only = the same number of moves, each at the LLM's "
             "mean tactical latency and estimated cost.", fontsize=8, color=INK_2)
    fig.tight_layout(rect=(0, 0.04, 1, 1))
    fig.savefig(out / "cost-per-level.png", facecolor=SURFACE)
    plt.close(fig)


def chart_outcomes(plt, outcomes: dict, out: Path) -> None:
    setups = [("jev_rule", "Jev + rule planner\n(no LLM)"), ("llm_only", "LLM only"),
              ("hybrid", "Jev + LLM planner")]
    levels = LEVELS[:5]
    classes = [("completed", THIRD), ("game over", FOURTH), ("stopped", "#c3c2b7")]
    fig, axes = figure(plt, 10, 3.2, ncols=3)
    top = max(sum(outcomes[s].get(lv, {}).values()) for s, _ in setups for lv in levels)
    for ax, (setup, title) in zip(axes, setups):
        bottom = [0] * len(levels)
        for cls, color in classes:
            vals = [outcomes[setup].get(lv, {}).get(cls, 0) for lv in levels]
            ax.bar(range(len(levels)), vals, 0.6, bottom=bottom, color=color, label=cls,
                   edgecolor=SURFACE, linewidth=1.5)
            bottom = [b + v for b, v in zip(bottom, vals)]
        ax.set_xticks(range(len(levels)))
        ax.set_xticklabels([lv[5:] for lv in levels], color=INK)
        ax.set_xlabel("level")
        ax.set_ylim(0, top + 1)
        ax.grid(axis="y", color=GRID, linewidth=0.8)
        ax.set_axisbelow(True)
        ax.set_title(title, fontsize=10.5, loc="left")
        style(ax)
    axes[0].set_ylabel("episodes")
    handles, labels = axes[0].get_legend_handles_labels()
    fig.legend(handles, labels, frameon=False, fontsize=8.5, labelcolor=INK, loc="upper right", ncols=3)
    fig.text(0.01, 0.01, "Live runs on levels 1-5. 'Stopped' = ended by hand or by a frame or token cap; "
             "runs under 5 decisions left out.", fontsize=8, color=INK_2)
    fig.tight_layout(rect=(0, 0.05, 1, 0.93))
    fig.savefig(out / "outcomes.png", facecolor=SURFACE)
    plt.close(fig)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--store", type=Path, default=Path("artifacts/live.sqlite"))
    ap.add_argument("--out", type=Path, default=Path("docs/images"))
    args = ap.parse_args()
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    _, episodes = load(args.store)
    episodes = [ep for ep in episodes if (ep["decisions"] or 0) >= 5]
    calls, by_kind = summarize_calls(episodes)
    llm_tac_ms = calls["llm_tactical"]["latency_ms_mean"]
    llm_tac_cost = calls["llm_tactical"]["cost_usd_per_call_estimated"]

    # Calls per level across hybrid episodes.
    per_level = {}
    for lv in LEVELS:
        eps = [ep for ep in episodes if ep["setup"] == "hybrid" and ep["scenario_id"] == lv]
        if not eps:
            continue
        stats = [episode_stats(ep) for ep in eps]
        per_level[lv] = {
            "episodes": len(eps),
            "completed": sum(ep["outcome"] == "level_complete" for ep in eps),
            "jev_calls_median": statistics.median(s["jev_calls"] for s in stats),
            "llm_calls_median": statistics.median(s["llm_calls"] for s in stats),
            "jev_calls_total": sum(s["jev_calls"] for s in stats),
            "llm_calls_total": sum(s["llm_calls"] for s in stats),
        }

    # Outcomes by setup and level.
    outcomes: dict = defaultdict(lambda: defaultdict(lambda: defaultdict(int)))
    deaths: dict = defaultdict(lambda: defaultdict(int))
    for ep in episodes:
        outcomes[ep["setup"]][ep["scenario_id"]][outcome_class(ep)] += 1
        deaths[ep["setup"]][ep["scenario_id"]] += ep["deaths"] or 0

    # Per-setup run lists (the article quotes some of them).
    run_lists = {
        setup: [
            {"run_id": ep["run_id"], "level": ep["scenario_id"], "outcome": ep["outcome"],
             "reason": ep["termination_reason"], "frames": ep["frames"], "deaths": ep["deaths"],
             "decisions": ep["decisions"], **{k: round(v, 4) for k, v in episode_stats(ep).items()}}
            for ep in sorted(episodes, key=lambda e: e["started_at"]) if ep["setup"] == setup
        ]
        for setup in ("jev_rule", "llm_only")
    }

    # Case studies.
    by_run = {ep["run_id"]: ep for ep in episodes}
    cases = {}
    for lv, run_id in {**CASE_RUNS, "level2_always": CONTRAST_RUN}.items():
        ep = by_run[run_id]
        s = episode_stats(ep)
        cases[lv] = {
            "run_id": run_id,
            "llm_calls_mode": ep["llm_calls"],
            "completed": ep["outcome"] == "level_complete",
            "outcome": ep["outcome"],
            "termination_reason": ep["termination_reason"],
            "frames": ep["frames"],
            "deaths": ep["deaths"],
            "decisions": ep["decisions"],
            "forced_decisions": ep["decisions"] - s["jev_calls"],
            "jev_calls": s["jev_calls"],
            "jev_cost": round(s["jev_cost"], 4),
            "jev_seconds": round(s["jev_ms"] / 1000, 1),
            "jev_cost_per_call": round(s["jev_cost"] / s["jev_calls"], 6),
            "llm_calls": s["llm_calls"],
            "llm_cost": round(s["llm_cost"], 4),
            "llm_seconds": round(s["llm_ms"] / 1000, 1),
            "llm_cost_per_call": round(s["llm_cost"] / s["llm_calls"], 5) if s["llm_calls"] else None,
            "total_cost": round(s["jev_cost"] + s["llm_cost"], 4),
            "total_seconds": round((s["jev_ms"] + s["llm_ms"]) / 1000, 1),
            "llm_share_of_cost": round(s["llm_cost"] / (s["jev_cost"] + s["llm_cost"]), 3),
            "jev_calls_per_llm_call": round(s["jev_calls"] / s["llm_calls"], 1) if s["llm_calls"] else None,
            # The same moves made by the LLM: its mean tactical latency and estimated cost, plus
            # the same planner calls.
            "llm_only_seconds_projected": round((s["jev_calls"] * llm_tac_ms + s["llm_ms"]) / 1000, 1),
            "llm_only_cost_projected": round(s["jev_calls"] * llm_tac_cost + s["llm_cost"], 4),
        }

    numbers = {
        "store": args.store.as_posix(),
        "episodes_used": len(episodes),
        "episodes_by_setup": {k: sum(ep["setup"] == k for ep in episodes) for k in ("hybrid", "jev_rule", "llm_only")},
        "calls": calls,
        "llm_tactical_price_assumed": {"input_per_mtok": PRICE_IN, "output_per_mtok": PRICE_OUT},
        "calls_per_level_hybrid": per_level,
        "outcomes": {s: {lv: dict(v) for lv, v in d.items()} for s, d in outcomes.items()},
        "deaths": {s: dict(d) for s, d in deaths.items()},
        "case_studies": cases,
        "runs": run_lists,
    }
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "numbers.json").write_text(json.dumps(numbers, indent=2), encoding="utf-8")

    chart_latency(plt, by_kind, args.out)
    chart_cost_per_call(plt, calls, args.out)
    chart_calls_per_level(plt, per_level, args.out)
    chart_case_studies(plt, {lv: cases[lv] for lv in CASE_RUNS}, args.out)
    chart_outcomes(plt, numbers["outcomes"], args.out)
    print(json.dumps({k: numbers[k] for k in ("episodes_by_setup", "calls")}, indent=2))


if __name__ == "__main__":
    main()
