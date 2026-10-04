"use client";

import Link from "next/link";
import { useMemo, useState } from "react";
import type { RunDecision } from "@/lib/bundle";
import { fmtTile, loopIndices } from "@/lib/view";

/** Every decision of the episode; rows inside back-and-forth loops are marked. */
export function Timeline({ run, decisions }: { run: string; decisions: RunDecision[] }) {
  const [filter, setFilter] = useState("");
  const [loopsOnly, setLoopsOnly] = useState(false);
  const loops = useMemo(() => loopIndices(decisions), [decisions]);
  const wanted = filter.replace(/[()\s]/g, "");
  const rows = decisions
    .map((d, i) => ({ d, loop: loops.has(i) }))
    .filter(({ d, loop }) => (!wanted || (d.tile && `${d.tile[0]},${d.tile[1]}` === wanted)) && (!loopsOnly || loop));
  return (
    <section className="panel">
      <h2>Decisions</h2>
      <div className="controls">
        <label>
          Tile <input value={filter} onChange={(e) => setFilter(e.target.value)} placeholder="e.g. 4,5" size={8} />
        </label>
        <label>
          <input type="checkbox" checked={loopsOnly} onChange={(e) => setLoopsOnly(e.target.checked)} /> loops only (
          {loops.size})
        </label>
      </div>
      <div className="table-wrap">
        <table className="timeline">
          <thead>
            <tr>
              <th>#</th>
              <th>Frame</th>
              <th>From</th>
              <th>Skill</th>
              <th>To</th>
              <th>Outcome</th>
              <th>p</th>
              <th>Goal</th>
            </tr>
          </thead>
          <tbody>
            {rows.map(({ d, loop }) => (
              <tr key={d.seq} className={[d.fatal ? "row-fatal" : "", loop ? "row-loop" : "", d.forced ? "row-forced" : ""].join(" ")}>
                <td>
                  {d.inspected ? <Link href={`/runs/${encodeURIComponent(run)}/${d.seq}`}>{d.seq}</Link> : d.seq}
                </td>
                <td>{d.frame}</td>
                <td className="mono">{fmtTile(d.tile)}</td>
                <td className="mono">
                  {d.skill}
                  {d.forced && <span className="muted"> forced</span>}
                  {d.fallback && <span className="badge warn">fallback</span>}
                </td>
                <td className="mono">{fmtTile(d.end_tile)}</td>
                <td>
                  {d.outcome}
                  {d.reason ? <span className="muted"> {d.reason}</span> : null}
                  {loop && <span className="badge">loop</span>}
                </td>
                <td>{d.provider_score?.toFixed(2) ?? ""}</td>
                <td className="mono">{d.goal_id}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}
