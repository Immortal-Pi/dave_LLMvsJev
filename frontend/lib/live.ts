"use client";
// The live viewer's connection to `dave-agent live` (src/dave_agent/runner/live.py): event types,
// the Server-Sent Events hook and the start/stop calls. Client-side only.
import { useEffect, useReducer } from "react";

export const LIVE_URL = process.env.NEXT_PUBLIC_LIVE_URL ?? "http://127.0.0.1:8765";

export type Tile = [number, number];

export type ObsView = {
  frame: number;
  level_id: string;
  tile: Tile | null;
  state: string;
  score: number | null;
  lives: number | null;
  inventory: Record<string, number> | null;
};

export type CallView = { provider: string; model: string; status: string; latency_ms: number | null; cost_usd: number | null };

export type RunInfo = {
  run_id: string;
  scenario: string;
  arm: string;
  seed: number;
  mode: string;
  planner: string;
  tactical: string;
  arm_config: { planner: string; tactical: string; graph_enabled: boolean };
};

export type LevelMapView = {
  origin: Tile;
  col_ruler: string[];
  rows: string[];
  screen_cols: Tile;
  legend: string;
};

export type PlanEvent = {
  frame: number;
  triggers: string[];
  chosen: string;
  goal_id: string;
  rationale: string | null;
  waypoint: { col: number; row: number } | null;
  waypoints: Tile[];
  route: Record<string, unknown> | null;
  fallback: boolean;
  fallback_reason: string | null;
  attempts: number;
  errors: string[];
  model_ms: number;
  calls: CallView[];
  candidates: { id: string; description: string; route: Record<string, unknown> | null }[];
  map: LevelMapView | null;
};

export type DecisionEvent = ObsView & {
  observation_id: number;
  goal: { target: string; type: string; waypoint: Tile | null } | null;
  candidates: { id: string; skill: string; description: string }[];
  screened: Record<string, string>;
  chosen: string;
  forced: boolean;
  fallback: boolean;
  fallback_reason: string | null;
  probabilities: Record<string, number> | null;
  calls: CallView[];
};

export type OutcomeEvent = ObsView & {
  candidate_id: string;
  skill: string;
  outcome: string;
  reason: string | null;
  frames: number;
  events: string[];
};

export type GoalEvent = { frame: number; goal_id: string; target_ref: string; status: string; reason: string };

export type FeedItem =
  | { kind: "decision"; seq: number; d: DecisionEvent; outcome: OutcomeEvent | null }
  | { kind: "plan"; seq: number; p: PlanEvent }
  | { kind: "goal"; seq: number; g: GoalEvent }
  | { kind: "note"; seq: number; tone: "info" | "warn" | "danger"; text: string };

export type LiveState = {
  connected: boolean;
  run: RunInfo | null;
  running: boolean;
  thinking: { frame: number; options: number; since: number } | null;
  now: ObsView | null;
  feed: FeedItem[]; // oldest first
  plans: PlanEvent[];
  decisions: number;
  deaths: number;
  finished: { outcome: string; termination_reason: string } | null;
};

const MAX_FEED = 400;

const initial: LiveState = {
  connected: false,
  run: null,
  running: false,
  thinking: null,
  now: null,
  feed: [],
  plans: [],
  decisions: 0,
  deaths: 0,
  finished: null,
};

type Action = { type: "connected"; value: boolean } | { type: "event"; name: string; seq: number; data: any }; // eslint-disable-line @typescript-eslint/no-explicit-any

function push(feed: FeedItem[], item: FeedItem): FeedItem[] {
  const out = [...feed, item];
  return out.length > MAX_FEED ? out.slice(out.length - MAX_FEED) : out;
}

