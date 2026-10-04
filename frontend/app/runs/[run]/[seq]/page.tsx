import Link from "next/link";
import { notFound } from "next/navigation";
import { GoalPanel } from "@/components/GoalPanel";
import { JsonView } from "@/components/JsonView";
import { KeyNav } from "@/components/KeyNav";
import { Perspective } from "@/components/Perspective";
import { loadDecision, loadRun } from "@/lib/bundle";
import { fmtTile } from "@/lib/view";

export default async function DecisionPage(props: PageProps<"/runs/[run]/[seq]">) {
  const { run: rawRun, seq: rawSeq } = await props.params;
  const run = decodeURIComponent(rawRun);
  const seq = Number(rawSeq);
  const [bundle, decision] = await Promise.all([loadRun(run), loadDecision(run, seq)]);
  if (!bundle || !decision) notFound();

  const inspected = bundle.decisions.filter((d) => d.inspected).map((d) => d.seq);
  const at = inspected.indexOf(seq);
  const href = (s: number | undefined) => (s === undefined ? null : `/runs/${encodeURIComponent(run)}/${s}`);
  const prev = href(at > 0 ? inspected[at - 1] : undefined);
  const next = href(at >= 0 && at < inspected.length - 1 ? inspected[at + 1] : undefined);
  const req = decision.request;
  const [system, user] = decision.bodies.azure.messages;

  return (
    <>
      <KeyNav prev={prev} next={next} />
      <nav className="crumbs">
        <Link href={`/runs/${encodeURIComponent(run)}`}>{run}</Link>
        <span>
          {prev ? <Link href={prev}>← prev</Link> : <span className="muted">← prev</span>}
          {" · "}
          {next ? <Link href={next}>next →</Link> : <span className="muted">next →</span>}
          <span className="muted"> (arrow keys)</span>
        </span>
      </nav>
      <section className="panel">
        <h1>
          Decision {seq} <span className="muted">frame {decision.frame}</span>
        </h1>
        <dl className="facts inline">
          <dt>Dave</dt>
          <dd>
            tile <span className="mono">{fmtTile(req.player.tile)}</span>, px{" "}
            <span className="mono">{req.player.px?.join(", ")}</span>, {req.player.state}, facing {req.player.facing}
          </dd>
          <dt>Lives / score</dt>
          <dd>
            {req.lives} / {req.score}
          </dd>
          <dt>Chosen</dt>
          <dd className="mono">
            {decision.chosen}
            {decision.fallback && <span className="badge warn">fallback</span>}
          </dd>
          <dt>Then</dt>
          <dd>
            {decision.executed.outcome} {decision.executed.reason ?? ""} → <span className="mono">{fmtTile(decision.executed.end_tile)}</span>
          </dd>
          <dt>Request</dt>
          <dd>
            <span className="mono">{decision.context_digest}</span>{" "}
            {decision.digest_match ? <span className="badge ok">matches the record</span> : <span className="badge warn">mismatch</span>}
          </dd>
        </dl>
      </section>

      <Perspective decision={decision} screenshot={decision.screenshot ? `/api/shot/${encodeURIComponent(run)}/${seq}` : null} />

      <div className="two-col">
        <GoalPanel decision={decision} />
        <section className="panel">
          <h2>Memory in the request</h2>
          <dl className="facts">
            {Object.entries(req.progress).map(([k, v]) => (
              <div key={k} className="fact-row">
                <dt>{k}</dt>
                <dd>{String(v)}</dd>
              </div>
            ))}
          </dl>
          <h3>Recent skills (oldest first)</h3>
          <ol className="recent">
            {req.recent.map((r, i) => (
              <li key={i}>
                <span className="mono">{r.skill}</span> {r.outcome}
                {r.reason ? ` (${r.reason})` : ""} → <span className="mono">{fmtTile(r.end_tile)}</span>
                <span className="muted"> moved [{r.moved_px?.join(", ")}]</span>
                {r.events.length ? <span className="muted"> · {r.events.join(", ")}</span> : null}
              </li>
            ))}
          </ol>
        </section>
      </div>

      <section className="panel">
        <h2>Exact payloads</h2>
        <JsonView
          tabs={[
            { label: "Jev body", note: "What OpenRouter's decisions endpoint receives: state + one choice question.", value: decision.bodies.jev },
            { label: "LLM system prompt", note: "Arm A's Azure system message.", value: system?.content ?? "" },
            {
              label: "LLM user message",
              note: "Arm A's Azure user message (the same request JSON Jev gets as state), pretty-printed.",
              value: user ? JSON.parse(user.content) : null,
            },
            { label: "Tactical request", note: "The shared request both models are built from.", value: req },
            { label: "Planner request", note: "The planner request behind the current goal.", value: decision.planner.request },
            { label: "Recorded answer", value: { recorded: decision.recorded, asked: decision.asked } },
          ]}
        />
      </section>
    </>
  );
}
