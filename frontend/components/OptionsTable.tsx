import type { Answer, Candidate, Outcome } from "@/lib/bundle";
import { estimateOf, fmtTile, sameTile, splitNotes } from "@/lib/view";
import { ProbabilityBar } from "./ProbabilityBar";

interface Props {
  candidates: Candidate[];
  chosen: string;
  recorded: Answer | null;
  asked: Answer[];
  outcomes: Outcome[] | null;
  selected: string | null;
  onSelect: (id: string | null) => void;
}

/** Every offered candidate as the models get it (description with notes), the recorded answer,
 * any live answers from `inspect --ask`, and what the candidate really does from this state. */
export function OptionsTable({ candidates, chosen, recorded, asked, outcomes, selected, onSelect }: Props) {
  const real = new Map((outcomes ?? []).map((o) => [o.candidate_id, o]));
  return (
    <div className="table-wrap">
      <table className="options">
        <thead>
          <tr>
            <th>Option</th>
            <th>What the models read</th>
            <th>{recorded ? `${recorded.provider} (recorded)` : "Recorded"}</th>
            {asked.map((a, i) => (
              <th key={i}>
                {a.provider}
                <span className="muted"> asked</span>
              </th>
            ))}
            <th>Estimated end</th>
            <th>Really happens</th>
          </tr>
        </thead>
        <tbody>
          {candidates.map((c) => {
            const { base, notes } = splitNotes(c.description);
            const est = estimateOf(c.description);
            const o = real.get(c.id);
            const wrong = o && est && (est.tile ? !sameTile(est.tile, o.end_tile) : !o.fatal);
            return (
              <tr
                key={c.id}
                className={[c.id === chosen ? "is-chosen" : "", c.id === selected ? "is-selected" : ""].join(" ")}
                onMouseEnter={() => onSelect(c.id)}
                onFocus={() => onSelect(c.id)}
                onClick={() => onSelect(c.id)}
                tabIndex={0}
              >
                <td className="mono">
                  {c.id}
                  {c.id === chosen && <span className="badge">chosen</span>}
                </td>
                <td>
                  <div>{base}</div>
                  {notes.map((n) => (
                    <div key={n} className={n.includes("died") || n.includes("fatal") ? "note danger" : "note"}>
                      {n}
                    </div>
                  ))}
                </td>
                <td>
                  <ProbabilityBar value={recorded?.probabilities?.[c.id] ?? (recorded?.chosen === c.id ? 1 : null)} highlight={c.id === chosen} />
                </td>
                {asked.map((a, i) => (
                  <td key={i}>
                    {a.probabilities ? (
                      <ProbabilityBar value={a.probabilities[c.id]} highlight={a.chosen === c.id} />
                    ) : a.chosen === c.id ? (
                      <span className="badge">picked</span>
                    ) : null}
                  </td>
                ))}
                <td className="mono">{est ? (est.tile ? fmtTile(est.tile) : est.note) : "—"}</td>
                <td className={o?.fatal ? "danger" : wrong ? "warn" : ""}>
                  {o ? (
                    <>
                      <span className="mono">{fmtTile(o.end_tile)}</span> {o.fatal ? "dies" : o.outcome}
                      {o.reason && !o.fatal ? ` (${o.reason})` : ""}
                      {wrong && <span className="muted"> · estimate wrong</span>}
                      {o.score_delta ? <span className="muted"> · +{o.score_delta}</span> : null}
                    </>
                  ) : (
                    <span className="muted">not run</span>
                  )}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}
