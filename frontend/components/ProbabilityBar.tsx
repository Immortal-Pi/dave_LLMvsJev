export function ProbabilityBar({ value, highlight }: { value: number | null | undefined; highlight?: boolean }) {
  if (value === null || value === undefined) return <span className="muted">—</span>;
  return (
    <span className="prob" title={value.toFixed(3)}>
      <span className={highlight ? "prob-fill chosen" : "prob-fill"} style={{ width: `${Math.round(value * 100)}%` }} />
      <span className="prob-label">{value.toFixed(2)}</span>
    </span>
  );
}
