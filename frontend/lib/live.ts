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

export type CallView = {
  provider: string;
  model: string;
  status: string;
  latency_ms: number | null;
  cost_usd: number | null;
  tokens?: number | null;
  input_tokens?: number | null;
  output_tokens?: number | null;
};

export type RunInfo = {
  run_id: string;
  scenario: string;
  arm: string;
  seed: number;
  mode: string;
  planner: string;
  tactical: string;
  arm_config: { planner: string; tactical: string; graph_enabled: boolean };
  /** false: the game runs on while models think (absent on older servers: paused). */
  pause?: boolean;
};

export type LevelMapView = {
  origin: Tile;
  col_ruler: string[];
  rows: string[];
  screen_cols: Tile;
  legend: string;
};

/** A platform as the planner saw it (control/platforms.py). */
export type PlatformView = {
  id: string;
  row: number;
  cols: Tile;
  reachable: boolean;
  hops?: number;
  exits: { to: string; by: string; from_col: number; note?: string }[];
  items?: string[];
  open?: string[];
  danger?: string[];
};

/** One goal tried on this level (control/attempts.py). */
export type AttemptView = {
  goal: string;
  waypoints: Tile[];
  outcome: string;
  reason?: string;
  start: Tile | null;
  furthest: Tile | null;
  frames?: number;
  waypoints_reached?: string;
};

export type FailedLink = { from: Tile; to: Tile; times: number; how: string[]; avoid: boolean };

/** Estimated move: landing cell and how Dave gets there ("start", walk, fall, jump, unknown). */
/** A jump also carries its simulated flight: Dave's top-left every 3rd tick, in tile units. */
export type PathStep = [number, number, string] | [number, number, string, [number, number][]];

/** `spawn` > 0: a shot not fired yet, fired in that many ticks. */
export type ThreatPath = { id: string; kind: string; spawn?: number; path: [number, number][] };

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
  /** Who chose: the planner model, the rule priority without a call (planning.llm_calls: escalate), or the fallback. */
  planner?: "llm" | "rule" | "fallback";
  model_ms: number;
  calls: CallView[];
  candidates: { id: string; description: string; route: Record<string, unknown> | null; path?: string | null }[];
  map: LevelMapView | null;
  platforms?: PlatformView[];
  tried?: AttemptView[];
  failed_links?: FailedLink[];
  path?: PathStep[];
  deaths?: { cause: string; tile: Tile | null }[];
};

/** The live graph's score of one option (graph arms; control/move_score.py): ``q`` is the cost
 * to the goal after it (lower is better), ``regret`` how much worse than the best option. */
export type MoveScore = {
  q: number | null;
  regret: number | null;
  best: boolean;
  p_ok: number;
  attempts: number;
  fatal: number;
  land: string | null;
  mode: "goal" | "explore";
};

