/**
 * Types shared by the read-only server and the Vue client.
 *
 * Every interface mirrors a table in sssf.db one-for-one (see
 * references/observability.md). Nothing here is derived state: phase durations,
 * session progress and lane layout are computed in the UI, never stored.
 */

/** sessions.status — a run is running until it earns success. */
export type SessionStatus = "running" | "success" | "fail";

/** phases.status — queued only for manifest-declared phases not yet entered. */
export type PhaseStatus = "queued" | "running" | "success" | "fail";

/** phases.kind — decides which lane a block renders in. */
export type PhaseKind = "engineer" | "code" | "agent";

/** events.type — the ten types tracer.py emits. */
export type EventType =
  | "phase_start"
  | "phase_end"
  | "agent_start"
  | "agent_end"
  | "tool_call"
  | "handoff"
  | "gate_pass"
  | "gate_fail"
  | "log"
  | "error";

export interface Session {
  adw_id: string;
  /** ADW script(s) that ran this session, e.g. "adw_plan + adw_build_test". */
  adw_name: string | null;
  request: string | null;
  status: SessionStatus | null;
  engineer: string | null;
  started_at: string | null;
  ended_at: string | null;
  total_tokens: number | null;
  total_cost: number | null;
  /** Non-dollar billing units reported by harnesses such as Kiro CLI. */
  total_credits: number | null;
  /** 1 once archived out of the review list. Review state, not run state. */
  archived: number | null;
}

/**
 * A session row with its phases embedded, so the L1 table draws the
 * mini-progress dots without a second request per row.
 */
export interface SessionSummary extends Session {
  /** Full phase rows, ordered by seq — one dot each. */
  phases: Phase[];
  phase_count: number;
  /**
   * The session's agents, same shape and merge rules as SessionDetail.agents —
   * so an L1 card can color its per-agent dots without a request per card.
   */
  agents: AgentSession[];
}

export interface Phase {
  phase_id: string;
  adw_id: string;
  seq: number | null;
  name: string | null;
  kind: PhaseKind | null;
  owner: string | null;
  description: string | null;
  status: PhaseStatus | null;
  attempt: number | null;
  retries: number | null;
  error: string | null;
  started_at: string | null;
  ended_at: string | null;
}

export interface Event {
  /** SQLite rowid — the polling cursor. Monotonic, insertion-ordered. */
  rowid: number;
  event_id: string;
  adw_id: string;
  phase_id: string | null;
  /** Span nesting: an agent phase expands into its tool-call children. */
  parent_id: string | null;
  type: EventType | null;
  name: string | null;
  /** Raw JSON string as written by the tracer; parse at the point of display. */
  payload_json: string | null;
  tokens: number | null;
  started_at: string | null;
  ended_at: string | null;
}

export interface Envelope {
  envelope_id: string;
  adw_id: string;
  phase_id: string | null;
  agent: string | null;
  /** Name of the data_types model the response was parsed against. */
  output_type: string | null;
  payload_json: string | null;
  /** SQLite integer boolean. */
  valid: number | null;
  attempt: number | null;
  created_at: string | null;
}

export interface GateResult {
  id: number;
  adw_id: string;
  phase_id: string | null;
  attempt: number | null;
  gate: string | null;
  /** SQLite integer boolean. */
  passed: number | null;
  /** JSON array of violation strings; "[]" on a pass. */
  violations_json: string | null;
  /**
   * JSON array of GateCheck — the per-item evidence behind the verdict, so a
   * green gate can say WHAT it verified rather than only that it passed.
   * Null on rows written before the tracer recorded checks; those are not
   * backfilled, so fall back to the verdict alone.
   */
  checks_json: string | null;
  created_at: string | null;
}

/** One item a gate inspected — the parsed element of `checks_json`. */
export interface GateCheck {
  item: string;
  ok: boolean;
  note: string;
}

/** agent_sessions — the queryable mirror of agent_map.json. Supplies lane labels (`name · model`). */
export interface AgentSession {
  adw_id: string;
  agent: string;
  coding_agent: string | null;
  model: string | null;
  session_id: string | null;
  /**
   * The agent's lane color from sssf.config.yaml, e.g. "#a78bfa". Null on dbs
   * written by a tracer predating the column, and on agents with no configured
   * color — fall back to the UI's own palette.
   */
  color: string | null;
  /**
   * How full the agent's context window was after its last turn, and the
   * model's ceiling. Null on dbs predating the columns and on an agent still
   * running — the lane draws no bar rather than a misleading empty one.
   */
  context_tokens: number | null;
  context_window: number | null;
  created_at: string | null;
  last_used_at: string | null;
}

