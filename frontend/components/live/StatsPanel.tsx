"use client";
import { tacticalName, type CallTally, type LiveState } from "@/lib/live";
import { ModelCompare } from "./ModelCompare";

type Segment = { label: string; value: number; tone: string };

/** A labelled stacked bar: every segment is named with its count in the legend, so the colour
 * never carries the meaning alone. */
function StackBar({ segments, label }: { segments: Segment[]; label: string }) {
  const total = segments.reduce((n, s) => n + s.value, 0);
  return (
    <div className="stack">
      <div className="stack-bar" role="img" aria-label={`${label}: ${segments.map((s) => `${s.label} ${s.value}`).join(", ")}`}>
        {total === 0 ? <span className="stack-empty" /> : null}
        {segments
          .filter((s) => s.value > 0)
          .map((s) => (
            <span key={s.label} className={`stack-seg ${s.tone}`} style={{ flexGrow: s.value }}
                  title={`${s.label}: ${s.value} (${Math.round((100 * s.value) / total)}%)`} />
          ))}
      </div>
      <ul className="stack-legend">
        {segments.map((s) => (
          <li key={s.label}>
            <span className={`stack-key ${s.tone}`} /> {s.label} <b>{s.value}</b>
            {total ? <span className="muted"> {Math.round((100 * s.value) / total)}%</span> : null}
          </li>
        ))}
      </ul>
    </div>
  );
}

const mean = (xs: number[]) => (xs.length ? xs.reduce((a, b) => a + b, 0) / xs.length : null);

function median(xs: number[]): number | null {
  if (!xs.length) return null;
  const s = [...xs].sort((a, b) => a - b);
  const m = Math.floor(s.length / 2);
  return s.length % 2 ? s[m] : (s[m - 1] + s[m]) / 2;
}

const ms = (v: number | null) => (v === null ? "—" : v >= 1000 ? `${(v / 1000).toFixed(2)} s` : `${Math.round(v)} ms`);

/** Cost as reported or estimated; calls with no cost are counted, never shown as $0. */
function cost(t: CallTally): string {
  if (!t.calls) return "—";
  const known = t.calls - t.costUnknown;
  if (!known || (t.costUsd === 0 && t.costUnknown)) return `unknown (${t.costUnknown} calls)`;
  if (t.costUsd === 0) return "$0";
  const usd = t.costUsd < 0.01 ? `$${t.costUsd.toFixed(5)}` : `$${t.costUsd.toFixed(3)}`;
  return t.costUnknown ? `${usd} + ${t.costUnknown} calls unknown` : usd;
}

function StatTile({ title, value, sub, children }: { title: string; value: string | number; sub?: string; children?: React.ReactNode }) {
  return (
    <div className="stat-tile">
      <div className="stat-title">{title}</div>
      <div className="stat-value">{value}</div>
      {sub ? <div className="stat-sub muted small">{sub}</div> : null}
      {children}
    </div>
  );
}

/** This run's tally: who made each tactical decision (the model, forced with no call, or the
 * deterministic fallback), the planner's calls, latency, tokens and cost, and what the skills did. */
export function StatsPanel({ live }: { live: LiveState }) {
  const { tactical: t, planner: p, outcomes, skills } = live.stats;
  const who = tacticalName(live.run);
  const plannerWho = live.run?.planner.startsWith("mock") ? "Rule planner (mock)" : "LLM planner";
  const decisions = t.model + t.forced + t.fallback;
  if (!live.run) {
    return (
      <section className="panel stats">
        <h2>Run stats</h2>
        <p className="muted">Decision and model-call counts appear here once a run starts.</p>
      </section>
    );
  }
  const topSkills = Object.entries(skills).sort((a, b) => b[1] - a[1]).slice(0, 6);
  const meanProb = mean(t.chosenProb);
  return (
    <section className="panel stats">
      <h2>
        Run stats <span className="muted small">{live.run.run_id} · {decisions} decisions · {p.plans} plans</span>
      </h2>
      <div className="stat-row">
        <StatTile title={`Tactical decisions · ${who}`} value={decisions}
              sub={meanProb === null ? undefined : `mean probability of ${who}'s choice: ${meanProb.toFixed(2)}`}>
          <StackBar label="Tactical decisions" segments={[
            { label: `by ${who}`, value: t.model, tone: "seg-model" },
            { label: "forced (one legal skill, no call)", value: t.forced, tone: "seg-forced" },
            { label: "fallback", value: t.fallback, tone: "seg-fallback" },
          ]} />
          {t.fallback ? (
            <div className="small muted">
              fallback reasons:{" "}
              {Object.entries(t.fallbackReasons).map(([r, n]) => (
                <span key={r} className="chip">
                  {r} ×{n}
                </span>
              ))}
            </div>
          ) : null}
        </StatTile>
        <StatTile title={plannerWho} value={p.plans} sub={`${p.calls} calls · ${p.failed} failed · ${p.fallback} fallback goals`}>
          <div className="small">
            {Object.entries(p.triggers)
              .sort((a, b) => b[1] - a[1])
              .map(([trigger, n]) => (
                <span key={trigger} className="chip">
                  {trigger} ×{n}
                </span>
              ))}
          </div>
        </StatTile>
        <StatTile title="Latency per call" value={ms(median(t.latency))} sub="median, tactical">
          <dl className="facts small">
            <div className="fact-row">
              <dt>{who}</dt>
              <dd>mean {ms(mean(t.latency))} · max {ms(t.latency.length ? Math.max(...t.latency) : null)} · {t.calls} calls{t.failed ? `, ${t.failed} failed` : ""}</dd>
            </div>
            <div className="fact-row">
              <dt>Planner</dt>
              <dd>mean {ms(mean(p.latency))} · median {ms(median(p.latency))}</dd>
            </div>
          </dl>
        </StatTile>
        <StatTile title="Tokens and cost" value={(t.tokens + p.tokens).toLocaleString()} sub="tokens, both roles">
          <dl className="facts small">
            <div className="fact-row">
              <dt>{who}</dt>
              <dd>{t.tokens.toLocaleString()} tokens · {cost(t)}</dd>
            </div>
            <div className="fact-row">
              <dt>Planner</dt>
              <dd>{p.tokens.toLocaleString()} tokens · {cost(p)}</dd>
            </div>
          </dl>
        </StatTile>
        <StatTile title="Skill outcomes" value={Object.values(outcomes).reduce((a, b) => a + b, 0)} sub={`${live.deaths} deaths`}>
          <StackBar label="Skill outcomes" segments={[
            { label: "completed", value: outcomes.completed ?? 0, tone: "seg-ok" },
            { label: "interrupted", value: outcomes.interrupted ?? 0, tone: "seg-warn" },
            { label: "failed", value: outcomes.failed ?? 0, tone: "seg-danger" },
          ]} />
          <div className="small">
            {topSkills.map(([skill, n]) => (
              <span key={skill} className="chip mono">
                {skill} ×{n}
              </span>
            ))}
          </div>
        </StatTile>
      </div>
      <ModelCompare live={live} />
    </section>
  );
}