export type DecisionEvent = ObsView & {
  observation_id: number;
  goal: { target: string; type: string; waypoint: Tile | null } | null;
  candidates: { id: string; skill: string; description: string; score?: MoveScore | null }[];
  screened: Record<string, string>;
  chosen: string;
  forced: boolean;
  fallback: boolean;
  fallback_reason: string | null;
  probabilities: Record<string, number> | null;
  calls: CallView[];
  threats?: ThreatPath[];
  /** Pause off only: game ticks that passed while the model thought. */
  wait_ticks?: number;
  /** Pause off only: the candidate id the choice ran as on the latest observation. */
  runs?: string;
  /** Pause off only: why the choice came too late and was dropped (then decided again). */
  stale?: string;
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

/** One platform segment of the learned world graph (memory/graph.py ``WorldGraph.view``). */
export type GraphNode = {
  id: string;
  row: number;
  col_min: number;
  col_max: number;
  visited: boolean;
  open_left: boolean;
  open_right: boolean;
  items: { kind: string; col: number }[];
  stays: number;
  inconclusive: number;
  failed: number;
  incidents: { cause: string | null; tile: Tile | null; skill: string | null }[];
  /** Goal credit per "skill|target_ref" (control/credit.py): past goals that used the move from here. */
  credit?: Record<string, GoalCredit>;
};

export type GoalCredit = { tries: number; closer: number; goals: number; reached: number };

/** A skill move Dave really made from one platform to another, with its evidence. */
export type GraphEdge = {
  source: string;
  target: string;
  key: string;
  skill: string;
  inventory_context: string[];
  attempts: number;
  successes: number;
  failures: number;
  fatal: number;
  p: number;
  frames: number;
  last_outcome: string | null;
};

export type GraphCounts = {
  nodes: number;
  visited_nodes: number;
  edges: number;
  attempts: number;
  successes: number;
  fatal: number;
  node_failures: number;
  unanchored: number;
  suggestions: number;
};

export type GraphEvent = {
  source: "learning" | "frozen";
  level_id: string;
  graph: { level_id: string; topology_version: number; counts: GraphCounts; nodes: GraphNode[]; edges: GraphEdge[] } | null;
  /** What the last skill added (absent on the episode-start snapshot). */
  last?: { recorded: string | null; skill: string; edge: [string, string, string] | null };
  seq: number;
};

/** Running totals for one model role in this run. */
export type CallTally = { calls: number; ok: number; failed: number; latency: number[]; tokens: number; costUsd: number; costUnknown: number };

/** Which model a call went to: the Azure LLM or Jev (mock and rule calls are not counted). */
export type ModelKind = "llm" | "jev";

/** Calls, tokens and cost for one model across both roles (planner and tactical). Tokens a
 * provider did not split into input and output are kept apart in `otherTokens`. */
export type ModelTally = {
  calls: number;
  failed: number;
  planner: number;
  tactical: number;
  inputTokens: number;
  outputTokens: number;
  otherTokens: number;
  costUsd: number;
  costUnknown: number;
};

export type ModelTallies = Record<ModelKind, ModelTally>;

export const modelKind = (provider: string): ModelKind | null =>
  provider === "azure_openai" ? "llm" : provider === "openrouter_jev" ? "jev" : null;

export type RunStats = {
  models: ModelTallies;
  tactical: CallTally & { model: number; forced: number; fallback: number; fallbackReasons: Record<string, number>; chosenProb: number[] };
  planner: CallTally & { plans: number; fallback: number; triggers: Record<string, number> };
  outcomes: Record<string, number>;
  skills: Record<string, number>;
};

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
  graph: GraphEvent | null;
  stats: RunStats;
  /** Per-model tallies of every run seen since the page opened, by run id (kept across resets). */
  history: Record<string, { arm: string; models: ModelTallies }>;
};

const MAX_FEED = 400;

const tally = (): CallTally => ({ calls: 0, ok: 0, failed: 0, latency: [], tokens: 0, costUsd: 0, costUnknown: 0 });

const modelTally = (): ModelTally => ({
  calls: 0, failed: 0, planner: 0, tactical: 0, inputTokens: 0, outputTokens: 0, otherTokens: 0, costUsd: 0, costUnknown: 0,
});

export const emptyModels = (): ModelTallies => ({ llm: modelTally(), jev: modelTally() });

/** Add one role's calls to the per-model tallies. A paid call with no cost counts as unknown. */
function addModelCalls(models: ModelTallies, calls: CallView[], role: "planner" | "tactical"): ModelTallies {
  const out = { llm: { ...models.llm }, jev: { ...models.jev } };
  for (const c of calls) {
    const kind = modelKind(c.provider);
    if (!kind) continue;
    const m = out[kind];
    m.calls += 1;
    m[role] += 1;
    if (c.status !== "ok") m.failed += 1;
    if (c.input_tokens != null || c.output_tokens != null) {
      m.inputTokens += c.input_tokens ?? 0;
      m.outputTokens += c.output_tokens ?? 0;
    } else m.otherTokens += c.tokens ?? 0;
    if (c.cost_usd !== null) m.costUsd += c.cost_usd;
    else m.costUnknown += 1;
  }
  return out;
}

/** The per-model tallies summed over runs. */
export function sumModels(all: ModelTallies[]): ModelTallies {
  const out = emptyModels();
  for (const t of all)
    for (const kind of ["llm", "jev"] as const)
      for (const key of Object.keys(out[kind]) as (keyof ModelTally)[]) out[kind][key] += t[kind][key];
  return out;
}

const emptyStats = (): RunStats => ({
  models: emptyModels(),
  tactical: { ...tally(), model: 0, forced: 0, fallback: 0, fallbackReasons: {}, chosenProb: [] },
  planner: { ...tally(), plans: 0, fallback: 0, triggers: {} },
  outcomes: {},
  skills: {},
});

const bump = (counts: Record<string, number>, key: string) => ({ ...counts, [key]: (counts[key] ?? 0) + 1 });

