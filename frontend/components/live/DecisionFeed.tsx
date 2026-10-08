"use client";
import { ProbabilityBar } from "@/components/ProbabilityBar";
import { fmtTile } from "@/lib/view";
import {
  screenReason,
  tacticalName,
  type DecisionEvent,
  type FeedItem,
  type LiveState,
  type MoveScore,
  type OutcomeEvent,
  type PlanEvent,
} from "@/lib/live";

function danger(description: string): string | null {
  const m = description.match(/^danger: ([^;]+)/);
  return m ? m[1] : null;
}

/** The goal manager's `route:` note: this option makes the next move of the planned route, or
 * walks to its take-off (control/goals.py ``_route_notes``). */
function route(description: string): string | null {
  const m = description.match(/(?:^|; )route: ([^;]+)/);
  return m ? m[1] : null;
}

/** The live graph's score: the best option from here, or how much worse than it (cost units). */
function Score({ s }: { s: MoveScore | null | undefined }) {
  if (!s) return null;
  const tried = s.attempts ? `ok ${Math.round(s.p_ok * 100)}% over ${s.attempts} tries${s.fatal ? `, ${s.fatal} fatal` : ""}` : "untried";
  if (s.q === null) {
    return <span className="badge" title={`no known way to the ${s.mode} after it; ${tried}`}>score ?</span>;
  }
  const title = `cost to the ${s.mode} after this option: ${s.q.toFixed(2)}; ${tried}`;
  return s.best ? (
    <span className="badge ok" title={title}>best</span>
  ) : (
    <span className="badge" title={title}>+{(s.regret ?? 0).toFixed(1)}</span>
  );
}

function Outcome({ o }: { o: OutcomeEvent | null }) {
  if (!o) return <span className="muted small">running…</span>;
  const died = o.events.includes("death") || o.reason === "hazard_contact";
  const picked = o.events.includes("item_collected");
  return (
    <span className={died ? "out bad" : o.outcome === "completed" ? "out ok" : "out"}>
      {died ? "burned" : o.outcome}
      {o.reason && !died ? <span className="muted"> ({o.reason})</span> : null} → {fmtTile(o.tile)}
      {picked ? <span className="badge ok">picked up</span> : null}
      <span className="muted small"> · {o.frames} frames</span>
    </span>
  );
}