function reduce(state: LiveState, action: Action): LiveState {
  if (action.type === "connected") return { ...state, connected: action.value };
  const { name, seq, data } = action;
  switch (name) {
    case "reset":
      return { ...initial, connected: state.connected };
    case "run":
      return { ...state, run: data as RunInfo, running: true };
    case "episode":
      if (data.status === "started") return { ...state, now: data as ObsView };
      if (data.status === "finished")
        return {
          ...state,
          now: data as ObsView,
          finished: { outcome: data.outcome, termination_reason: data.termination_reason },
          feed: push(state.feed, { kind: "note", seq, tone: "info", text: `Episode ended: ${data.outcome} (${data.termination_reason})` }),
        };
      if (data.status === "stopped")
        return { ...state, thinking: null, finished: { outcome: "stopped", termination_reason: "stopped from the viewer" } };
      return state;
    case "plan":
      return { ...state, plans: [...state.plans, data as PlanEvent].slice(-50), feed: push(state.feed, { kind: "plan", seq, p: data }) };
    case "goal":
      return { ...state, feed: push(state.feed, { kind: "goal", seq, g: data }) };
    case "deciding":
      return { ...state, thinking: { frame: data.frame, options: data.options, since: Date.now() } };
    case "decision":
      return {
        ...state,
        thinking: null,
        now: data as ObsView,
        decisions: state.decisions + 1,
        feed: push(state.feed, { kind: "decision", seq, d: data, outcome: null }),
      };
    case "outcome": {
      const o = data as OutcomeEvent;
      const feed = [...state.feed];
      for (let i = feed.length - 1; i >= 0; i--) {
        const item = feed[i];
        if (item.kind === "decision" && !item.outcome && item.d.chosen === o.candidate_id) {
          feed[i] = { ...item, outcome: o };
          break;
        }
      }
      return { ...state, now: o, feed, deaths: state.deaths + (o.events.includes("death") ? 1 : 0) };
    }
    case "notice":
      return { ...state, feed: push(state.feed, { kind: "note", seq, tone: "warn", text: `Paid run: ${JSON.stringify(data.budget)}` }) };
    case "error":
      return { ...state, feed: push(state.feed, { kind: "note", seq, tone: "danger", text: data.message }) };
    case "idle":
      return { ...state, running: false, thinking: null };
    default:
      return state;
  }
}

const EVENTS = ["reset", "run", "episode", "plan", "goal", "deciding", "decision", "outcome", "notice", "error", "idle", "summary"];

/** Subscribe to the live server's event stream. Reconnects (and catches up) on its own. */
export function useLive(): LiveState {
  const [state, dispatch] = useReducer(reduce, initial);
  useEffect(() => {
    const source = new EventSource(`${LIVE_URL}/events`);
    source.onopen = () => dispatch({ type: "connected", value: true });
    source.onerror = () => dispatch({ type: "connected", value: false });
    for (const name of EVENTS) {
      source.addEventListener(name, (e) => {
        const msg = e as MessageEvent<string>;
        dispatch({ type: "event", name, seq: Number(msg.lastEventId), data: JSON.parse(msg.data) });
      });
    }
    return () => source.close();
  }, []);
  return state;
}

export type ServerStatus = {
  running: boolean;
  run_id: string | null;
  adapter: string;
  levels: string[];
  allow_paid: boolean;
  arms: Record<string, { planner: string; tactical: string; graph_enabled: boolean }>;
  max_frames: number;
};

export async function getStatus(): Promise<ServerStatus> {
  const res = await fetch(`${LIVE_URL}/status`, { cache: "no-store" });
  return res.json();
}

export async function startRun(body: { scenario: string; arm: string; planner: string; tactical: string }): Promise<string | null> {
  const res = await fetch(`${LIVE_URL}/start`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (res.ok) return null;
  const data = await res.json().catch(() => ({ error: res.statusText }));
  return data.error ?? res.statusText;
}

export async function stopRun(): Promise<void> {
  await fetch(`${LIVE_URL}/stop`, { method: "POST" });
}

/** "threat:hazard@44" -> "hazard in 44 ticks". */
export function screenReason(reason: string): string {
  const m = reason.match(/^threat:(.+)@(\d+)$/);
  if (!m) return reason;
  const what = m[1].replace(/\d+$/, "");
  return `${what} in ${m[2]} ticks`;
}

/** The target tile in a goal id such as "collect:trophy:c13:r6". */
export function goalTile(goalId: string | null | undefined): Tile | null {
  const m = goalId?.match(/:c(\d+):r(\d+)$/);
  return m ? [Number(m[1]), Number(m[2])] : null;
}

/** "jev" / "llm" for the arm's tactical model, as a display name. */
export function tacticalName(run: RunInfo | null): string {
  if (!run) return "Model";
  if (run.tactical.startsWith("mock")) return run.arm_config.tactical === "jev" ? "Jev (mock)" : "LLM (mock)";
  return run.arm_config.tactical === "jev" ? "Jev" : "LLM";
}
