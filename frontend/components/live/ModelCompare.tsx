"use client";
import { useState } from "react";
import { emptyModels, sumModels, type LiveState, type ModelKind, type ModelTallies, type ModelTally } from "@/lib/live";

const KINDS: ModelKind[] = ["llm", "jev"];
const NAME: Record<ModelKind, string> = { llm: "LLM", jev: "Jev" };
const WHO: Record<ModelKind, string> = { llm: "Azure OpenAI", jev: "OpenRouter" };

const tokens = (m: ModelTally) => m.inputTokens + m.outputTokens + m.otherTokens;

function compact(n: number): string {
  if (n >= 1e6) return `${(n / 1e6).toFixed(n >= 1e7 ? 1 : 2)}M`;
  if (n >= 1e4) return `${(n / 1e3).toFixed(n >= 1e5 ? 0 : 1)}k`;
  return Math.round(n).toLocaleString();
}

function usd(v: number): string {
  if (v === 0) return "$0";
  if (v < 0.01) return `$${v.toFixed(5)}`;
  if (v < 1) return `$${v.toFixed(4)}`;
  return `$${v.toFixed(2)}`;
}

const pct = (v: number, total: number) => (total ? `${Math.round((100 * v) / total)}%` : "—");

type Tip = { text: string; x: number; y: number } | null;

/** A hover (and keyboard focus) tooltip shared by every mark in the section. */
function useTip() {
  const [tip, setTip] = useState<Tip>(null);
  const bind = (text: string) => ({
    tabIndex: 0,
    "aria-label": text,
    onMouseMove: (e: React.MouseEvent) => {
      const box = (e.currentTarget as Element).closest(".mc")!.getBoundingClientRect();
      setTip({ text, x: e.clientX - box.left, y: e.clientY - box.top });
    },
    onFocus: (e: React.FocusEvent) => {
      const box = (e.currentTarget as Element).closest(".mc")!.getBoundingClientRect();
      const r = (e.currentTarget as Element).getBoundingClientRect();
      setTip({ text, x: r.left - box.left + r.width / 2, y: r.top - box.top });
    },
    onMouseLeave: () => setTip(null),
    onBlur: () => setTip(null),
  });
  return [tip, bind] as const;
}

type Bind = ReturnType<typeof useTip>[1];
type Part = { value: number; light?: boolean; tip: string };

/** One bar chart: a row per model on a shared scale, the value labelled at the bar's end. */
function Bars({ title, sub, rows, format, bind, foot }: {
  title: string;
  sub: string;
  foot?: string;
  rows: { kind: ModelKind; parts: Part[]; note?: string }[];
  format: (v: number) => string;
  bind: Bind;
}) {
  const max = Math.max(...rows.map((r) => r.parts.reduce((n, p) => n + p.value, 0)), 0);
  return (
    <figure className="mc-chart">
      <figcaption>
        <b>{title}</b> <span className="muted small">{sub}</span>
      </figcaption>
      {rows.map(({ kind, parts, note }) => {
        const total = parts.reduce((n, p) => n + p.value, 0);
        return (
          <div key={kind} className="mc-row">
            <span className="mc-label">{NAME[kind]}</span>
            <span className="mc-track">
              <span className="mc-stack" style={{ width: `${max ? (100 * total) / max : 0}%` }}>
                {parts
                  .filter((p) => p.value > 0)
                  .map((p, i) => (
                    <span key={i} className={`mc-bar ${kind}${p.light ? " light" : ""}`}
                          style={{ flexGrow: p.value }} {...bind(p.tip)} />
                  ))}
              </span>
            </span>
            <span className="mc-value">
              {total ? format(total) : "none"}
              {note ? <span className="muted small"> {note}</span> : null}
            </span>
          </div>
        );
      })}
      {foot ? <div className="mc-foot muted small">{foot}</div> : null}
    </figure>
  );
}

const R = 34;
const C = 2 * Math.PI * R;
const GAP = 2;

/** One donut: each model's share of the total, the total in the middle. */
function Donut({ title, values, format, bind }: {
  title: string;
  values: Record<ModelKind, number>;
  format: (v: number) => string;
  bind: Bind;
}) {
  const total = values.llm + values.jev;
  const shown = KINDS.filter((k) => values[k] > 0);
  const lens = shown.map((k) => (C * values[k]) / total);
  const starts = lens.map((_, i) => lens.slice(0, i).reduce((a, b) => a + b, 0));
  return (
    <figure className="mc-chart mc-donut">
      <figcaption>
        <b>{title}</b>
      </figcaption>
      <svg viewBox="0 0 88 88" width="120" height="120" role="img"
           aria-label={`${title}: ${KINDS.map((k) => `${NAME[k]} ${pct(values[k], total)}`).join(", ")}`}>
        <circle cx="44" cy="44" r={R} className="mc-ring" />
        {shown.map((k, i) => (
          <circle key={k} cx="44" cy="44" r={R} className={`mc-arc ${k}`}
                  strokeDasharray={`${Math.max(lens[i] - (shown.length > 1 ? GAP : 0), 0.5)} ${C}`}
                  strokeDashoffset={-starts[i]} transform="rotate(-90 44 44)"
                  {...bind(`${NAME[k]}: ${format(values[k])} of ${format(total)} (${pct(values[k], total)})`)} />
        ))}
        <text x="44" y="42" className="mc-total">{total ? format(total) : "—"}</text>
        <text x="44" y="54" className="mc-total-sub">total</text>
      </svg>
      <ul className="mc-legend">
        {KINDS.map((k) => (
          <li key={k}>
            <span className={`mc-key ${k}`} /> {NAME[k]} <b>{pct(values[k], total)}</b>
          </li>
        ))}
      </ul>
    </figure>
  );
}

