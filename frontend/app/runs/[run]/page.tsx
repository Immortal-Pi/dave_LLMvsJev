import Link from "next/link";
import { notFound } from "next/navigation";
import { MiniMap } from "@/components/MiniMap";
import { Timeline } from "@/components/Timeline";
import { loadRun } from "@/lib/bundle";

export default async function RunPage(props: PageProps<"/runs/[run]">) {
  const { run } = await props.params;
  const bundle = await loadRun(decodeURIComponent(run));
  if (!bundle) notFound();
  const e = bundle.episode;
  const firstInspected = bundle.decisions.find((d) => d.inspected);
  return (
    <>
      <section className="panel">
        <h1>
          {bundle.run_id} <span className="muted">arm {bundle.arm} · {bundle.controller} · {bundle.scenario}</span>
        </h1>
        <dl className="facts inline">
          <dt>Outcome</dt>
          <dd>
            {e.outcome} <span className="muted">{e.termination_reason}</span>
          </dd>
          <dt>Frames</dt>
          <dd>{e.frames}</dd>
          <dt>Score</dt>
          <dd>{e.score}</dd>
          <dt>Deaths</dt>
          <dd>{e.deaths}</dd>
          <dt>Decisions</dt>
          <dd>{e.decisions}</dd>
          <dt>Requests verified</dt>
          <dd>
            {bundle.digests_verified} <span className="muted">(rebuilt requests match the recorded digests)</span>
          </dd>
          <dt>Graph</dt>
          <dd className="mono">{bundle.graph ?? "none (arm without memory)"}</dd>
        </dl>
        {firstInspected && (
          <p>
            <Link href={`/runs/${encodeURIComponent(bundle.run_id)}/${firstInspected.seq}`}>Open the first inspected decision →</Link>
          </p>
        )}
      </section>
      <div className="two-col">
        <MiniMap decisions={bundle.decisions} />
        <section className="panel">
          <h2>Goals</h2>
          <ol className="goals">
            {bundle.goals.map((g) => (
              <li key={g.seq} className={g.event_type === "goal_set" ? "" : "muted"}>
                <span className="mono">f{g.frame}</span> {g.event_type.replace("goal_", "")}{" "}
                <span className="mono">{g.target_ref ?? g.goal_id}</span>
                {g.reason ? ` (${g.reason})` : ""}
                {g.waypoint ? <span className="muted"> → waypoint ({g.waypoint.col},{g.waypoint.row})</span> : null}
                {g.rationale && <div className="muted small">{g.rationale}</div>}
              </li>
            ))}
          </ol>
        </section>
      </div>
      <Timeline run={bundle.run_id} decisions={bundle.decisions} />
    </>
  );
}
