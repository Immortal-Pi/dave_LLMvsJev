// Types and server-side loaders for inspection bundles written by `dave-agent inspect`
// (docs/inspector.md). The app only reads these files; it never calls a model or writes.
import "server-only";
import { readdir, readFile, stat } from "node:fs/promises";
import path from "node:path";

export type Tile = [number, number];

export interface RunDecision {
  seq: number;
  frame: number;
  tile: Tile | null;
  chosen: string;
  skill: string | null;
  outcome: string | null;
  reason: string | null;
  end_tile: Tile | null;
  goal_id: string | null;
  forced: boolean;
  fallback: boolean;
  provider_score: number | null;
  fatal: boolean;
  inspected: boolean;
}

export interface GoalEvent {
  seq: number;
  frame: number;
  event_type: string;
  goal_id?: string;
  goal_type?: string;
  target_ref?: string;
  rationale?: string;
  candidates?: string[];
  triggers?: string[];
  waypoint?: { col: number; row: number } | null;
  route?: Record<string, unknown>;
  reason?: string;
  status?: string;
  fallback?: boolean;
  attempts?: number;
}

export interface RunBundle {
  schema_version: number;
  run_id: string;
  episode_key: string;
  arm: string;
  controller: string;
  adapter: string;
  scenario: string;
  seed: number;
  store: string;
  graph: string | null;
  inspected_at: string;
  episode: {
    outcome: string | null;
    termination_reason: string | null;
    frames: number | null;
    score: number | null;
    lives: number | null;
    deaths: number | null;
    decisions: number | null;
    model_calls: number | null;
  };
  digests_verified: number;
  decisions: RunDecision[];
  goals: GoalEvent[];
}

export interface Candidate {
  id: string;
  description: string;
  max_frames: number;
}

export interface TacticalRequest {
  level_id: string;
  frame: number;
  player: {
    tile: Tile | null;
    px: Tile | null;
    state: string | null;
    grounded: boolean | null;
    facing: string | null;
    velocity: Tile | null;
  };
  lives: number | null;
  inventory: Record<string, number> | null;
  score: number | null;
  view: { origin: Tile; rows: string[] };
  entities: { type: string; tile: Tile | null; velocity: Tile | null }[];
  goal: {
    goal_type: string;
    target: string;
    success: string;
    waypoint: Tile | null;
    waypoint_offset: Tile | null;
    constraints: string[];
    frames_left: number | null;
  } | null;
  progress: Record<string, number | boolean>;
  recent: {
    skill: string;
    outcome: string;
    reason: string | null;
    events: string[];
    end_tile: Tile | null;
    moved_px: Tile | null;
  }[];
  candidates: Candidate[];
}

export interface Outcome {
  candidate_id: string;
  skill: string;
  outcome: string;
  reason: string | null;
  fatal: boolean;
  end_tile: Tile | null;
  end_px: Tile | null;
  end_state: string | null;
  frames: number;
  events: string[];
  score_delta: number | null;
  trajectory: Tile[];
}

export interface Answer {
  provider: string;
  model: string;
  status: string;
  chosen: string | null;
  probabilities: Record<string, number> | null;
  confidence: number | null;
  latency_ms: number | null;
  cost_usd: number | null;
  usage: Record<string, number> | null;
  error?: string | null;
  asked_at?: string;
  provider_score?: number | null;
}

export interface DecisionBundle {
  schema_version: number;
  seq: number;
  frame: number;
  tile: Tile | null;
  chosen: string;
  skill: string | null;
  goal_id: string | null;
  fallback: boolean;
  context_digest: string;
  digest_match: boolean;
  request: TacticalRequest;
  bodies: {
    jev: Record<string, unknown>;
    azure: { messages: { role: string; content: string }[] } & Record<string, unknown>;
  };
  recorded: Answer | null;
  executed: { skill: string | null; outcome: string | null; reason: string | null; frames: number | null; end_tile: Tile | null };
  planner: { goal: GoalEvent | null; request: Record<string, unknown> | null };
  outcomes: Outcome[] | null;
  screenshot: string | null;
  asked: Answer[];
}

const SAFE_NAME = /^[A-Za-z0-9._-]+$/;

export function inspectDir(): string {
  // Read at request time from outside the app; not part of the build output.
  return path.resolve(/*turbopackIgnore: true*/ process.cwd(), process.env.INSPECT_DIR ?? "../artifacts/inspect");
}

function runDir(run: string): string {
  if (!SAFE_NAME.test(run) || run.startsWith(".")) throw new Error(`invalid run id: ${run}`);
  return path.join(inspectDir(), run);
}

async function readJson<T>(file: string): Promise<T | null> {
  try {
    return JSON.parse(await readFile(file, "utf-8")) as T;
  } catch {
    return null;
  }
}

export async function listRuns(): Promise<RunBundle[]> {
  let names: string[];
  try {
    names = await readdir(inspectDir());
  } catch {
    return [];
  }
  const runs = await Promise.all(
    names.filter((n) => SAFE_NAME.test(n)).map((n) => readJson<RunBundle>(path.join(inspectDir(), n, "run.json"))),
  );
  return runs
    .filter((r): r is RunBundle => r !== null)
    .sort((a, b) => b.inspected_at.localeCompare(a.inspected_at));
}

export async function loadRun(run: string): Promise<RunBundle | null> {
  return readJson<RunBundle>(path.join(runDir(run), "run.json"));
}

export async function loadDecision(run: string, seq: number): Promise<DecisionBundle | null> {
  if (!Number.isInteger(seq) || seq < 0) return null;
  return readJson<DecisionBundle>(path.join(runDir(run), "decisions", `${seq}.json`));
}

export async function readScreenshot(run: string, seq: number): Promise<Uint8Array<ArrayBuffer> | null> {
  if (!Number.isInteger(seq) || seq < 0) return null;
  try {
    const file = path.join(runDir(run), "decisions", `${seq}.bmp`);
    return (await stat(file)).isFile() ? new Uint8Array(await readFile(file)) : null;
  } catch {
    return null;
  }
}