/** LLM against Jev: calls, tokens (input and output) and cost, as bars on a shared scale and as
 * each model's share. Shown for this run, or summed over every run since the page opened. */
export function ModelCompare({ live }: { live: LiveState }) {
  const [scope, setScope] = useState<"run" | "all">("run");
  const [tip, bind] = useTip();
  const runs = Object.values(live.history);
  const m: ModelTallies = scope === "run" ? live.stats.models ?? emptyModels() : sumModels(runs.map((r) => r.models));
  const any = m.llm.calls + m.jev.calls > 0;
  const unknown = (k: ModelKind) => (m[k].costUnknown ? `${m[k].costUnknown} calls with no cost` : undefined);
  const noCost = KINDS.filter((k) => m[k].costUnknown).map((k) => `${NAME[k]}: ${unknown(k)} (not in the total)`).join(" · ");

  return (
    <div className="mc">
      <div className="mc-head">
        <h3>LLM vs Jev</h3>
        <span className="mc-legend inline">
          {KINDS.map((k) => (
            <span key={k}>
              <span className={`mc-key ${k}`} /> {NAME[k]} <span className="muted">({WHO[k]})</span>
            </span>
          ))}
        </span>
        <span className="mc-scope" role="group" aria-label="Scope: this run, or every run since the page opened">
          <button className={scope === "run" ? "on" : ""} onClick={() => setScope("run")}>This run</button>
          <button className={scope === "all" ? "on" : ""} onClick={() => setScope("all")}>
            All runs ({runs.length})
          </button>
        </span>
      </div>
      {!any ? (
        <p className="muted small">No LLM or Jev calls yet{scope === "run" ? " in this run" : ""} (mock and rule calls are not counted).</p>
      ) : (
        <>
          <div className="mc-grid">
            <Bars title="Calls" sub="planner + tactical" format={compact} bind={bind}
                  rows={KINDS.map((k) => ({
                    kind: k,
                    note: m[k].failed ? `(${m[k].failed} failed)` : undefined,
                    parts: [{ value: m[k].calls, tip: `${NAME[k]}: ${m[k].calls} calls · ${m[k].planner} planner, ${m[k].tactical} tactical${m[k].failed ? ` · ${m[k].failed} failed` : ""}` }],
                  }))} />
            <Bars title="Tokens" sub="solid = output, light = input" format={compact} bind={bind}
                  rows={KINDS.map((k) => ({
                    kind: k,
                    parts: [
                      { value: m[k].outputTokens, tip: `${NAME[k]} output: ${m[k].outputTokens.toLocaleString()} tokens${k === "llm" ? " (reasoning included)" : ""}` },
                      { value: m[k].inputTokens, light: true, tip: `${NAME[k]} input: ${m[k].inputTokens.toLocaleString()} tokens` },
                      { value: m[k].otherTokens, light: true, tip: `${NAME[k]}: ${m[k].otherTokens.toLocaleString()} tokens not split into input and output` },
                    ],
                  }))} />
            <Bars title="Cost (USD)" sub="LLM estimated from the price table · Jev as reported" format={usd} bind={bind}
                  foot={noCost || undefined}
                  rows={KINDS.map((k) => ({
                    kind: k,
                    parts: [{ value: m[k].costUsd, tip: `${NAME[k]}: ${usd(m[k].costUsd)}${k === "llm" ? " estimated" : " reported"}${m[k].calls ? ` · ${usd(m[k].costUsd / m[k].calls)} a call` : ""}${unknown(k) ? ` · ${unknown(k)}` : ""}` }],
                  }))} />
          </div>
          <div className="mc-grid donuts">
            <Donut title="Share of calls" values={{ llm: m.llm.calls, jev: m.jev.calls }} format={compact} bind={bind} />
            <Donut title="Share of tokens" values={{ llm: tokens(m.llm), jev: tokens(m.jev) }} format={compact} bind={bind} />
            <Donut title="Share of cost" values={{ llm: m.llm.costUsd, jev: m.jev.costUsd }} format={usd} bind={bind} />
          </div>
        </>
      )}
      {tip ? (
        <div className="mc-tip" style={{ left: tip.x, top: tip.y }} role="status">
          {tip.text}
        </div>
      ) : null}
    </div>
  );
}