/** Add calls to a tally. A paid call with no reported cost counts as unknown, never as zero. */
function addCalls<T extends CallTally>(t: T, calls: CallView[]): T {
  const out = { ...t, latency: [...t.latency] };
  for (const c of calls) {
    out.calls += 1;
    if (c.status === "ok") out.ok += 1;
    else out.failed += 1;
    if (c.latency_ms !== null) out.latency.push(c.latency_ms);
    out.tokens += c.tokens ?? 0;
    if (c.cost_usd !== null) out.costUsd += c.cost_usd;
    else if (c.provider !== "mock") out.costUnknown += 1;
  }
  return out;
}

function addDecision(stats: RunStats, d: DecisionEvent): RunStats {
  const models = addModelCalls(stats.models, d.calls, "tactical");
  let t = addCalls(stats.tactical, d.calls);
  if (d.forced) t = { ...t, forced: t.forced + 1 };
  else if (d.fallback) t = { ...t, fallback: t.fallback + 1, fallbackReasons: bump(t.fallbackReasons, d.fallback_reason ?? "unknown") };
  else t = { ...t, model: t.model + 1 };
  const p = d.probabilities?.[d.chosen];
  if (p !== undefined && !d.forced && !d.fallback) t = { ...t, chosenProb: [...t.chosenProb, p] };
  return { ...stats, models, tactical: t };
}

function addPlan(stats: RunStats, p: PlanEvent): RunStats {
  const models = addModelCalls(stats.models, p.calls, "planner");
  let t = addCalls(stats.planner, p.calls);
  t = { ...t, plans: t.plans + 1, fallback: t.fallback + (p.fallback ? 1 : 0) };
  for (const trigger of p.triggers) t = { ...t, triggers: bump(t.triggers, trigger) };
  return { ...stats, models, planner: t };
}

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
  graph: null,
  stats: emptyStats(),
  history: {},
};

type Action = { type: "connected"; value: boolean } | { type: "event"; name: string; seq: number; data: any }; // eslint-disable-line @typescript-eslint/no-explicit-any

function push(feed: FeedItem[], item: FeedItem): FeedItem[] {
  const out = [...feed, item];
  return out.length > MAX_FEED ? out.slice(out.length - MAX_FEED) : out;
}

/** Keep this run's per-model tallies in the history, so they outlive the next run's reset. A
 * reconnect replays the run under the same id and overwrites its entry instead of adding twice. */
function reduce(state: LiveState, action: Action): LiveState {
  const next = step(state, action);
  if (!next.run || next.stats.models === state.stats.models) return next;
  return { ...next, history: { ...next.history, [next.run.run_id]: { arm: next.run.arm, models: next.stats.models } } };
}

function step(state: LiveState, action: Action): LiveState {
  if (action.type === "connected") return { ...state, connected: action.value };
  const { name, seq, data } = action;
  switch (name) {
    case "reset":
      return { ...initial, connected: state.connected, history: state.history };
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
      return {
        ...state,
        plans: [...state.plans, data as PlanEvent].slice(-50),
        feed: push(state.feed, { kind: "plan", seq, p: data }),
        stats: addPlan(state.stats, data as PlanEvent),
      };
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
        stats: addDecision(state.stats, data as DecisionEvent),
      };
    case "outcome": {
      const o = data as OutcomeEvent;
      const feed = [...state.feed];
      for (let i = feed.length - 1; i >= 0; i--) {
        const item = feed[i];
        if (item.kind === "decision" && !item.outcome && !item.d.stale && (item.d.runs ?? item.d.chosen) === o.candidate_id) {
          feed[i] = { ...item, outcome: o };
          break;
        }
      }
      const stats = { ...state.stats, outcomes: bump(state.stats.outcomes, o.outcome), skills: bump(state.stats.skills, o.skill) };
      return { ...state, now: o, feed, stats, deaths: state.deaths + (o.events.includes("death") ? 1 : 0) };
    }
    case "graph":
      return { ...state, graph: { ...(data as Omit<GraphEvent, "seq">), seq } };
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

const EVENTS = ["reset", "run", "episode", "plan", "goal", "deciding", "decision", "outcome", "graph", "notice", "error", "idle", "summary"];

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

export async function startRun(body: { scenario: string; arm: string; planner: string; tactical: string; pause: boolean }): Promise<string | null> {
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