function DecisionCard({ d, outcome, who }: { d: DecisionEvent; outcome: OutcomeEvent | null; who: string }) {
  const chosen = d.candidates.find((c) => c.id === d.chosen);
  const probs = d.probabilities;
  const options = [...d.candidates].sort((a, b) => (probs?.[b.id] ?? 0) - (probs?.[a.id] ?? 0));
  const latency = d.calls.reduce((sum, c) => sum + (c.latency_ms ?? 0), 0);
  const how = d.forced ? "had one option:" : d.fallback ? `fell back (${d.fallback_reason}) to` : "chose";
  const onRoute = d.candidates.filter((c) => route(c.description)).map((c) => c.id);
  const scored = d.candidates.some((c) => c.score && c.score.q !== null);
  const chosenScore = chosen?.score;
  return (
    <article className="card decision-card">
      <header>
        <strong>{d.forced ? "Engine" : who}</strong> {how} <strong className="mono">{chosen?.skill ?? d.chosen}</strong>
        {probs?.[d.chosen] !== undefined ? <span className="muted"> ({probs[d.chosen].toFixed(2)})</span> : null}
        <span className="muted small">
          {" "}
          · at {fmtTile(d.tile)} · frame {d.frame}
          {latency ? ` · ${Math.round(latency)} ms` : ""}
          {d.wait_ticks ? ` · game ran ${d.wait_ticks} ticks meanwhile` : ""}
        </span>
        {d.stale ? (
          <span className="badge warn" title="the choice came too late for the game; the model is asked again">
            too late ({d.stale}), deciding again
          </span>
        ) : null}
        {onRoute.length ? (
          onRoute.includes(d.chosen) ? (
            <span className="badge ok" title="the chosen skill makes the planned route's next move">on route</span>
          ) : (
            <span className="badge warn" title="a skill marked route: was offered but not chosen">off route</span>
          )
        ) : null}
        {scored && chosenScore && chosenScore.q !== null ? (
          chosenScore.best ? (
            <span className="badge ok" title="the live graph scores this the best option from here">graph's best</span>
          ) : (
            <span className="badge warn" title="the live graph scores another option better from here">
              +{(chosenScore.regret ?? 0).toFixed(1)} vs graph's best
            </span>
          )
        ) : null}
      </header>
      {d.goal ? (
        <div className="small muted">
          goal {d.goal.target} · heading to {fmtTile(d.goal.waypoint)}
        </div>
      ) : null}
      {!d.forced ? (
        <ul className="options">
          {options.map((c) => {
            const warn = danger(c.description);
            const step = route(c.description);
            return (
              <li key={c.id} className={c.id === d.chosen ? "chosen" : ""} title={c.description}>
                <span className="mono skill">{c.skill}</span>
                {probs ? <ProbabilityBar value={probs[c.id]} highlight={c.id === d.chosen} /> : null}
                {warn ? <span className="badge warn">{warn}</span> : null}
                {step ? <span className="badge route" title={step}>route</span> : null}
                <Score s={c.score} />
              </li>
            );
          })}
          {Object.entries(d.screened).map(([id, reason]) => (
            <li key={id} className="screened" title="removed by the threat screen before the model was asked">
              <span className="mono skill">{id.replace(/^c\d+_/, "")}</span>
              <span className="muted small">removed: {screenReason(reason)}</span>
            </li>
          ))}
        </ul>
      ) : null}
      {d.stale ? null : (
        <div>
          <Outcome o={outcome} />
        </div>
      )}
    </article>
  );
}

function PlanCard({ p }: { p: PlanEvent }) {
  const planner = p.calls[0] ? `${p.calls[0].provider === "mock" ? "Rule planner (mock)" : "LLM planner"}` : "Planner";
  return (
    <article className="card plan-card">
      <header>
        <strong>{p.fallback ? "Fallback" : planner}</strong> set goal <strong className="mono">{p.chosen}</strong>
        <span className="muted small"> · frame {p.frame}{p.model_ms ? ` · ${Math.round(p.model_ms)} ms` : ""}</span>
      </header>
      <div className="small">
        {p.triggers.map((t) => (
          <span key={t} className="chip">
            {t}
          </span>
        ))}
        {p.waypoints.length ? (
          <span>
            via {p.waypoints.map((w, i) => `${i + 1}:(${w[0]},${w[1]})`).join(" → ")}
          </span>
        ) : null}
      </div>
      {p.rationale ? <blockquote>{p.rationale}</blockquote> : null}
    </article>
  );
}

function Item({ item, who }: { item: FeedItem; who: string }) {
  switch (item.kind) {
    case "decision":
      return <DecisionCard d={item.d} outcome={item.outcome} who={who} />;
    case "plan":
      return <PlanCard p={item.p} />;
    case "goal":
      return (
        <div className={`card note ${item.g.status === "achieved" ? "ok" : "warn"}`}>
          Goal {item.g.target_ref} {item.g.status} <span className="muted">({item.g.reason})</span>
        </div>
      );
    case "note":
      return <div className={`card note ${item.tone}`}>{item.text}</div>;
  }
}

/** Newest first: every tactical decision with its options, what the threat screen removed,
 * and what the skill really did; the planner's goals in between. */
export function DecisionFeed({ live }: { live: LiveState }) {
  const who = tacticalName(live.run);
  const items = [...live.feed].reverse();
  return (
    <section className="panel feed">
      <h2>
        Decisions <span className="muted small">{live.decisions} so far</span>
      </h2>
      {live.thinking ? <div className="card note info">{who} is thinking…</div> : null}
      {items.length === 0 ? <p className="muted">Decisions appear here as they are made.</p> : null}
      {items.map((item) => (
        <Item key={item.seq} item={item} who={who} />
      ))}
    </section>
  );
}