// ── payload_json shapes ──────────────────────────────────────────────────────
// events.payload_json is stored as a string. These are the parsed shapes for
// the two payloads the UI renders; every field is optional because the tracer
// writes what the coding agent reported, which varies by agent and by version.

/** Parsed `agent_start` payload — the live source of a lane's label and color. */
export interface AgentStartPayload {
  model?: string;
  thinking?: string;
  session_id?: string;
  color?: string;
  coding_agent?: string;
  purpose?: string;
  /** Tool allowlist; null means all tools. Absent on pre-config-payload rows. */
  tools?: string[] | null;
  harness_engineering?: string[];
}

/**
 * Tokens and dollars per component for one agent phase, summed across every
 * send it made (a retried phase paid more than once). Mirrors pi's `usage`:
 * `input_tokens` EXCLUDES cache reads, which bill at their own rate.
 */
export interface UsageBreakdown {
  input_tokens: number;
  output_tokens: number;
  cache_read_tokens: number;
  cache_write_tokens: number;
  /**
   * Thinking tokens — the reasoning SHARE of `output_tokens`, not a fifth
   * component. Billed at the output rate; adding it to the others would
   * double-count. Absent (undefined) on runs predating the field.
   */
  reasoning_tokens?: number;
  total_tokens: number;
  input_cost: number;
  output_cost: number;
  cache_read_cost: number;
  cache_write_cost: number;
  total_cost: number;
}

/** Parsed `agent_end` payload — closes out a call with its cost and context use. */
export interface AgentEndPayload {
  cost?: number;
  /** Absent on runs predating the breakdown; `cost` alone survives there. */
  usage?: UsageBreakdown;
  /** Window occupancy after the final turn, and the model's ceiling. */
  context_tokens?: number;
  context_window?: number;
}

/**
 * Parsed `tool_call` payload — one event per real tool call, emitted when the
 * tool returns. `result_snippet` and `duration_ms` are absent when the coding
 * agent never reported a result.
 */
export interface ToolCallPayload {
  tool?: string;
  tool_call_id?: string;
  args?: Record<string, unknown>;
  result_snippet?: string;
  ok?: boolean;
  duration_ms?: number;
  agent?: string;
}

// ── API responses ────────────────────────────────────────────────────────────

/** GET /api/sessions */
export type SessionsResponse = SessionSummary[];

/** GET /api/sessions/:adw_id */
/**
 * What actually moved through a session, summed across every agent.
 *
 * Deliberately NOT the billed total: `sessions.total_tokens` also counts every
 * cached re-read, which is the same context charged again on each turn.
 */
export interface SessionUsage {
  /** Raw prompt tokens read for the first time: new input + cache writes. */
  read: number;
  /** Tokens generated. Each produced exactly once, so this needs no adjusting. */
  written: number;
}

export interface SessionDetail {
  session: Session;
  /** Derived from agent_end payloads, so historical runs have it too. */
  usage: SessionUsage;
  /** Ordered by seq. */
  phases: Phase[];
  /**
   * One entry per agent that has run OR is running under this adw_id — lane
   * labels come from here. Finished agents come from the agent_sessions table;
   * an agent still in flight has no row there yet, so its entry is built from
   * its agent_start event (coding_agent is null until it finishes).
   */
  agents: AgentSession[];
}

/**
 * GET /api/sessions/:adw_id/events?after=<rowid>&limit=500
 *
 * Poll with `after` = the cursor from the previous response. `cursor` is the
 * highest rowid in this page (or the `after` you sent, when the page is empty),
 * so it can be fed straight back in. `has_more` means the page hit the limit.
 */
export interface EventsPage {
  events: Event[];
  cursor: number;
  has_more: boolean;
}

/**
 * GET /api/sessions/:adw_id/agents/:agent/prompts
 *
 * The exact compiled prompts sent to an agent, read from
 * `{data_dir}/sessions/{adw_id}/{agent}/prompts/`. These live only as files —
 * the db has no copy. Either field is null when that file isn't on disk, which
 * is the normal state for an agent that never ran in this session, so a 200
 * with two nulls is a valid answer rather than an error.
 */
export interface AgentPrompts {
  system: string | null;
  user: string | null;
}

/** Alias matching the naming of the other endpoint payloads. */
export type PromptsResponse = AgentPrompts;

/** GET /api/sessions/:adw_id/envelopes */
export type EnvelopesResponse = Envelope[];

/** GET /api/sessions/:adw_id/gates */
export type GatesResponse = GateResult[];

/** GET /api/health */
export interface HealthResponse {
  ok: boolean;
  db: string;
  journal_mode: string;
  sessions: number;
}

export interface ApiError {
  error: string;
}
