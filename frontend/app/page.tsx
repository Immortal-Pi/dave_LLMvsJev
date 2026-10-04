import Link from "next/link";
import { inspectDir, listRuns } from "@/lib/bundle";

export default async function Home() {
  const runs = await listRuns();
  return (
    <section className="panel">
      <h1>Inspected runs</h1>
      {runs.length === 0 ? (
        <>
          <p>
            No bundles in <code>{inspectDir()}</code>.
          </p>
          <p>Create one from the repository root, for example:</p>
          <pre>uv run dave-agent inspect --store artifacts/benchmark-dave.sqlite --run-id demo-C-L2-1</pre>
        </>
      ) : (
        <div className="table-wrap">
          <table className="timeline">
            <thead>
              <tr>
                <th>Run</th>
                <th>Arm</th>
                <th>Controller</th>
                <th>Scenario</th>
                <th>Outcome</th>
                <th>Frames</th>
                <th>Deaths</th>
                <th>Decisions</th>
                <th>Verified</th>
                <th>Inspected</th>
              </tr>
            </thead>
            <tbody>
              {runs.map((r) => (
                <tr key={r.run_id}>
                  <td>
                    <Link href={`/runs/${encodeURIComponent(r.run_id)}`}>{r.run_id}</Link>
                  </td>
                  <td>{r.arm}</td>
                  <td className="mono">{r.controller}</td>
                  <td>{r.scenario}</td>
                  <td>
                    {r.episode.outcome} <span className="muted">{r.episode.termination_reason}</span>
                  </td>
                  <td>{r.episode.frames}</td>
                  <td>{r.episode.deaths}</td>
                  <td>{r.episode.decisions}</td>
                  <td>{r.digests_verified}</td>
                  <td className="muted">{r.inspected_at}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}
