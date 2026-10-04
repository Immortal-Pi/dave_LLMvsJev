"use client";
import { useEffect, useState } from "react";
import { DecisionFeed } from "@/components/live/DecisionFeed";
import { GameView } from "@/components/live/GameView";
import { PlannerPanel } from "@/components/live/PlannerPanel";
import { getStatus, LIVE_URL, startRun, stopRun, useLive, type ServerStatus } from "@/lib/live";

const ARM_LABEL: Record<string, string> = { A: "A · LLM tactical", B: "B · Jev tactical", C: "C · Jev + learned graph" };

export default function LivePage() {
  const live = useLive();
  const [status, setStatus] = useState<ServerStatus | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [scenario, setScenario] = useState("level1");
  const [arm, setArm] = useState("B");
  const [planner, setPlanner] = useState("mock");
  const [tactical, setTactical] = useState("mock");
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    getStatus()
      .then((s) => {
        setStatus(s);
        setScenario((current) => (s.levels.includes(current) ? current : s.levels[0]));
      })
      .catch(() => setError(`No live server at ${LIVE_URL}. Start it with: uv run dave-agent live`));
  }, [live.connected]);

  const start = async () => {
    setBusy(true);
    setError(null);
    try {
      setError(await startRun({ scenario, arm, planner, tactical }));
    } catch {
      setError(`No live server at ${LIVE_URL}. Start it with: uv run dave-agent live`);
    } finally {
      setBusy(false);
    }
  };

  // While a run is going (also after a reload), the selectors show its settings.
  const run = live.running ? live.run : null;
  const shown = run
    ? {
        scenario: run.scenario,
        arm: run.arm,
        planner: run.planner.startsWith("mock") ? "mock" : "live",
        tactical: run.tactical.startsWith("mock") ? "mock" : "live",
      }
    : { scenario, arm, planner, tactical };
  const paid = planner === "live" || tactical === "live";
  return (
    <div className="live">
      <section className="panel live-controls">
        <label>
          Level
          <select value={shown.scenario} onChange={(e) => setScenario(e.target.value)} disabled={live.running}>
            {(status?.levels ?? ["level1"]).map((l) => (
              <option key={l}>{l}</option>
            ))}
          </select>
        </label>
        <label>
          Arm
          <select value={shown.arm} onChange={(e) => setArm(e.target.value)} disabled={live.running}>
            {Object.keys(status?.arms ?? ARM_LABEL).map((a) => (
              <option key={a} value={a}>
                {ARM_LABEL[a] ?? a}
              </option>
            ))}
          </select>
        </label>
        <label>
          Planner
          <select value={shown.planner} onChange={(e) => setPlanner(e.target.value)} disabled={live.running}>
            <option value="mock">rule (free)</option>
            <option value="live">Azure LLM (paid)</option>
          </select>
        </label>
        <label>
          Tactical
          <select value={shown.tactical} onChange={(e) => setTactical(e.target.value)} disabled={live.running}>
            <option value="mock">mock (free)</option>
            <option value="live">{shown.arm === "A" ? "Azure LLM" : "Jev"} (paid)</option>
          </select>
        </label>
        {live.running ? (
          <button type="button" className="stop" onClick={() => stopRun()}>
            Stop
          </button>
        ) : (
          <button type="button" onClick={start} disabled={busy || !status}>
            Start
          </button>
        )}
        <span className="muted small status-line">
          {live.connected ? "connected" : "not connected"}
          {live.run ? ` · ${live.run.run_id} · ${live.run.mode}` : ""}
          {paid && status && !status.allow_paid ? " · paid models need: dave-agent live --allow-paid" : ""}
        </span>
        {error ? <p className="error-line">{error}</p> : null}
      </section>
      <div className="live-grid">
        <GameView live={live} />
        <DecisionFeed live={live} />
        <PlannerPanel live={live} />
      </div>
    </div>
  );
}
